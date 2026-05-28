#!/usr/bin/env python3
"""
机票价格监控：西安→澳洲（悉尼/墨尔本/布里斯班/珀斯）
Playwright 多源爬取 + 历史均价对比 + 飞书推送

数据源（按优先级）：
  1. Google Flights — 结构化好、反爬松，优先使用
  2. Trip.com — 备选
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
HISTORY_FILE = Path(__file__).parent / "data" / "flight_history.json"

ROUTES = [
    {"from": "XIY", "from_name": "西安", "to": "SYD", "to_name": "悉尼"},
    {"from": "XIY", "from_name": "西安", "to": "MEL", "to_name": "墨尔本"},
    {"from": "XIY", "from_name": "西安", "to": "BNE", "to_name": "布里斯班"},
    {"from": "XIY", "from_name": "西安", "to": "PER", "to_name": "珀斯"},
]


# 机场 IATA → Google Flights 城市名映射
GOOGLE_CITY = {
    "XIY": "Xi%27an", "SYD": "Sydney", "MEL": "Melbourne",
    "BNE": "Brisbane", "PER": "Perth",
}


# -----------------------------------------------
# 数据持久化
# -----------------------------------------------

def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"routes": {}, "last_updated": None}


def save_history(data: dict):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# -----------------------------------------------
# Playwright 工具
# -----------------------------------------------

def _launch_playwright():
    import playwright.sync_api
    pw = playwright.sync_api.sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu",
              "--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        locale="zh-CN", timezone_id="Asia/Shanghai",
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    page = ctx.new_page()
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    """)
    return pw, browser, page


def _extract_prices(html: str, min_p=300, max_p=50000) -> list:
    """从 HTML 中提取合理范围的价格数字"""
    prices = set()
    for m in re.finditer(r'"price"\s*:\s*(\d+)', html):
        p = int(m.group(1))
        if min_p < p < max_p:
            prices.add(p)
    for field in ["totalPrice", "salePrice", "adultPrice", "minPrice",
                   "priceAmount", "amount", "farePrice"]:
        for m in re.finditer(rf'"{field}"\s*:\s*(\d+)', html):
            p = int(m.group(1))
            if min_p < p < max_p:
                prices.add(p)
    for m in re.finditer(r'[¥￥]\s*(\d[\d,]*\d)', html):
        p = int(m.group(1).replace(",", ""))
        if min_p < p < max_p:
            prices.add(p)
    # Google Flights 特有: 数据在 window.INITIAL_STATE 或 script#__NEXT_DATA__
    for m in re.finditer(r'"priceValue"\s*:\s*(\d+)', html):
        p = int(m.group(1))
        if min_p < p < max_p:
            prices.add(p)
    for m in re.finditer(r'"totalPrice"\s*:\s*{.*?"amount"\s*:\s*(\d+)', html):
        p = int(m.group(1))
        if min_p < p < max_p:
            prices.add(p)
    return sorted(prices)


# -----------------------------------------------
# 源1：Google Flights
# -----------------------------------------------

def scrape_google_flights(from_code: str, to_code: str, date_from: str) -> list:
    """通过 Google Flights 获取航班价格"""
    city_from = GOOGLE_CITY.get(from_code, from_code)
    city_to = GOOGLE_CITY.get(to_code, to_code)

    # 搜索最近 7 天（Google Flights 日历视图会显示多日价格）
    url = (
        f"https://www.google.com/travel/flights?"
        f"q=Flights+to+{city_to}+from+{city_from}+on+{date_from}"
        f"&curr=CNY&hl=zh-CN"
    )

    try:
        import playwright.sync_api
        pw, browser, page = _launch_playwright()
        print(f"    [GF] → {url[:100]}...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # 等结果渲染
        for _ in range(5):
            try:
                page.wait_for_selector("[class*=price i],[class*=flight i],[role=listbox]",
                                       timeout=10000)
                break
            except Exception:
                page.wait_for_timeout(3000)
        page.wait_for_timeout(5000)

        html = page.content()
        browser.close()
        pw.stop()

        prices = _extract_prices(html)
        if prices:
            print(f"    [GF] 提取到 {len(prices)} 个价格: ¥{prices[0]}~¥{prices[-1]}")
        else:
            print(f"    [GF] 未提取到价格（页面 {len(html)} bytes）")
        return prices

    except Exception as e:
        print(f"    [GF Error] {e}", file=sys.stderr)
        return []


# -----------------------------------------------
# 源2：Trip.com
# -----------------------------------------------

def scrape_tripcom(from_code: str, to_code: str, date_from: str) -> list:
    """通过 Trip.com 获取航班价格"""
    url = f"https://flights.trip.com/international/search/{from_code}-{to_code}?departDate={date_from}"

    try:
        import playwright.sync_api
        pw, browser, page = _launch_playwright()
        print(f"    [Trip] → {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        for _ in range(3):
            try:
                page.wait_for_selector("[class*=flight],[class*=price]",
                                       timeout=15000)
                break
            except Exception:
                page.wait_for_timeout(5000)
        page.wait_for_timeout(8000)

        html = page.content()
        browser.close()
        pw.stop()

        prices = _extract_prices(html)
        if prices:
            print(f"    [Trip] 提取到 {len(prices)} 个价格: ¥{prices[0]}~¥{prices[-1]}")
        else:
            print(f"    [Trip] 未提取到价格（页面 {len(html)} bytes）")
        return prices

    except Exception as e:
        print(f"    [Trip Error] {e}", file=sys.stderr)
        return []


# -----------------------------------------------
# 价格抓取（多源 + 降级）
# -----------------------------------------------

def scrape_prices(from_code: str, to_code: str, date_from: str) -> list:
    """综合多源抓取，返回合并后的价格列表"""
    # 先试 Google Flights
    prices = scrape_google_flights(from_code, to_code, date_from)
    if prices:
        return prices

    # 降级到 Trip.com
    prices = scrape_tripcom(from_code, to_code, date_from)
    return prices


# -----------------------------------------------
# 飞书推送
# -----------------------------------------------

def send_to_feishu(content: str, title: str):
    if not FEISHU_WEBHOOK:
        print("  [飞书] 未设置 FEISHU_WEBHOOK", file=sys.stderr)
        return

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "markdown", "content": content},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": f"自动生成 · {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}"}
                ]},
            ],
        },
    }
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(card, ensure_ascii=False),
             FEISHU_WEBHOOK],
            capture_output=True, text=True, timeout=15,
        )
        print(f"  [飞书] 响应: {result.stdout[:100]}")
    except Exception as e:
        print(f"  [飞书] 异常: {e}", file=sys.stderr)


# -----------------------------------------------
# 主逻辑
# -----------------------------------------------

def run_monitor() -> str:
    today = datetime.now(CST)
    today_str = today.strftime("%Y-%m-%d")
    date_from = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    lines = [f"✈️ 西安→澳洲 机票价格监控报告",
             f"生成时间: {today.strftime('%Y-%m-%d %H:%M')}", ""]
    history = load_history()
    alerts = []

    for route in ROUTES:
        route_key = f"{route['from']}-{route['to']}"
        route_name = f"{route['from_name']}→{route['to_name']}"
        print(f"\n[{route_name}]")

        prices = scrape_prices(route["from"], route["to"], date_from)

        lines.append(f"【{route_name}】")
        if prices:
            min_p = min(prices)
            avg_p = sum(prices) / len(prices)
            lines.append(f"  最低价: ¥{min_p}")
            lines.append(f"  均价: ¥{avg_p:.0f}")
            lines.append(f"  采样: {len(prices)} 个")

            hist = history.get("routes", {}).get(route_key, {})
            hist_avg = hist.get("avg_price")
            if hist_avg:
                diff = (avg_p - hist_avg) / hist_avg * 100
                lines.append(f"  历史均价: ¥{hist_avg:.0f}  |  偏差: {diff:+.1f}%")
                if avg_p < hist_avg * 0.95:
                    alerts.append({
                        "route": route_name,
                        "current_avg": round(avg_p, 0),
                        "hist_avg": hist_avg,
                        "min_price": min_p,
                    })
            else:
                lines.append(f"  历史均价: 暂无（首次采集）")

            # 更新历史
            entry = history.setdefault("routes", {}).setdefault(route_key, {"prices": []})
            entry.setdefault("prices", []).append({
                "date": today_str, "avg": round(avg_p, 0), "min": min_p,
            })
            entry["prices"] = entry["prices"][-30:]
            entry["avg_price"] = round(
                sum(p["avg"] for p in entry["prices"]) / len(entry["prices"]), 0
            )

            lines.append(f"  低价TOP5: ¥{'  ¥'.join(str(p) for p in prices[:5])}")
        else:
            lines.append(f"  ⚠️ 暂未获取到价格（反爬或数据不足）")
        lines.append("")

    history["last_updated"] = today_str
    save_history(history)
    report = "\n".join(lines)

    # 低价告警
    if alerts:
        alert_lines = ["**🎯 发现低价机票**\n"]
        for a in alerts:
            ratio = (1 - a["current_avg"] / a["hist_avg"]) * 100
            alert_lines.append(f"**{a['route']}**")
            alert_lines.append(f"- 当前均价: ¥{a['current_avg']:.0f}")
            alert_lines.append(f"- 历史均价: ¥{a['hist_avg']:.0f}")
            alert_lines.append(f"- 最低: ¥{a['min_price']}")
            alert_lines.append(f"- 低于历史 {ratio:.1f}%")
        send_to_feishu("\n".join(alert_lines), "🎯 机票低价提醒")

    return report


def main():
    if not FEISHU_WEBHOOK:
        print("⚠ 未设置 FEISHU_WEBHOOK，推送将跳过")

    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 开始...")
    try:
        report = run_monitor()
    except Exception as e:
        report = f"⚠️ 脚本异常: {e}"
        print(report, file=sys.stderr)

    print("\n" + "=" * 40)
    print(report)

    # 始终推送（即使出错）
    send_to_feishu(report, "✈️ 西安→澳洲 机票价格日报")
    print("✅ 完成")


if __name__ == "__main__":
    main()
