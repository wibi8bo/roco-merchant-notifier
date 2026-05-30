import os
import requests
import asyncio
import re
from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

# ================= 1. 配置区域 =================
ROCOM_API_KEY = os.environ.get("ROCOM_API_KEY")
IMGBB_KEY = os.environ.get("IMGBB_KEY")
NOTIFYME_UUID = os.environ.get("NOTIFYME_UUID")
BARK_KEY = os.environ.get("BARK_KEY")

GAME_API_URL = "https://wegame.shallow.ink/api/v1/games/rocom/merchant/info"
NOTIFYME_SERVER = "https://notifyme-server.wzn556.top/api/send"
ASSETS_DIR = os.path.abspath("assets/yuanxing-shangren")
HTML_TEMPLATE_FILE = "index.html"
TEMP_RENDER_FILE = "temp_render.html"



# ================= 2. 时间与数据处理逻辑 =================

def get_beijing_time():
    """获取精准的北京时间"""
    return datetime.now(timezone(timedelta(hours=8)))

def format_timestamp(ts_ms):
    """格式化时间戳为 HH:mm"""
    if not ts_ms: return "--:--"
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%H:%M")

def format_countdown(seconds):
    """格式化倒计时为 HH:MM:SS"""
    if seconds <= 0:
        return "00:00:00"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def get_round_info():
    """计算当前远行商人的轮次与倒计时"""
    now = get_beijing_time()
    start_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
    
    if now < start_time:
        # 24:00 - 8:00 期间，返回距离次日8:00的倒计时
        next_open = start_time
        remaining = next_open - now
        hours, rem = divmod(int(remaining.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)
        countdown_str = f"{hours}小时{minutes}分钟"
        return {
            "current": 0,
            "total": 4,
            "countdown": countdown_str,
            "is_market_open": False
        }
    
    delta_seconds = int((now - start_time).total_seconds())
    round_index = (delta_seconds // (4 * 3600)) + 1
    
    if round_index > 4:
        return {"current": 4, "total": 4, "countdown": "今日已收市", "is_market_open": False}
    
    round_end = start_time + timedelta(hours=round_index * 4)
    remaining = round_end - now
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes, _ = divmod(rem, 60)
    
    countdown_str = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
    
    return {
        "current": round_index,
        "total": 4,
        "countdown": countdown_str,
        "is_market_open": True
    }

def get_current_round_time_range():
    """获取当前轮次的时间范围"""
    now = get_beijing_time()
    start_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
    
    if now < start_time:
        return "未开始"
    
    delta_seconds = int((now - start_time).total_seconds())
    round_index = (delta_seconds // (4 * 3600)) + 1
    
    if round_index > 4:
        return "已结束"
    
    round_start = start_time + timedelta(hours=(round_index - 1) * 4)
    round_end = start_time + timedelta(hours=round_index * 4)
    
    return f"{round_start.strftime('%H:%M')} - {round_end.strftime('%H:%M')}"

def fetch_wiki_description(item_name):
    """从 wiki 获取商品简介"""
    try:
        wiki_url = f"https://wiki.biligame.com/rocom/{item_name}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://wiki.biligame.com/"
        }
        resp = requests.get(wiki_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            # 匹配 Markdown 格式的简介 **内容**（非贪婪匹配）
            match = re.search(r'\*\*(.+?)\*\*', resp.text)
            if match:
                return match.group(1)
    except Exception:
        pass
    return ""

def fetch_descriptions(item_names):
    """批量获取商品简介"""
    descriptions = {}
    for name in item_names:
        desc = fetch_wiki_description(name)
        if desc:
            descriptions[name] = desc
        else:
            descriptions[name] = ""
    return descriptions

def process_data_for_template(data):
    if not data: return {}
    
    now_ms = int(get_beijing_time().timestamp() * 1000)
    round_info = get_round_info()
    
    activities = data.get("merchantActivities") or data.get("merchant_activities") or []
    activity = activities[0] if activities else {}
    
    # 获取三种类型的商品
    buckets = [
        ("道具", activity.get("get_props") or []),
        ("额外道具", activity.get("get_extra_props") or []),
        ("精灵", activity.get("get_pets") or []),
    ]

    # 匹配商品元数据字典 (用于获取价格和限购次数)
    random_goods = data.get("random_goods") if isinstance(data.get("random_goods"), list) else []
    goods_meta_by_name = {
        str(item.get("goods_name", "") or item.get("name", "")).strip(): item
        for item in random_goods
        if isinstance(item, dict) and str(item.get("goods_name", "") or item.get("name", "")).strip()
    }

    all_products = []
    active_products = []
    
    for category, items in buckets:
        for item in items:
            if not isinstance(item, dict): continue

            goods_meta = goods_meta_by_name.get(str(item.get("name", "")).strip(), {})
            
            s_time = item.get("start_time")
            e_time = item.get("end_time")

            # 兜底继承大活动时间
            if s_time is None: s_time = activity.get("start_time")
            if e_time is None: e_time = activity.get("end_time")

            start_ms = int(s_time) if s_time else None
            end_ms = int(e_time) if e_time else None

            is_active = True
            if start_ms is not None and end_ms is not None:
                is_active = start_ms <= now_ms < end_ms

            status_label = "当前轮次"
            if start_ms is not None and now_ms < start_ms:
                status_label = "未开始"
            elif end_ms is not None and now_ms >= end_ms:
                status_label = "已结束"

            # 时间标签格式化
            start_str = format_timestamp(start_ms)
            end_str = format_timestamp(end_ms)
            if start_str[:5] == end_str[:5] and start_str != "--:--":
                time_label = f"{start_str} - {end_str[6:]}" if len(end_str) > 6 else f"{start_str} - {end_str}"
            else:
                time_label = f"{start_str} - {end_str}"

            product = {
                "name": item.get("name", "未知商品"),
                "image": item.get("icon_url", ""),
                "time_label": time_label,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "is_active": is_active,
                "status_label": status_label,
                "price": item.get("price") if item.get("price") not in (None, "") else goods_meta.get("price"),
                "buy_limit_num": item.get("buy_limit_num") if item.get("buy_limit_num") not in (None, "") else goods_meta.get("buy_limit_num")
            }
            
            all_products.append(product)
            if is_active:
                active_products.append(product)
                
    # 历史记录分组逻辑
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    grouped = {}
    
    for product in all_products:
        if product["is_active"]: continue
        start_ms = product["start_ms"]
        if not start_ms: continue
        
        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone(timedelta(hours=8)))
        if start_dt.strftime("%Y-%m-%d") != today: continue

        key = f"{start_ms}-{product['end_ms'] or ''}"
        if key not in grouped:
            grouped[key] = {
                "time_label": product["time_label"] or "--:--",
                "status_label": product["status_label"] or "其他时段",
                "sort": start_ms,
                "products": []
            }
        group = grouped[key]
        names = {p["name"] for p in group["products"]}
        # 每段最多展示5个不重复商品
        if product["name"] not in names and len(group["products"]) < 5:
            group["products"].append(product)

    history_groups = [
        {k: v for k, v in g.items() if k != "sort"}
        for g in sorted(grouped.values(), key=lambda x: x["sort"])
        if g["products"]
    ]

    # 获取当前轮次时间范围
    current_time_range = get_current_round_time_range()

    # 分离热销商品和当前轮次商品
    current_round_products = []
    hot_products = []

    # 获取所有活跃商品的名称
    active_names = {p["name"] for p in active_products}

    # 预先获取所有商品的简介（避免重复请求）
    all_item_names = list(set([p["name"] for p in active_products]))
    descriptions = fetch_descriptions(all_item_names) if all_item_names else {}

    for product in active_products:
        product["description"] = descriptions.get(product["name"], "")

        # 根据售卖时长判断：≤4小时为轮次商品，>4小时为热销商品
        duration_ms = (product["end_ms"] or 0) - (product["start_ms"] or 0)
        duration_hours = duration_ms / (1000 * 60 * 60)

        if duration_hours > 4:
            # 热销商品：售卖时长超过4小时
            if product["end_ms"]:
                remaining_seconds = max(0, (product["end_ms"] - now_ms) // 1000)
                product["countdown"] = format_countdown(remaining_seconds)
            else:
                product["countdown"] = "00:00:00"
            hot_products.append(product)
        else:
            # 当前轮次商品
            current_round_products.append(product)

    # 更新 product_count 为总商品数（当前轮次 + 热销）
    total_product_count = len(current_round_products) + len(hot_products)

    return {
        "title": activity.get("name", "远行商人"),
        "subtitle": activity.get("start_date", "每日 08:00 / 12:00 / 16:00 / 20:00 刷新"),
        "product_count": total_product_count,
        "round_info": round_info,
        "current_time_range": current_time_range,
        "current_products": current_round_products,
        "hot_products": hot_products,
        "products": active_products,
        "history_groups": history_groups,

        # 本地资源支持变量
        "_res_path": "",
        "background": "img/bg.C8CUoi7I.jpg",
        "titleIcon": True
    }

# ================= 3. 图像渲染与上传 =================

async def render_to_image(processed_data):
    """渲染 HTML 并精准切割截图"""
    if not processed_data or processed_data["product_count"] == 0:
        print("当前无活跃商品，跳过渲染")
        return None
    
    screenshot_file = "merchant_render.jpg"
    temp_html_path = os.path.join(ASSETS_DIR, TEMP_RENDER_FILE)
    
    try:
        env = Environment(loader=FileSystemLoader(ASSETS_DIR))
        template = env.get_template(HTML_TEMPLATE_FILE)
        rendered_html = template.render(processed_data)
        
        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(rendered_html)
            
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 900, "height": 1600},
                device_scale_factor=2
            )
            page = await context.new_page()
            await page.goto(f"file://{temp_html_path}")
            
            # 等待所有图文加载完毕
            await page.evaluate("document.fonts.ready")
            await page.wait_for_load_state("networkidle")
            
            data_region = page.locator('.merchant-page')
            await data_region.screenshot(path=screenshot_file, type="jpeg", quality=90)
            
            await browser.close()
            print(f"✅ 图片渲染成功: {screenshot_file}")
            return screenshot_file
            
    except Exception as e:
        print(f"❌ 渲染图片失败: {e}")
        return None
    finally:
        if os.path.exists(temp_html_path): os.remove(temp_html_path)

async def upload_to_imgbb(image_path):
    """上传到 ImgBB 图床"""
    if not image_path or not IMGBB_KEY: return None
    try:
        with open(image_path, "rb") as f:
            res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_KEY}, files={"image": f}, timeout=30)
            json_data = res.json()
            if json_data.get("status") == 200:
                print("✅ 图床上传成功")
                return json_data["data"]["url"]
            else:
                print(f"❌ 图床上传失败: {json_data.get('error', {}).get('message')}")
                return None
    except Exception as e:
        print(f"❌ 图床请求异常: {e}")
        return None

# ================= 4. 推送分发 =================

def push_all(title, body, markdown, image_url):
    """执行双通道推送"""
    if NOTIFYME_UUID:
        payload = {
            "data": {
                "uuid": NOTIFYME_UUID, "ttl": 86400, "priority": "high",
                "data": {
                    "title": title, "body": body, "group": "洛克王国", "bigText": True, "record": 1,
                    "markdown": f"{markdown}\n\n![render]({image_url})" if image_url else markdown
                }
            }
        }
        try:
            requests.post(NOTIFYME_SERVER, json=payload, timeout=10)
            print("✅ NotifyMe 推送已发送")
        except: pass
    
    if BARK_KEY:
        try:
            requests.post(f"https://bark.wibi8bo.top/{BARK_KEY}", data={
                "title": title, "body": body, "group": "洛克王国", "image": image_url, "isArchive": 1, "ttl": 14400
            }, timeout=10)
            print("✅ Bark 推送已发送")
        except: pass

# ================= 5. 主入口 =================

async def main():
    try:
        resp = requests.get(GAME_API_URL, headers={"X-API-Key": ROCOM_API_KEY}, timeout=30)
        resp.raise_for_status()
        raw_data = resp.json().get("data", {})
        err = None if resp.json().get("code") == 0 else resp.json().get("message")
    except Exception as e:
        raw_data, err = None, f"请求异常: {e}"
    
    if err or not raw_data:
        push_all("⚠️ 监控异常", err or "无法获取数据", "无法获取数据", None)
        return

    processed = process_data_for_template(raw_data)
    item_names = [p["name"] for p in processed["products"]]
    push_body = f"当前售卖: {'、'.join(item_names)}" if item_names else "当前暂无商品"
    
    local_img = await render_to_image(processed)
    img_url = await upload_to_imgbb(local_img)
    
    push_all("📢 远行商人已刷新", push_body, "### 🛒 商人刷新详情", img_url)

if __name__ == "__main__":
    asyncio.run(main())
