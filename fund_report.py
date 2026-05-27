#!/usr/bin/env python3
"""
基金持仓分析：每日净值 + 重仓股行情 + DeepSeek 分析 + 飞书推送
数据源：天天基金（净值）+ 长桥/Sina（重仓股行情）
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-v4-pro"

# ========== 用户持仓基金配置 ==========
PORTFOLIO = [
    {"code": "022365", "name": "永赢科技智选混合C", "type": "偏股混合"},
    {"code": "019018", "name": "易方达信息产业混合C", "type": "偏股混合"},
]


# ========== 天天基金数据接口 ==========

def fetch_fund_nav(code: str, days: int = 30) -> list:
    """获取基金历史净值"""
    url = f"https://api.fund.eastmoney.com/f10/lsjz?callback=j&fundCode={code}&pageIndex=1&pageSize={days}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://fundf10.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
            m = re.search(r'\{.*\}', raw)
            if not m:
                return []
            data = json.loads(m.group())
            records = data.get("Data", {}).get("LSJZList", [])
            result = []
            for row in records:
                result.append({
                    "date": row.get("FSRQ", ""),
                    "nav": float(row.get("DWJZ", 0)) if row.get("DWJZ") else 0,
                    "acc_nav": float(row.get("LJJZ", 0)) if row.get("LJJZ") else 0,
                    "change_pct": row.get("JZZZL", ""),
                })
            return result
    except Exception as e:
        print(f"  [净值] {code} 获取失败: {e}", file=sys.stderr)
        return []


def fetch_fund_realtime(code: str) -> dict:
    """获取基金盘中实时估值"""
    url = f"http://fundgz.1234567.com.cn/js/{code}.js"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "http://fundgz.1234567.com.cn/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
            m = re.search(r'\{.*\}', raw)
            if m:
                data = json.loads(m.group())
                return {
                    "fundcode": data.get("fundcode", code),
                    "name": data.get("name", ""),
                    "nav": data.get("dwjz", "-"),       # 昨日净值
                    "gsz": data.get("gsz", "-"),         # 估算净值
                    "gszzl": data.get("gszzl", "-"),     # 估算涨跌幅
                    "gztime": data.get("gztime", ""),    # 估算时间
                }
    except Exception as e:
        print(f"  [估值] {code} 获取失败: {e}", file=sys.stderr)
    return {}


def fetch_fund_holdings(code: str) -> list:
    """获取基金前十大重仓股（从页面HTML解析）"""
    url = f"https://fundf10.eastmoney.com/ccmx_{code}.html"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://fundf10.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8")

        # 找第一个持仓表格 (最新报告期)
        # 格式: <td><a href=...>股票名称</a></td>  <td class='tor'>占比%</td>
        stocks = re.findall(
            r'<td[^>]*><a[^>]*target="_blank"[^>]*>([^<]+)</a></td>\s*<td[^>]*class="tor"[^>]*>([^<]+)</td>',
            html
        )
        result = []
        for name, pct in stocks[:10]:
            try:
                pct_val = float(pct.replace("%", ""))
            except ValueError:
                pct_val = 0
            result.append({"name": name.strip(), "pct": pct_val})
        return result
    except Exception as e:
        print(f"  [持仓] {code} 解析失败: {e}", file=sys.stderr)
        return []


def fetch_stock_quotes(stock_names: list) -> dict:
    """获取重仓股当日行情（通过新浪财经）"""
    if not stock_names:
        return {}

    # 构建新浪代码映射（简化版：只处理常见命名）
    sina_map = {}
    name_to_code = {}
    for name in stock_names:
        # 先搜索新浪的股票代码（通过简单的关键词匹配）
        # 这里直接用已知的重仓股代码
        pass

    # 由于股票代码映射复杂，这里先用已知的常见重仓股
    known_stocks = {
        "中际旭创": "sz300308",
        "新易盛": "sz300502",
        "沪电股份": "sz002463",
        "深南电路": "sz002916",
        "工业富联": "sh601138",
        "东山精密": "sz002384",
        "寒武纪": "sh688256",
        "腾景科技": "sh688195",
        "江丰电子": "sz300666",
        "博迁新材": "sh605376",
        "中国巨石": "sh600176",
        "鼎泰高科": "sz301377",
    }

    codes = {}
    for name in stock_names:
        if name in known_stocks:
            codes[known_stocks[name]] = name

    if not codes:
        return {}

    symbols = ",".join(codes.keys())
    cmd = [
        "curl", "-s",
        "-H", "Referer: https://finance.sina.com.cn",
        f"https://hq.sinajs.cn/list={symbols}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        raw = result.stdout
    except Exception:
        return {}

    try:
        raw = raw.encode("latin1").decode("gbk")
    except Exception:
        pass

    quotes = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or not line.startswith("var hq_str_"):
            continue
        m = re.search(r'"([^"]*)"', line)
        if not m:
            continue
        fields = m.group(1).split(",")
        name_in_data = fields[0] if fields else ""
        code_match = re.search(r"hq_str_(\w+)=", line)
        sina_code = code_match.group(1) if code_match else ""

        stock_name = codes.get(sina_code, name_in_data)
        price = fields[3] if len(fields) > 3 else "-"
        prev_close = fields[2] if len(fields) > 2 else "0"
        change_pct = "-"
        if price != "-" and prev_close != "0":
            try:
                change_pct = round((float(price) - float(prev_close)) / float(prev_close) * 100, 2)
            except (ValueError, ZeroDivisionError):
                pass
        quotes[stock_name] = {
            "price": price,
            "change_pct": change_pct,
        }

    return quotes


def fetch_cls_news(num: int = 5) -> list:
    """从财联社获取最新快讯"""
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


# ========== 数据聚合 ==========

def build_fund_report() -> str:
    """获取所有基金数据并格式化为文本"""
    today = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 基金持仓分析报告 — {today}", ""]

    all_holdings = []

    for fund in PORTFOLIO:
        code = fund["code"]
        name = fund["name"]
        lines.append(f"【{name} ({code})】")

        # 实时估值
        rt = fetch_fund_realtime(code)
        if rt:
            lines.append(f"  实时估值: {rt.get('gsz', '-')}  |  估算涨跌: {rt.get('gszzl', '-')}%")
            lines.append(f"  昨日净值: {rt.get('nav', '-')}")
        else:
            lines.append(f"  实时估值: 获取失败")

        # 近期净值走势
        navs = fetch_fund_nav(code, 20)
        if len(navs) >= 2:
            latest = navs[-1]
            prev = navs[-2]
            lines.append(f"  最新净值 ({latest['date']}): {latest['nav']}  |  日涨跌: {latest['change_pct']}%")
            lines.append(f"  前一日: {prev['nav']}  ({prev['date']})")

            # 近期表现
            if len(navs) >= 5:
                change_5d = (latest['nav'] - navs[-5]['nav']) / navs[-5]['nav'] * 100
                lines.append(f"  近5日涨幅: {change_5d:+.2f}%")
            if len(navs) >= 20:
                change_20d = (latest['nav'] - navs[-20]['nav']) / navs[-20]['nav'] * 100
                lines.append(f"  近20日涨幅: {change_20d:+.2f}%")
        else:
            lines.append(f"  净值数据不足")

        # 重仓股
        holdings = fetch_fund_holdings(code)
        if holdings:
            lines.append(f"  前十大重仓股:")
            all_holdings.extend(holdings)
            for h in holdings:
                lines.append(f"    {h['name']:<12} {h['pct']:>5.2f}%")
        else:
            lines.append(f"  重仓股: 获取失败")
        lines.append("")

    # 重仓股行情（去重）
    if all_holdings:
        unique_stocks = list(dict.fromkeys([h["name"] for h in all_holdings]))
        lines.append("【重仓股今日行情】")
        quotes = fetch_stock_quotes(unique_stocks)
        if quotes:
            for stock in unique_stocks:
                if stock in quotes:
                    q = quotes[stock]
                    change_str = f"{q['change_pct']:+.2f}%" if q['change_pct'] != '-' else "-"
                    lines.append(f"  {stock:<12} {q['price']:<10} {change_str}")
                else:
                    lines.append(f"  {stock:<12} 获取失败")
        else:
            lines.append("  行情数据获取失败")
        lines.append("")

    # 财联社快讯
    news = fetch_cls_news(5)
    if news:
        lines.append("【今日要闻】")
        for n in news:
            lines.append(f"  • {n}")
        lines.append("")

    return "\n".join(lines)


# ========== DeepSeek 分析 ==========

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


def generate_analysis(fund_data: str) -> str:
    """生成专业级基金分析报告"""
    prompt = f"""【系统角色】
你是一位在头部基金公司从业10年以上的公募基金经理/基金研究员，专注于TMT/科技赛道投资。
你的风格是"基金研报"级别：数据驱动、逻辑严密、注重风险收益比分析。
每一条判断都必须基于提供的具体数据展开，严禁空泛描述。

【写作要求】
- 每个结论必须引用具体数据（净值涨跌幅、重仓股行情、收益率等）
- 直接输出内容，不要额外解释

【基金数据】
{fund_data}

【分析框架 — 严格按照以下结构输出】
1. 持仓组合总览 (2-3句)
   - 今日两只基金整体表现（引用具体净值/估算涨跌幅）
   - 对比基准表现，跑赢还是跑输

2. 净值归因分析 (3-4句)
   - 结合重仓股当日行情，分解净值波动来源
   - 找出对净值影响最大的1-2只重仓股
   - 分析板块驱动因素（光通信/PCB/半导体/AI算力等）

3. 基金对比 (2-3句)
   - 022365永赢科技 vs 019018易方达信息产业
   - 持仓差异带来的表现差异
   - 风格评价（进攻型 vs 均衡型）

4. 风险提示 (1-2句)
   - 当前共同的持仓风险点（集中度、板块估值、外围风险等）

5. 综合研判 (2-3句)
   - 当前科技板块整体判断
   - 持仓评估：匹配当前市场环境的程度
   - 后市关注的核心变量

6. 【口播稿】
   - 约200字，口语化适合朗读，开头"各位投资者朋友大家好"
   - 浓缩核心结论：今天怎么样了？为什么？接下来看什么？"""

    return call_deepseek(prompt)


# ========== 飞书推送 ==========

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


# ========== 主函数 ==========

def main():
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 获取基金数据...")
    fund_data = build_fund_report()
    print("✅ 数据获取完成")
    print(fund_data[:500] + "...\n")

    print("🤖 调用 DeepSeek 生成分析...")
    content = generate_analysis(fund_data)
    print("✅ 内容生成完成")

    print("📤 发送到飞书...")
    result = send_to_feishu(content, "📊 基金持仓收盘分析 17:00")
    print(f"📬 飞书响应: {result[:200]}")

    print("\n" + "=" * 40)
    print(content)


if __name__ == "__main__":
    main()
