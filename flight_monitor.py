#!/usr/bin/env python3
"""
机票价格监控：西安→澳洲（悉尼/墨尔本/布里斯班/珀斯）
通过 Playwright 爬取 Trip.com 航班比价，低于历史均价时飞书推送提醒

策略：
  1. 每条航线用 Playwright 加载一次 Trip.com 搜索页
  2. 从 DOM 提取航班价格（一个月内多个日期）
  3. 记录最低价与均价，与历史均价对比
  4. 低于历史均价 5% 以上时触发飞书告警
  5. 每次运行后更新 data/flight_history.json

数据源说明：
  - Trip.com 国际版（GitHub Actions US 服务器可正常访问）
  - 在中国本地无法测试（反爬检测），通过 CI 日志调试
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
# Trip.com 爬取（Playwright）
# -----------------------------------------------

def _make_browser(playwright):
    """启动一个反检测优化的 Chromium 实例"""
    return playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )


def _make_context(browser):
    """创建带有合理用户特征的浏览器上下文"""
    return browser.new_context(
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    )


def scrape_route(from_code: str, to_code: str, date_from: str) -> list:
    """
    爬取一条航线未来 N 天的价格。
    返回 [2800, 3200, ...]（不含日期的价格集合）
    通过 setTimeout 等待数据加载 + 页面文本提取。
    """
    url = f"https://flights.trip.com/international/search/{from_code}-{to_code}?departDate={date_from}"

    import playwright.sync_api

    try:
        with playwright.sync_api.sync_playwright() as pw:
            browser = _make_browser(pw)
            ctx = _make_context(browser)
            page = ctx.new_page()

            # 尝试禁用 webdriver 检测
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """)

            print(f"    → {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 多次等待，给动态加载的数据足够时间
            for i in range(3):
                try:
                    page.wait_for_selector(
                        "[class*=flight],[class*=price],[class*=result],[data-bind*=flight]",
                        timeout=15000,
                    )
                    break
                except Exception:
                    page.wait_for_timeout(5000)

            # 额外等待确保 API 数据到达
            page.wait_for_timeout(8000)

            html = page.content()
            browser.close()

        # ----- 价格提取策略 -----

        # 策略 A: 从 <script> 中寻找 JSON 格式的价格数据
        prices = set()
        for m in re.finditer(r'"price"\s*:\s*(\d+)', html):
            p = int(m.group(1))
            if 500 < p < 30000:
                prices.add(p)

        # 策略 B: DOM 中 ¥ 前缀的价格
        if not prices:
            for m in re.finditer(r'[¥￥]\s*(\d[\d,]*\d)', html):
                p = int(m.group(1).replace(",", ""))
                if 500 < p < 30000:
                    prices.add(p)

        # 策略 C: 寻找 totalPrice / salePrice 等常见字段
        if not prices:
            for field in ["totalPrice", "salePrice", "adultPrice", "minPrice"]:
                for m in re.finditer(rf'"{field}"\s*:\s*(\d+)', html):
                    p = int(m.group(1))
                    if 500 < p < 30000:
                        prices.add(p)

        result = sorted(prices)
        if result:
            print(f"      提取到 {len(result)} 个价格区间: ¥{result[0]} ~ ¥{result[-1]}")
        else:
            print(f"      未提取到价格数据（页面 {len(html)} bytes）")

        return result

    except Exception as e:
        print(f"    [Error] {from_code}→{to_code}: {e}", file=sys.stderr)
        return []


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
            "header": {
                "title": {"tag": "plain_text", "content": title}
            },
            "elements": [
                {"tag": "markdown", "content": content},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"自动生成 · {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}"}
                    ]
                },
            ]
        }
    }
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(card, ensure_ascii=False),
             FEISHU_WEBHOOK],
            capture_output=True, text=True, timeout=15
        )
        print(f"  [飞书] 响应: {result.stdout[:100]}")
    except Exception as e:
        print(f"  [飞书] 发送异常: {e}", file=sys.stderr)


# -----------------------------------------------
# 主逻辑
# -----------------------------------------------

def run_monitor() -> str:
    today = datetime.now(CST)
    today_str = today.strftime("%Y-%m-%d")
    date_from = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    lines = [f"✈️ 西安→澳洲 机票价格监控报告", f"生成时间: {today.strftime('%Y-%m-%d %H:%M')}", ""]
    history = load_history()
    alerts = []

    for route in ROUTES:
        route_key = f"{route['from']}-{route['to']}"
        route_name = f"{route['from_name']}→{route['to_name']}"
        print(f"\n[{route_name}] 爬取中...")

        prices = scrape_route(route["from"], route["to"], date_from)

        lines.append(f"【{route_name}】")
        if prices:
            min_p = min(prices)
            avg_p = sum(prices) / len(prices)
            lines.append(f"  最低价: ¥{min_p}")
            lines.append(f"  均价: ¥{avg_p:.0f}")
            lines.append(f"  采样量: {len(prices)} 个价格点")

            # 与历史均价对比
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

            # 更新历史记录
            hist_entry = history.setdefault("routes", {}).setdefault(route_key, {"prices": []})
            hist_entry.setdefault("prices", []).append({
                "date": today_str,
                "avg": round(avg_p, 0),
                "min": min_p,
            })
            hist_entry["prices"] = hist_entry["prices"][-30:]
            hist_entry["avg_price"] = round(
                sum(p["avg"] for p in hist_entry["prices"]) / len(hist_entry["prices"]), 0
            )

            lines.append(f"  低价TOP5: ¥{'  ¥'.join(str(p) for p in prices[:5])}")
        else:
            lines.append(f"  暂无数据（可能反爬限制，后续自动恢复）")
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

    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 开始机票价格监控...")
    report = run_monitor()
    print("\n" + "=" * 40)
    print(report)

    # 每日例行推送（无论有无低价）
    send_to_feishu(report, "✈️ 西安→澳洲 机票价格日报")
    print("✅ 完成")


if __name__ == "__main__":
    main()
