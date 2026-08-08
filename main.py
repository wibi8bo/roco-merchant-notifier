import os
import json
import asyncio
import requests
from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

# ================= 1. 配置区域 =================
IMGBB_KEY = os.environ.get("IMGBB_KEY")
NOTIFYME_UUID = os.environ.get("NOTIFYME_UUID")
BARK_KEY = os.environ.get("BARK_KEY")

# 数据源：18183 远行商人助手
DATA_URL = "https://db.18183.com/lkwgyxsr/outputs/goods-cache.js"
IMAGE_BASE_URL = "https://db.18183.com/lkwgyxsr/outputs/"

NOTIFYME_SERVER = "https://notifyme-server.wzn556.top/api/send"
ASSETS_DIR = os.path.abspath("assets/yuanxing-shangren")
HTML_TEMPLATE_FILE = "index.html"
TEMP_RENDER_FILE = "temp_render.html"

BEIJING_TZ = timezone(timedelta(hours=8))

# 轮次时间表：每日 08:00 / 12:00 / 16:00 / 20:00 刷新
ROUND_HOURS = {
    1: (8, 12),
    2: (12, 16),
    3: (16, 20),
    4: (20, 24),
}


# ================= 2. 时间工具 =================

def get_beijing_time():
    """获取北京时间，GitHub Actions 运行在 UTC 环境，必须显式转换"""
    return datetime.now(BEIJING_TZ)


def calculate_round(now=None):
    """根据当前时间计算轮次，0 表示未开市"""
    now = now or get_beijing_time()
    hour = now.hour
    for round_num, (start, end) in ROUND_HOURS.items():
        if start <= hour < end:
            return round_num
    if hour >= 20:
        return 4
    return 0


def get_round_time_range(round_num):
    """获取轮次的时间区间文本"""
    if round_num not in ROUND_HOURS:
        return "未开始"
    start, end = ROUND_HOURS[round_num]
    return f"{start:02d}:00 - {end:02d}:00"


def get_round_end_time(round_num, now=None):
    """获取轮次结束时刻"""
    now = now or get_beijing_time()
    if round_num not in ROUND_HOURS:
        return None
    _, end_hour = ROUND_HOURS[round_num]
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(hours=end_hour)


def get_round_info():
    """构建页面顶部的轮次与倒计时信息"""
    now = get_beijing_time()
    current_round = calculate_round(now)

    if current_round == 0:
        # 00:00-08:00 未开市，显示距次日开市的时间
        next_open = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_open:
            next_open += timedelta(days=1)
        remaining = int((next_open - now).total_seconds())
        hours, rem = divmod(remaining, 3600)
        minutes = rem // 60
        return {
            "current": 0,
            "total": 4,
            "countdown": f"{hours}小时{minutes}分钟",
            "is_market_open": False,
        }

    round_end = get_round_end_time(current_round, now)
    remaining = max(0, int((round_end - now).total_seconds()))
    hours, rem = divmod(remaining, 3600)
    minutes = rem // 60
    countdown = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"

    return {
        "current": current_round,
        "total": 4,
        "countdown": countdown,
        "is_market_open": True,
    }


def parse_datetime(text):
    """把 '2026-08-08 08:00:00' 解析为带北京时区的 datetime"""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        return None


def normalize_end_time(dt):
    """23:59:59 表示当天营业结束，向上对齐到整点便于倒计时显示"""
    if dt is None:
        return None
    if dt.minute == 59 and dt.second == 59:
        return (dt + timedelta(seconds=1)).replace(microsecond=0)
    return dt


def format_countdown(seconds):
    """格式化倒计时为 HH:MM:SS"""
    if seconds <= 0:
        return "00:00:00"
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_price(price):
    """价格千分位显示"""
    if price is None:
        return None
    return f"{price:,}"


def get_full_image_url(image):
    """把相对路径补全为完整图片地址"""
    if not image:
        return ""
    if image.startswith("http://") or image.startswith("https://"):
        return image
    return IMAGE_BASE_URL + image.lstrip("/")


# ================= 3. 数据获取与解析 =================

def fetch_merchant_data():
    """从 18183 获取远行商人数据

    数据源是形如 `const GOODS_CACHE = {...}` 的 JS 赋值语句，
    需剥离外壳后按 JSON 解析。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://db.18183.com/lkwgyxsr/index.html",
    }

    # 源站 Cache-Control 长达十年，必须加时间戳参数绕过缓存
    url = f"{DATA_URL}?_t={int(datetime.now().timestamp() * 1000)}"

    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            text = resp.text

            start = text.find("{")
            if start == -1:
                raise ValueError("数据格式异常：未找到 JSON 起始位置")

            raw = json.loads(text[start:].strip().rstrip(";"))
            products = transform_products(raw)

            if not products:
                raise ValueError("数据源返回空商品列表")

            print(f"✅ 数据获取成功，共 {len(products)} 条商品记录")
            return products

        except Exception as e:
            last_error = e
            print(f"⚠️ 第 {attempt + 1} 次获取失败: {e}")
            if attempt < 2:
                import time
                time.sleep(3 * (attempt + 1))

    print(f"❌ 数据获取失败: {last_error}")
    return None


def transform_products(raw):
    """把源数据摊平为统一的商品列表"""
    products = []

    for group in raw.get("list") or []:
        if not isinstance(group, dict):
            continue

        try:
            batch = int(group.get("batch"))
        except (TypeError, ValueError):
            continue

        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue

            start_dt = parse_datetime(item.get("stime"))
            end_dt = normalize_end_time(parse_datetime(item.get("etime")))
            if start_dt is None or end_dt is None:
                continue

            name = (item.get("name") or "").strip()
            if not name:
                continue

            duration_hours = (end_dt - start_dt).total_seconds() / 3600

            products.append({
                # dict_id + batch 组合确保全天商品在各轮次不被误判为重复
                "id": f"{item.get('dict_id') or item.get('id')}_{batch}",
                "batch": batch,
                "name": name,
                "description": (item.get("content") or "").strip(),
                "image": get_full_image_url(item.get("image")),
                "price": to_int(item.get("price")),
                "buy_limit": to_int(item.get("buy_limit")),
                "start_dt": start_dt,
                "end_dt": end_dt,
                # 售卖超过 4 小时视为全天长效商品
                "is_all_day": duration_hours > 4,
            })

    return products


def to_int(value):
    """安全转整数，源数据的数值字段都是字符串"""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ================= 4. 模板数据组装 =================

def process_data_for_template(products):
    """按轮次拆分商品，组装模板所需的数据结构"""
    if not products:
        return {}

    now = get_beijing_time()
    current_round = calculate_round(now)
    round_info = get_round_info()

    active = [p for p in products if p["batch"] == current_round] if current_round else []

    current_products = []
    hot_products = []

    for product in active:
        remaining = (product["end_dt"] - now).total_seconds()
        entry = {
            "name": product["name"],
            "image": product["image"],
            "description": product["description"],
            "price": format_price(product["price"]),
            "buy_limit": product["buy_limit"],
            "countdown": format_countdown(remaining),
        }
        if product["is_all_day"]:
            hot_products.append(entry)
        else:
            current_products.append(entry)

    # 已结束轮次按倒序展示，仅展示限时商品
    history_groups = []
    for batch in sorted((b for b in ROUND_HOURS if b < current_round), reverse=True):
        batch_products = [
            {
                "name": p["name"],
                "image": p["image"],
                "price": format_price(p["price"]),
            }
            for p in products
            if p["batch"] == batch and not p["is_all_day"]
        ]
        if batch_products:
            history_groups.append({
                "time_label": get_round_time_range(batch),
                "status_label": "已结束",
                "products": batch_products[:5],
            })

    return {
        "title": "远行商人",
        "subtitle": "每日 08:00 / 12:00 / 16:00 / 20:00 刷新",
        "product_count": len(current_products) + len(hot_products),
        "round_info": round_info,
        "current_time_range": get_round_time_range(current_round),
        "current_products": current_products,
        "hot_products": hot_products,
        "history_groups": history_groups,
        "_res_path": "",
        "background": "img/bg.C8CUoi7I.jpg",
        "titleIcon": True,
    }


# ================= 5. 图像渲染与上传 =================

async def render_to_image(processed_data):
    """渲染 HTML 模板并截图"""
    if not processed_data or processed_data.get("product_count", 0) == 0:
        print("当前无活跃商品，跳过渲染")
        return None

    now = get_beijing_time()
    current_round = processed_data.get("round_info", {}).get("current", 1)
    screenshot_file = f"{now.strftime('%Y%m%d')}-{now.strftime('%H%M')}-{current_round}.jpg"
    temp_html_path = os.path.join(ASSETS_DIR, TEMP_RENDER_FILE)

    try:
        env = Environment(loader=FileSystemLoader(ASSETS_DIR))
        template = env.get_template(HTML_TEMPLATE_FILE)

        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(template.render(processed_data))

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 900, "height": 1600},
                device_scale_factor=2,
            )
            page = await context.new_page()
            await page.goto(f"file://{temp_html_path}")

            await page.evaluate("document.fonts.ready")
            await page.wait_for_load_state("networkidle")

            # 等待远程图片加载完成，避免截图出现空白图位
            try:
                await page.wait_for_function(
                    """() => {
                        const imgs = Array.from(document.querySelectorAll('img'))
                            .filter(img => img.src.startsWith('http'));
                        if (imgs.length === 0) return true;
                        return imgs.every(img => img.complete && img.naturalWidth > 0);
                    }""",
                    timeout=20000,
                )
            except Exception:
                print("⚠️ 部分图片加载超时，继续截图")

            await page.locator(".merchant-page").screenshot(
                path=screenshot_file, type="jpeg", quality=90
            )
            await browser.close()

        print(f"✅ 图片渲染成功: {screenshot_file}")
        return screenshot_file

    except Exception as e:
        print(f"❌ 渲染图片失败: {e}")
        return None
    finally:
        if os.path.exists(temp_html_path):
            os.remove(temp_html_path)


def upload_to_imgbb(image_path):
    """上传到 ImgBB 图床"""
    if not image_path or not IMGBB_KEY:
        return None
    try:
        with open(image_path, "rb") as f:
            res = requests.post(
                "https://api.imgbb.com/1/upload",
                data={"key": IMGBB_KEY},
                files={"image": f},
                timeout=30,
            )
        data = res.json()
        if data.get("status") == 200:
            print("✅ 图床上传成功")
            return data["data"]["url"]
        print(f"❌ 图床上传失败: {data.get('error', {}).get('message')}")
    except Exception as e:
        print(f"❌ 图床请求异常: {e}")
    return None


# ================= 6. 推送分发 =================

def push_all(title, body, markdown, image_url):
    """执行双通道推送"""
    if NOTIFYME_UUID:
        payload = {
            "data": {
                "uuid": NOTIFYME_UUID,
                "ttl": 86400,
                "priority": "high",
                "data": {
                    "title": title,
                    "body": body,
                    "group": "洛克王国",
                    "bigText": True,
                    "record": 1,
                    "markdown": f"{markdown}\n\n![render]({image_url})" if image_url else markdown,
                },
            }
        }
        try:
            requests.post(NOTIFYME_SERVER, json=payload, timeout=10)
            print("✅ NotifyMe 推送已发送")
        except Exception as e:
            print(f"⚠️ NotifyMe 推送失败: {e}")

    if BARK_KEY:
        try:
            requests.post(
                f"https://bark.wibi8bo.top/{BARK_KEY}",
                data={
                    "title": title,
                    "body": body,
                    "group": "洛克王国",
                    "image": image_url,
                    "isArchive": 1,
                    "ttl": 14400,
                },
                timeout=10,
            )
            print("✅ Bark 推送已发送")
        except Exception as e:
            print(f"⚠️ Bark 推送失败: {e}")


# ================= 7. 主入口 =================

async def main():
    products = fetch_merchant_data()

    if not products:
        push_all("⚠️ 监控异常", "无法获取远行商人数据", "无法获取远行商人数据", None)
        return

    processed = process_data_for_template(products)

    if not processed or processed.get("product_count", 0) == 0:
        print("当前无在售商品，跳过推送")
        return

    names = [p["name"] for p in processed["current_products"] + processed["hot_products"]]
    push_body = f"当前售卖: {'、'.join(names)}" if names else "当前暂无商品"

    local_img = await render_to_image(processed)
    img_url = upload_to_imgbb(local_img)

    push_all("📢 远行商人已刷新", push_body, "### 🛒 商人刷新详情", img_url)


if __name__ == "__main__":
    asyncio.run(main())
