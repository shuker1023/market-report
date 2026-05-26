#!/usr/bin/env python3
"""
市场数据采集 + DeepSeek V4 生成 + 飞书推送
支持 GitHub Actions 环境
"""

import subprocess
import json
import os
import sys
import re
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# 从环境变量读取（GitHub Actions 通过 Secrets 注入）
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/10c2a131-2efe-4fba-a84e-4952c5412281")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-v4-pro"

# 各市场指数
A_SHARE_INDICES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sz399107": "科创50",
}

HK_INDICES = {
    "hkHSI": "恒生指数",
    "hkHSTECH": "恒生科技指数",
}

US_INDICES = {
    "gb_dji": "道琼斯指数",
    "gb_ixic": "纳斯达克指数",
    "gb_inx": "标普500指数",
}

# 热门个股
A_SHARE_HOT = {
    "sh600036": "招商银行",
    "sh600519": "贵州茅台",
    "sh601318": "中国平安",
    "sz300750": "宁德时代",
    "sz000858": "五粮液",
}

HK_HOT = {
    "hk00700": "腾讯控股",
    "hk09988": "阿里巴巴",
    "hk09961": "哔哩哔哩",
    "hk01810": "小米集团",
    "hk03690": "美团",
}

US_HOT = {
    "gb_aapl": "苹果(AAPL)",
    "gb_msft": "微软(MSFT)",
    "gb_nvda": "英伟达(NVDA)",
    "gb_tsla": "特斯拉(TSLA)",
    "gb_amzn": "亚马逊(AMZN)",
}


def fetch_sina(codes: dict) -> dict:
    """从新浪财经获取实时行情"""
    symbols = ",".join(codes.keys())
    cmd = [
        "curl", "-s",
        "-H", "Referer: https://finance.sina.com.cn",
        f"https://hq.sinajs.cn/list={symbols}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        raw = result.stdout
    except Exception as e:
        return {k: {"error": str(e), "name": codes[k], "code": k} for k in codes}

    try:
        raw = raw.encode("latin1").decode("gbk")
    except Exception:
        pass

    data = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or not line.startswith("var hq_str_"):
            continue
        m = re.search(r'"([^"]*)"', line)
        if not m:
            continue
        fields = m.group(1).split(",")
        name = fields[0] if fields else ""
        code_match = re.search(r"hq_str_(\w+)=", line)
        code = code_match.group(1) if code_match else ""

        if code in codes:
            parsed = {"name": codes[code], "code": code}
            if code.startswith("sh") or code.startswith("sz"):
                if len(fields) >= 6:
                    parsed["open"] = fields[1]
                    parsed["prev_close"] = fields[2]
                    parsed["price"] = fields[3]
                    parsed["high"] = fields[4]
                    parsed["low"] = fields[5]
                    if fields[1] and fields[2]:
                        try:
                            p = float(fields[3])
                            pc = float(fields[2])
                            parsed["change_pct"] = round((p - pc) / pc * 100, 2)
                        except (ValueError, IndexError):
                            pass
                if len(fields) >= 32:
                    parsed["date"] = fields[30]
                    parsed["time"] = fields[31]
            elif code.startswith("hk"):
                if len(fields) >= 8:
                    parsed["open"] = fields[2]
                    parsed["prev_close"] = fields[3]
                    parsed["high"] = fields[4]
                    parsed["low"] = fields[5]
                    parsed["price"] = fields[6]
                    parsed["change"] = fields[7]
                    parsed["change_pct"] = fields[8]
                if len(fields) >= 14:
                    parsed["date"] = fields[13]
                    parsed["time"] = fields[14]
            elif code.startswith("gb"):
                if len(fields) >= 8:
                    parsed["price"] = fields[1]
                    parsed["change_pct"] = fields[2]
                    parsed["datetime"] = fields[3]
                    parsed["change_amount"] = fields[4]
                    parsed["open"] = fields[5]
                    parsed["high"] = fields[6]
                    parsed["low"] = fields[7]
            data[code] = parsed

    for code in codes:
        if code not in data:
            data[code] = {"name": codes[code], "code": code, "error": "no data"}

    return data


def fetch_sina_news(num: int = 8) -> list:
    """从新浪财经获取最新新闻"""
    cmd = [
        "curl", "-s",
        "-H", "User-Agent: Mozilla/5.0",
        f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num={num}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        items = data.get("result", {}).get("data", [])
        return [item.get("title", "") for item in items if item.get("title")]
    except Exception:
        return []


def fetch_cls_news(num: int = 8) -> list:
    """从财联社电报获取最新快讯"""
    cmd = [
        "curl", "-s",
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "https://www.cls.cn/telegraph",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        html = result.stdout
        m = re.search(r'"data":(\[.*?\]),"total"', html, re.DOTALL)
        if m:
            items = json.loads(m.group(1))
            news = []
            for item in items[:num]:
                title = item.get("title") or item.get("digest") or ""
                if title:
                    news.append(title)
            return news
    except Exception:
        pass
    return []


def fetch_northbound_flow() -> str:
    """获取北向资金/南向资金数据"""
    cmd = [
        "curl", "-s",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "-H", "Referer: https://data.eastmoney.com/",
        "https://push2.eastmoney.com/api/qt/kamt.kline/get?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55&klt=1&lmt=2",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        d = json.loads(result.stdout)
        data = d.get("data", {})
        lines = []
        for key, label in [("hk2sh", "深股通(沪)",), ("sh2hk", "沪市港股通"),
                            ("hk2sz", "深股通(深)"), ("sz2hk", "深市港股通")]:
            vals = data.get(key, [])
            if vals:
                row = vals[-1].split(",")
                if len(row) >= 4:
                    date, buy, sell, net = row[0], row[1], row[2], row[3]
                    try:
                        net_val = float(net)
                        net_str = f"{net_val:+.0f}万"
                    except ValueError:
                        net_str = net
                    lines.append(f"  {label}: 净流入 {net_str} ({date})")
        return "\n".join(lines) if lines else "  暂无数据"
    except Exception:
        return "  获取失败"


def format_index_table(indices: dict, title: str) -> str:
    """格式化指数表格"""
    lines = [f"【{title}】"]
    lines.append(f"{'指数':<16} {'最新价':<12} {'涨跌幅':<10} {'今开':<12} {'昨收':<12}")
    lines.append("-" * 70)
    for code, info in indices.items():
        if info.get("error"):
            lines.append(f"{info['name']:<16} {'数据获取失败':<34}")
            continue
        name = info["name"]
        price = info.get("price", "-")
        change = info.get("change_pct", "-")
        if change != "-":
            try:
                c = float(change)
                change_str = f"{c:+.2f}%"
            except (ValueError, TypeError):
                change_str = str(change)
        else:
            change_str = "-"
        open_ = info.get("open", "-")
        prev = info.get("prev_close", "-")
        lines.append(f"{name:<16} {str(price):<12} {change_str:<10} {str(open_):<12} {str(prev):<12}")
    return "\n".join(lines)


def format_stocks_table(stocks: dict, title: str) -> str:
    """格式化个股表格"""
    lines = [f"【{title}】"]
    for code, info in stocks.items():
        if info.get("error"):
            lines.append(f"  {info['name']}: 数据获取失败")
            continue
        name = info["name"]
        price = info.get("price", "-")
        change = info.get("change_pct", "-")
        if change != "-":
            try:
                c = float(change)
                change_str = f"{c:+.2f}%"
            except (ValueError, TypeError):
                change_str = str(change)
        else:
            change_str = "-"
        lines.append(f"  {name:<16} {str(price):<12} {change_str}")
    return "\n".join(lines)


def get_market_data(mode: str) -> str:
    """获取所有市场数据并格式化为文本"""
    today = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    a_share = fetch_sina(A_SHARE_INDICES)
    hk = fetch_sina(HK_INDICES)
    us = fetch_sina(US_INDICES)
    a_hot = fetch_sina(A_SHARE_HOT)
    hk_hot = fetch_sina(HK_HOT)
    us_hot = fetch_sina(US_HOT)

    sections = [f"📊 市场数据报告 — {today}", ""]

    # A股
    sections.append(format_index_table(a_share, "A股指数"))
    sections.append("")
    sections.append(format_stocks_table(a_hot, "A股热门个股"))
    sections.append("")

    # 港股
    if mode in ("evening", "afternoon"):
        sections.append(format_index_table(hk, "港股指数"))
        sections.append("")
        sections.append(format_stocks_table(hk_hot, "港股热门个股"))
        sections.append("")

    # 美股
    if mode in ("morning", "evening"):
        sections.append(format_index_table(us, "美股指数"))
        sections.append("")
        sections.append(format_stocks_table(us_hot, "美股热门个股"))
        sections.append("")

    # 北向资金
    sections.append("【资金流向】")
    sections.append(fetch_northbound_flow())
    sections.append("")

    # 财联社快讯
    cls_news = fetch_cls_news(8)
    if cls_news:
        sections.append("【财联社快讯】")
        for n in cls_news:
            sections.append(f"  • {n}")
        sections.append("")

    # 新浪财经新闻
    sina_news = fetch_sina_news(8)
    if sina_news:
        sections.append("【财经要闻】")
        for n in sina_news:
            sections.append(f"  • {n}")
        sections.append("")

    return "\n".join(sections)


def call_deepseek(prompt: str) -> str:
    """直接调用 DeepSeek API"""
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


def generate_content(market_data: str, mode: str) -> str:
    """生成文章和口播稿"""
    if mode == "morning":
        prompt = f"""你是一个专业的财经分析师。基于以下今天的最新市场数据，写一篇中文财经早报和60秒口播稿。

当前时间是早上7:00，内容侧重：美股隔夜回顾 + A股盘前展望

【市场数据】
{market_data}

数据中包含了指数行情、热门个股、北向/南向资金流向、财联社快讯、财经要闻等多维度信息。

要求：
1. **财经早报文章**（约500字）：基于全部数据进行综合分析，包含隔夜美股行情总结、中概股表现、资金流向解读、财联社等新闻要闻点评、A股盘前展望与预测
2. **60秒口播稿**（约250-300字）：口语化、适合朗读，开头用"各位听众朋友早上好"
3. 在文章末尾写一段"**今日展望**"，给出对当日市场走势的判断和关注要点
4. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出文章和口播稿内容，不要额外解释。"""
    elif mode == "afternoon":
        prompt = f"""你是一个专业的财经分析师。基于以下今天的最新市场数据，写一篇A股收盘复盘和60秒口播稿。

当前时间是下午15:15，内容侧重：A股收盘总结

【市场数据】
{market_data}

数据中包含了指数行情、热门个股、北向/南向资金流向、财联社快讯、财经要闻等多维度信息。

要求：
1. **收盘复盘文章**（约500字）：基于全部数据，包含三大指数收盘情况、资金流向分析（尤其是北向资金）、财联社等新闻解读、港股实时动态、晚间美股盘前关注
2. **60秒口播稿**（约250-300字）：口语化、适合朗读，开头用"各位投资者朋友下午好"
3. 在文章末尾给出**后市展望和预测**
4. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出文章和口播稿内容，不要额外解释。"""
    else:  # evening
        prompt = f"""你是一个专业的财经分析师。基于以下今天的最新市场数据，写一篇港股收盘复盘+美股盘前分析和60秒口播稿。

当前时间是下午16:30，内容侧重：港股收盘 + 美股盘前

【市场数据】
{market_data}

数据中包含了指数行情、热门个股、北向/南向资金流向、财联社快讯、财经要闻等多维度信息。

要求：
1. **复盘文章**（约500字）：基于全部数据，包含港股收盘总结、南向资金动态分析、A股收盘影响、财联社等新闻要闻点评、美股盘前分析和今晚展望与预测
2. **60秒口播稿**（约250-300字）：口语化、适合朗读，开头用"各位投资者朋友晚上好"
3. 在文章末尾给出**对今晚美股开盘的走势预判**
4. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出文章和口播稿内容，不要额外解释。"""

    return call_deepseek(prompt)


def send_to_feishu(content: str, title: str):
    """发送内容到飞书 webhook"""
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
    if len(sys.argv) < 2 or sys.argv[1] not in ("morning", "afternoon", "evening"):
        print("Usage: market_report.py <morning|afternoon|evening>")
        sys.exit(1)

    mode = sys.argv[1]
    titles = {
        "morning": "📊 每日财经早报 7:00",
        "afternoon": "📊 A股收盘复盘 15:15",
        "evening": "📊 港股收盘+美股盘前 16:30",
    }

    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 开始获取市场数据...")
    market_data = get_market_data(mode)
    print("✅ 数据获取完成")

    print("🤖 调用 DeepSeek V4 生成内容...")
    content = generate_content(market_data, mode)
    print("✅ 内容生成完成")

    print("📤 发送到飞书...")
    result = send_to_feishu(content, titles[mode])
    print(f"📬 飞书响应: {result[:200]}")


if __name__ == "__main__":
    main()
