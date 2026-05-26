#!/usr/bin/env python3
"""
自选股监控报告：获取用户自选股行情 + K线趋势分析
通过 DeepSeek V4 生成分析报告并推送到飞书
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/10c2a131-2efe-4fba-a84e-4952c5412281")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-v4-pro"

# 自选股配置
WATCHLIST = [
    {"symbol": "07709.HK", "name": "南方两倍做多海力士", "market": "HK"},
    {"symbol": "TSLL.US",   "name": "两倍做多特斯拉(TSLL)", "market": "US"},
    {"symbol": "NOK.US",    "name": "诺基亚(NOK)", "market": "US"},
]


def fetch_longbridge_quotes() -> list:
    """从长桥获取自选股实时行情"""
    app_key = os.environ.get("LONGBRIDGE_APP_KEY", "")
    app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", "")
    access_token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", "")

    if not all([app_key, app_secret, access_token]):
        return []

    try:
        from longbridge.openapi import QuoteContext, Config
    except ImportError:
        return []

    try:
        config = Config(app_key, app_secret, access_token)
        ctx = QuoteContext(config)

        symbols = [s["symbol"] for s in WATCHLIST]
        quotes = ctx.get_quote(symbols)

        results = []
        for q in quotes:
            info = {
                "symbol": q.symbol,
                "price": q.last_done,
                "change": q.change_val,
                "change_pct": (q.change_rate * 100) if q.change_rate is not None else None,
                "open": q.open,
                "high": q.high,
                "low": q.low,
                "prev_close": q.prev_close,
                "volume": q.volume,
                "turnover": q.turnover,
            }
            results.append(info)
        return results
    except Exception as e:
        print(f"[长桥] 行情获取异常: {e}", file=sys.stderr)
        return []


def fetch_longbridge_candlesticks(symbol: str, count: int = 30) -> list:
    """从长桥获取K线数据"""
    app_key = os.environ.get("LONGBRIDGE_APP_KEY", "")
    app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", "")
    access_token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", "")

    if not all([app_key, app_secret, access_token]):
        return []

    try:
        from longbridge.openapi import QuoteContext, Config, CandlestickPeriod
    except ImportError:
        return []

    try:
        config = Config(app_key, app_secret, access_token)
        ctx = QuoteContext(config)

        candles = ctx.get_candlesticks(symbol, CandlestickPeriod.Day_1, count)
        results = []
        for c in candles:
            results.append({
                "date": str(c.timestamp.date()),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            })
        return results
    except Exception as e:
        print(f"[长桥] K线获取异常 {symbol}: {e}", file=sys.stderr)
        return []


def calc_trend(candles: list) -> dict:
    """计算趋势指标"""
    if not candles or len(candles) < 2:
        return {}

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    current = closes[-1]
    prev_close = closes[-2]

    # 涨跌幅
    daily_change = (current - prev_close) / prev_close * 100

    # 均线
    ma5 = sum(closes[-5:]) / min(5, len(closes)) if len(closes) >= 5 else None
    ma10 = sum(closes[-10:]) / min(10, len(closes)) if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / min(20, len(closes)) if len(closes) >= 20 else None
    ma30 = sum(closes[-30:]) / min(30, len(closes)) if len(closes) >= 30 else None

    # 最高/最低
    high_30 = max(closes)
    low_30 = min(closes)
    high_pos = closes.index(high_30)
    low_pos = closes.index(low_30)

    # 成交量变化
    avg_vol_5 = sum(volumes[-5:]) / min(5, len(volumes)) if len(volumes) >= 5 else None
    avg_vol_20 = sum(volumes[-20:]) / min(20, len(volumes)) if len(volumes) >= 20 else None
    vol_ratio = (avg_vol_5 / avg_vol_20) if (avg_vol_5 and avg_vol_20 and avg_vol_20 > 0) else None

    # 趋势判断
    trend = "横盘"
    if len(closes) >= 5:
        recent_5 = [closes[-i] / closes[-i-1] - 1 for i in range(1, 5)]
        avg_recent = sum(recent_5) / len(recent_5)
        if avg_recent > 0.01:
            trend = "上升"
        elif avg_recent < -0.01:
            trend = "下跌"

    # 均线排列
    ma_alignment = "未知"
    if all(v is not None for v in [ma5, ma10, ma20]):
        if ma5 > ma10 > ma20:
            ma_alignment = "多头排列（均线向上发散）"
        elif ma5 < ma10 < ma20:
            ma_alignment = "空头排列（均线向下发散）"
        else:
            ma_alignment = "均线交叉缠绕"

    # 相对位置
    position = "未知"
    range_30 = high_30 - low_30
    if range_30 > 0:
        pct_from_low = (current - low_30) / range_30 * 100
        if pct_from_low > 80:
            position = "近30日高位"
        elif pct_from_low > 50:
            position = "近30日中部偏上"
        elif pct_from_low > 20:
            position = "近30日中部偏下"
        else:
            position = "近30日低位"

    return {
        "daily_change_pct": round(daily_change, 2),
        "current_price": current,
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma30": round(ma30, 2) if ma30 else None,
        "high_30": high_30,
        "low_30": low_30,
        "high_30_days_ago": len(closes) - high_pos - 1,
        "low_30_days_ago": len(closes) - low_pos - 1,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "trend": trend,
        "ma_alignment": ma_alignment,
        "position": position,
    }


def call_deepseek(prompt: str) -> str:
    """调用 DeepSeek API"""
    if not DEEPSEEK_API_KEY:
        return "【错误】未设置 DEEPSEEK_API_KEY"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"【错误】DeepSeek 调用失败: {str(e)}"


def build_report_data() -> str:
    """获取并格式化所有自选股数据"""
    today = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 自选股监控报告 — {today}", ""]

    # 获取行情
    quotes = fetch_longbridge_quotes()
    if not quotes:
        return "长桥数据获取失败，请检查配置。"

    # 对每只股票获取K线并分析
    for stock in WATCHLIST:
        symbol = stock["symbol"]
        name = stock["name"]
        lines.append(f"【{name} ({symbol})】")

        # 行情
        q = next((q for q in quotes if q["symbol"] == symbol), None)
        if q:
            change_str = f"{q['change_pct']:+.2f}%" if q['change_pct'] is not None else "-"
            lines.append(f"  最新: {q['price']}  |  涨跌幅: {change_str}")
            lines.append(f"  今开: {q['open']}  最高: {q['high']}  最低: {q['low']}")
            lines.append(f"  昨收: {q['prev_close']}")
            if q['volume']:
                lines.append(f"  成交量: {q['volume']}")
        else:
            lines.append(f"  行情数据获取失败")

        # K线趋势
        candles = fetch_longbridge_candlesticks(symbol, 30)
        if len(candles) >= 2:
            trend = calc_trend(candles)
            lines.append(f"  【趋势分析】")
            lines.append(f"  近5日趋势: {trend.get('trend', '未知')}")
            lines.append(f"  今日涨跌: {trend.get('daily_change_pct', 'N/A')}%")
            lines.append(f"  MA5: {trend.get('ma5', 'N/A')}  MA10: {trend.get('ma10', 'N/A')}  MA20: {trend.get('ma20', 'N/A')}")
            lines.append(f"  均线排列: {trend.get('ma_alignment', '未知')}")
            lines.append(f"  30日最高: {trend.get('high_30')}  ({trend.get('high_30_days_ago')}天前)")
            lines.append(f"  30日最低: {trend.get('low_30')}  ({trend.get('low_30_days_ago')}天前)")
            lines.append(f"  当前位置: {trend.get('position', '未知')}")
            if trend.get('vol_ratio'):
                lines.append(f"  成交量比(5日/20日): {trend['vol_ratio']}")

            # 最近5日收盘价
            recent = ", ".join([str(c["close"]) for c in candles[-5:]])
            lines.append(f"  近5日收盘: {recent}")
        else:
            lines.append(f"  趋势数据不足")

        lines.append("")

    return "\n".join(lines)


def generate_analysis(data: str) -> str:
    """用 DeepSeek 生成分析文章"""
    prompt = f"""你是一个专业的股票分析师。基于以下自选股数据，写一篇分析报告和60秒口播稿。

【自选股数据】
{data}

要求：
1. **分析报告**（约400字）：对每只股票进行行情回顾和技术面分析，包括趋势判断、均线形态、量价关系分析、支撑/阻力位判断
2. **60秒口播稿**（约200-250字）：口语化、适合朗读，开头用"各位投资者朋友大家好"
3. 在文章末尾给出**综合建议**和**操作策略**
4. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出内容，不要额外解释。"""

    return call_deepseek(prompt)


def send_to_feishu(content: str, title: str):
    """发送到飞书"""
    article = ""
    broadcast = ""
    if "【文章】" in content and "【口播】" in content:
        parts = content.split("【口播】", 1)
        article_part = parts[0]
        broadcast = "【口播】" + parts[1]
        article = article_part.replace("【文章】", "").strip()
    else:
        article = content

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title}
            },
            "elements": []
        }
    }

    elements = [{"tag": "markdown", "content": article}]
    if broadcast:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": f"**🎙 60秒口播稿**\n\n{broadcast}"})
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text", "content": f"自动生成 · {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}"}
        ]
    })
    card["card"]["elements"] = elements

    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(card, ensure_ascii=False),
             FEISHU_WEBHOOK],
            capture_output=True, text=True, timeout=15
        )
        return result.stdout
    except Exception as e:
        return f"发送失败: {str(e)}"


def main():
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 获取自选股数据...")
    data = build_report_data()
    if data.startswith("长桥数据获取失败"):
        print("❌", data)
        sys.exit(1)
    print("✅ 数据获取完成")

    print("🤖 调用 DeepSeek 生成分析...")
    content = generate_analysis(data)
    print("✅ 内容生成完成")

    print("📤 发送到飞书...")
    result = send_to_feishu(content, "📊 自选股监控 · 收盘分析")
    print(f"📬 飞书响应: {result[:200]}")

    # 也打印一份到日志
    print("\n" + "="*40)
    print(data)
    print("="*40)
    print(content)


if __name__ == "__main__":
    main()
