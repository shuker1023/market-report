#!/usr/bin/env python3
"""
市场数据采集 + DeepSeek 生成 + 飞书推送
v3 — 切换腾讯行情为主要数据源，东方财富为备用
"""
import json
import os
import sys
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# ============================================================
# 配置
# ============================================================
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 各市场指数
A_SHARE_INDICES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
}

HK_INDICES = {
    "szHSI": "恒生指数",
    "szHSTECH": "恒生科技指数",
}

US_INDICES_SP = {
    "usDJI": "道琼斯指数",
    "usIXIC": "纳斯达克指数",
    "usINX": "标普500指数",
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
    "sz00700": "腾讯控股",
    "sz09988": "阿里巴巴",
    "sz09961": "哔哩哔哩",
    "sz01810": "小米集团",
    "sz03690": "美团",
}

US_HOT = {
    "usaapl": "苹果(AAPL)",
    "usmsft": "微软(MSFT)",
    "usnvda": "英伟达(NVDA)",
    "ustsla": "特斯拉(TSLA)",
    "usamzn": "亚马逊(AMZN)",
}


# ============================================================
# 腾讯行情 API (主数据源)
# ============================================================

def fetch_qq_data(codes: list) -> dict:
    """
    从腾讯行情获取数据
    接口: qt.gtimg.cn/q=code1,code2,...
    返回格式: v_code1="fields..."; v_code2="fields...";
    关键字段索引(以分号分隔):
      3=最新价, 4=昨收, 5=今开,
      29=涨跌额, 30=涨跌幅%,
      32=最高, 33=最低
    编码: GBK
    """
    if not codes:
        return {}
    qstring = ",".join(codes)
    url = f"https://qt.gtimg.cn/q={qstring}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://qt.gtimg.cn/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            # Tencent returns GBK encoding
            text = raw.decode("gbk", errors="ignore")
    except Exception as e:
        return {}

    result = {}
    # Each line: v_code="field1~field2~..."
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or not line.startswith("v_"):
            continue
        # Extract the quoted string
        m = re.match(r'v_\w+="(.+)"', line)
        if not m:
            continue
        fields = m.group(1).split("~")
        if len(fields) < 34:
            continue
        code = fields[2]  # market code
        name = fields[1]  # name
        price = fields[3] if fields[3] not in ("", "0.00") else "-"
        prev_close = fields[4] if fields[4] not in ("", "0.00") else "-"
        open_ = fields[5] if fields[5] not in ("", "0.00") else "-"
        change_amount = fields[29] if fields[29] not in ("", "0.00") else "-"
        change_pct = fields[30] if fields[30] not in ("", "0.00") else "-"
        high = fields[32] if fields[32] not in ("", "0.00") else "-"
        low = fields[33] if fields[33] not in ("", "0.00") else "-"

        parsed = {
            "code": code,
            "name": name,
            "price": price,
            "prev_close": prev_close,
            "open": open_,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "high": high,
            "low": low,
        }
        result[code] = parsed
    return result


def map_qq_result(qq_data: dict, codes: dict) -> dict:
    """将腾讯行情结果映射到统一格式，codes 的 key 是 Tencent 格式的 code"""
    result = {}
    for tcode, cname in codes.items():
        # Extract the numeric part from tcode (e.g., "sh000001" -> "000001")
        m = re.search(r"(\d+)$", tcode)
        num_code = m.group(1) if m else tcode
        if num_code in qq_data:
            info = qq_data[num_code]
            result[tcode] = {
                "name": cname,
                "code": num_code,
                "price": info.get("price", "-"),
                "change_pct": info.get("change_pct", "-"),
                "change_amount": info.get("change_amount", "-"),
                "high": info.get("high", "-"),
                "low": info.get("low", "-"),
                "open": info.get("open", "-"),
                "prev_close": info.get("prev_close", "-"),
            }
        else:
            result[tcode] = {"name": cname, "code": num_code, "error": "no data"}
    return result


def get_a_share_data() -> dict:
    """获取A股指数 — 腾讯主 + 东方财富备"""
    print("  📡 获取 A股指数 (腾讯行情)...")
    qq = fetch_qq_data(list(A_SHARE_INDICES.keys()))
    result = map_qq_result(qq, A_SHARE_INDICES)
    if validate_market_data(result, "A股指数(腾讯)"):
        return result

    # Fallback: 东方财富
    print("  ⚠️ 腾讯A股指数失败，切换东方财富...")
    return get_a_share_eastmoney()


def get_a_share_eastmoney() -> dict:
    """东方财富备选 — A股指数"""
    sh = fetch_eastmoney_indices(
        {k: v for k, v in A_SHARE_INDICES.items() if k.startswith("sh")},
        prefix="1",
        code_map={"sh": ""},
    )
    sz = fetch_eastmoney_indices(
        {k: v for k, v in A_SHARE_INDICES.items() if k.startswith("sz")},
        prefix="0",
        code_map={"sz": ""},
    )
    result = {**sh, **sz}
    if not validate_market_data(result, "A股指数(东方财富)"):
        print("  ❌ A股指数所有数据源均失败")
        sys.exit(1)
    return result


def get_a_hot_data() -> dict:
    """获取A股热门个股 — 腾讯主 + 东方财富备"""
    print("  📡 获取 A股热门个股 (腾讯行情)...")
    qq = fetch_qq_data(list(A_SHARE_HOT.keys()))
    result = map_qq_result(qq, A_SHARE_HOT)
    if validate_market_data(result, "A股热门(腾讯)"):
        return result
    print("  ⚠️ 腾讯A股个股失败，切换东方财富...")
    return fetch_eastmoney_stocks(A_SHARE_HOT)


def get_hk_data() -> dict:
    """获取港股指数"""
    print("  📡 获取港股指数 (腾讯行情)...")
    qq = fetch_qq_data(list(HK_INDICES.keys()))
    result = map_qq_result(qq, HK_INDICES)
    if validate_market_data(result, "港股指数(腾讯)"):
        return result
    print("  ⚠️ 腾讯港股指数失败，切换东方财富...")
    return fetch_eastmoney_hk(HK_INDICES)


def get_hk_hot_data() -> dict:
    """获取港股热门个股"""
    print("  📡 获取港股热门个股 (腾讯行情)...")
    qq = fetch_qq_data(list(HK_HOT.keys()))
    result = map_qq_result(qq, HK_HOT)
    if validate_market_data(result, "港股热门(腾讯)"):
        return result
    print("  ⚠️ 腾讯港股个股失败，切换东方财富...")
    return fetch_eastmoney_hk(HK_HOT)


def get_us_data() -> dict:
    """获取美股指数"""
    print("  📡 获取美股指数 (腾讯行情)...")
    qq = fetch_qq_data(list(US_INDICES_SP.keys()))
    result = map_qq_result(qq, US_INDICES_SP)
    if validate_market_data(result, "美股指数(腾讯)"):
        return result
    print("  ⚠️ 腾讯美股指数失败，切换东方财富...")
    return fetch_us_stocks(US_INDICES_SP)


def get_us_hot_data() -> dict:
    """获取美股热门个股"""
    print("  📡 获取美股热门个股 (腾讯行情)...")
    qq = fetch_qq_data(list(US_HOT.keys()))
    result = map_qq_result(qq, US_HOT)
    if validate_market_data(result, "美股热门(腾讯)"):
        return result
    print("  ⚠️ 腾讯美股个股失败，切换东方财富...")
    return fetch_us_stocks(US_HOT)


# ============================================================
# 东方财富 API (备用)
# ============================================================

def fetch_eastmoney_indices(codes: dict, prefix: str = "1", code_map: dict = None) -> dict:
    """
    从东方财富获取指数/股票行情 (备用)
    codes: dict with Tencent-format keys -> name
    """
    # Build secids from the numeric part of each code
    secid_list = []
    code_to_key = {}
    for tcode, name in codes.items():
        m = re.search(r"(\d+)$", tcode)
        num_code = m.group(1) if m else tcode
        secid = f"{prefix}.{num_code}"
        secid_list.append(secid)
        code_to_key[secid] = tcode

    if not secid_list:
        return {}

    url = (
        f"https://push2.eastmoney.com/api/qt/ulist.np/get"
        f"?fltt=2&invt=2&fields=f2,f3,f4,f12,f14,f15,f16,f17,f18"
        f"&secids={','.join(secid_list)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
    except Exception as e:
        return {tcode: {"error": str(e), "name": name, "code": tcode} for tcode, name in codes.items()}

    result = {}
    items = raw.get("data", {}).get("diff", []) if raw.get("data") else []
    for item in items:
        f12 = str(item.get("f12", ""))
        secid = f"{prefix}.{f12}"
        tcode = code_to_key.get(secid)
        if tcode and tcode in codes:
            f2 = item.get("f2")
            f3 = item.get("f3")
            f4 = item.get("f4")
            f15 = item.get("f15")
            f16 = item.get("f16")
            f17 = item.get("f17")
            f18 = item.get("f18")
            result[tcode] = {
                "name": codes[tcode],
                "code": f12,
                "price": f2 if f2 is not None else "-",
                "change_pct": f3 if f3 is not None else "-",
                "change_amount": f4 if f4 is not None else "-",
                "high": f15 if f15 is not None else "-",
                "low": f16 if f16 is not None else "-",
                "open": f17 if f17 is not None else "-",
                "prev_close": f18 if f18 is not None else "-",
            }

    for tcode in codes:
        if tcode not in result:
            result[tcode] = {"name": codes[tcode], "code": tcode, "error": "no data"}
    return result


def fetch_eastmoney_hk(codes: dict) -> dict:
    """港股使用东方财富港股接口"""
    # For HK stocks through eastmoney, we use prefix "128"
    secid_list = []
    code_to_key = {}
    for tcode, name in codes.items():
        m = re.search(r"(\d+)$", tcode)
        num_code = m.group(1) if m else tcode
        secid = f"128.{num_code}"
        secid_list.append(secid)
        code_to_key[secid] = tcode

    if not secid_list:
        return {}

    url = (
        f"https://push2.eastmoney.com/api/qt/ulist.np/get"
        f"?fltt=2&invt=2&fields=f2,f3,f4,f12,f14,f15,f16,f17,f18"
        f"&secids={','.join(secid_list)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
    except Exception as e:
        return {tcode: {"error": str(e), "name": name, "code": tcode} for tcode, name in codes.items()}

    result = {}
    items = raw.get("data", {}).get("diff", []) if raw.get("data") else []
    for item in items:
        f12 = str(item.get("f12", ""))
        secid = f"128.{f12}"
        tcode = code_to_key.get(secid)
        if tcode and tcode in codes:
            f2 = item.get("f2")
            f3 = item.get("f3")
            result[tcode] = {
                "name": codes[tcode],
                "code": f12,
                "price": f2 if f2 is not None else "-",
                "change_pct": f3 if f3 is not None else "-",
            }

    for tcode in codes:
        if tcode not in result:
            result[tcode] = {"name": codes[tcode], "code": tcode, "error": "no data"}
    return result


def fetch_eastmoney_stocks(codes: dict) -> dict:
    """A股个股备用"""
    # Map Tencent format codes to eastmoney prefixes
    sh_codes = {}
    sz_codes = {}
    for tcode, name in codes.items():
        m = re.search(r"(\d+)$", tcode)
        num_code = m.group(1) if m else tcode
        if tcode.startswith("sh") or num_code.startswith("6"):
            sh_codes[tcode] = name
        else:
            sz_codes[tcode] = name

    result = {}
    if sh_codes:
        result.update(fetch_eastmoney_indices(sh_codes, prefix="1"))
    if sz_codes:
        result.update(fetch_eastmoney_indices(sz_codes, prefix="0"))
    for tcode in codes:
        if tcode not in result:
            result[tcode] = {"name": codes[tcode], "code": tcode, "error": "no data"}
    return result


def fetch_us_stocks(codes: dict) -> dict:
    """美股备用 — 东方财富国际接口"""
    data = {}
    symbol_to_code = {}
    secids = []
    for tcode, name in codes.items():
        m = re.search(r"(\w+)$", tcode)
        sym = m.group(1) if m else tcode
        sid = f"105.{sym}"
        secids.append(sid)
        symbol_to_code[sid] = tcode

    if not secids:
        return data

    url = (
        f"https://push2.eastmoney.com/api/qt/ulist.np/get"
        f"?fltt=2&invt=2&fields=f2,f3,f4,f12,f14,f15,f16,f17,f18"
        f"&secids={','.join(secids)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
    except Exception as e:
        return {tcode: {"error": str(e), "name": name, "code": tcode} for tcode, name in codes.items()}

    items = raw.get("data", {}).get("diff", []) if raw.get("data") else []
    for item in items:
        f12 = str(item.get("f12", ""))
        secid = f"105.{f12}"
        tcode = symbol_to_code.get(secid)
        if tcode and tcode in codes:
            f2 = item.get("f2")
            f3 = item.get("f3")
            data[tcode] = {
                "name": codes[tcode],
                "code": f12,
                "price": f2 if f2 is not None else "-",
                "change_pct": f3 if f3 is not None else "-",
            }

    for tcode in codes:
        if tcode not in data:
            data[tcode] = {"name": codes[tcode], "code": tcode, "error": "no data"}
    return data


def fetch_northbound_flow() -> str:
    """获取北向资金/南向资金数据"""
    url = (
        "https://push2.eastmoney.com/api/qt/kamt.kline/get"
        "?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55&klt=1&lmt=2"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://data.eastmoney.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
    except Exception:
        return "  获取失败"

    data = d.get("data", {})
    lines = []
    for key, label in [
        ("hk2sh", "沪股通"),
        ("sh2hk", "沪市港股通"),
        ("hk2sz", "深股通"),
        ("sz2hk", "深市港股通"),
    ]:
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


def fetch_sina_news(num: int = 8) -> list:
    """从新浪财经获取最新新闻"""
    url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num={num}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        items = d.get("result", {}).get("data", [])
        return [item.get("title", "") for item in items if item.get("title")]
    except Exception:
        return []


def fetch_cls_news(num: int = 8) -> list:
    """从财联社电报获取最新快讯"""
    req = urllib.request.Request(
        "https://www.cls.cn/telegraph",
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
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


def fetch_longbridge(mode: str) -> str:
    """从长桥OpenAPI获取行情数据"""
    app_key = os.environ.get("LONGBRIDGE_APP_KEY", "")
    app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", "")
    access_token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", "")

    if not all([app_key, app_secret, access_token]):
        return ""

    try:
        from longbridge.openapi import QuoteContext, Config
    except ImportError:
        return ""

    try:
        config = Config(app_key, app_secret, access_token)
        ctx = QuoteContext(config)

        symbols = [
            "000001.SH", "399001.SZ", "399006.SZ",
            "HSI.HK", "HSTECH.HK",
            "DJI.US", "IXIC.US", "INX.US",
        ]

        if mode in ("afternoon", "evening"):
            symbols += ["600519.SH", "300750.SZ", "000858.SZ",
                        "00700.HK", "09988.HK", "09961.HK", "01810.HK", "03690.HK"]
        if mode in ("morning", "evening", "weekly"):
            symbols += ["AAPL.US", "MSFT.US", "NVDA.US", "TSLA.US", "AMZN.US"]

        resp = ctx.get_quote(symbols)

        output = {"沪深": [], "港股": [], "美股": []}
        lb_names = {
            "000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指",
            "HSI.HK": "恒生指数", "HSTECH.HK": "恒生科技",
            "DJI.US": "道琼斯", "IXIC.US": "纳斯达克", "INX.US": "标普500",
            "600519.SH": "贵州茅台", "300750.SZ": "宁德时代", "000858.SZ": "五粮液",
            "00700.HK": "腾讯控股", "09988.HK": "阿里巴巴", "09961.HK": "哔哩哔哩",
            "01810.HK": "小米集团", "03690.HK": "美团",
            "AAPL.US": "苹果", "MSFT.US": "微软", "NVDA.US": "英伟达",
            "TSLA.US": "特斯拉", "AMZN.US": "亚马逊",
        }

        for quote in resp:
            sym = quote.symbol
            name = lb_names.get(sym, sym)
            price = quote.last_done
            change_rate = (quote.change_rate * 100) if quote.change_rate is not None else None

            if sym.endswith(".SH") or sym.endswith(".SZ"):
                bucket = "沪深"
            elif sym.endswith(".HK"):
                bucket = "港股"
            elif sym.endswith(".US"):
                bucket = "美股"
            else:
                continue

            change_str = f"{change_rate:+.2f}%" if change_rate is not None else "-"
            output[bucket].append(f"  {name:<12} {price:<12} {change_str}")

        lines = ["【长桥行情数据】"]
        for bucket_name, items in output.items():
            if items:
                for item in items:
                    lines.append(item)
        return "\n".join(lines)
    except Exception as e:
        return f"【长桥】数据获取异常: {str(e)}"


# ============================================================
# 数据校验与格式化
# ============================================================

def validate_market_data(data: dict, label: str) -> bool:
    """检查数据是否有效"""
    total = len(data)
    if total == 0:
        print(f"  ⚠️ {label}: 无任何数据")
        return False
    errors = sum(1 for v in data.values() if v.get("error") or v.get("price") in (None, "-", ""))
    if errors > total / 2:
        print(f"  ⚠️ {label}: {errors}/{total} 条数据无效")
        return False
    return True


def format_index_table(indices: dict, title: str) -> str:
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
    print(f"  📡 数据时间: {today}")

    # A股指数 — 腾讯主 + 东方财富备
    a_share = get_a_share_data()

    # A股热门个股
    a_hot = get_a_hot_data()

    # 港股
    print("  📡 获取港股数据...")
    hk = get_hk_data()
    hk_hot = get_hk_hot_data()

    # 美股
    print("  📡 获取美股数据...")
    us = get_us_data()
    us_hot = get_us_hot_data()

    sections = [f"📊 市场数据报告 — {today}", ""]

    # A股
    sections.append(format_index_table(a_share, "A股指数"))
    sections.append("")
    sections.append(format_stocks_table(a_hot, "A股热门个股"))
    sections.append("")

    # 港股 (evening / afternoon / weekly)
    if mode in ("evening", "afternoon", "weekly"):
        sections.append(format_index_table(hk, "港股指数"))
        sections.append("")
        sections.append(format_stocks_table(hk_hot, "港股热门个股"))
        sections.append("")

    # 美股 (morning / evening / weekly)
    if mode in ("morning", "evening", "weekly"):
        sections.append(format_index_table(us, "美股指数"))
        sections.append("")
        sections.append(format_stocks_table(us_hot, "美股热门个股"))
        sections.append("")

    # 资金流向
    sections.append("【资金流向】")
    sections.append(fetch_northbound_flow())
    sections.append("")

    # 长桥（补充）
    lb = fetch_longbridge(mode)
    if lb:
        sections.append(lb)
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
    """调用 DeepSeek API"""
    if not DEEPSEEK_API_KEY:
        return "【错误】未设置 DEEPSEEK_API_KEY"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 4096,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        return f"【错误】DeepSeek API HTTP {e.code}: {error_body[:300]}"
    except Exception as e:
        return f"【错误】DeepSeek 调用失败: {str(e)}"


def generate_content(market_data: str, mode: str) -> str:
    """生成文章和口播稿"""
    now = datetime.now(CST)
    date_str = now.strftime("%Y年%m月%d日")
    weekday = now.weekday()
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    is_trading_day = weekday < 5

    base_instruction = f"""
重要说明：
1. 数据采集时间为 {now.strftime('%Y-%m-%d %H:%M')}，{date_str} {weekday_names[weekday]}
2. {'今天是交易日' if is_trading_day else '今天是非交易日（周末），数据为最近一个交易日的收盘数据'}
3. 必须严格基于上方提供的实际数据进行分析，不要编造数据
4. 涨跌幅为"+"表示上涨，"-"表示下跌
5. 如果某条数据显示"数据获取失败"或价格为空，请在分析中注明该数据不可用
"""

    if mode == "morning":
        prompt = f"""你是一个专业的财经分析师。基于以下今天的最新市场数据，写一篇中文财经早报和60秒口播稿。

当前时间是早上7:00，内容侧重：美股隔夜回顾 + A股盘前展望

【市场数据】
{market_data}

{base_instruction}

要求：
1. **财经早报文章**（约500字）：基于全部数据进行综合分析，包含隔夜美股行情总结、中概股表现、资金流向解读、新闻要闻点评、A股盘前展望
2. **60秒口播稿**（约250-300字）：口语化、适合朗读，开头用"各位听众朋友早上好"
3. 在文章末尾写一段"**今日展望**"
4. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出内容，不要额外解释。"""
    elif mode == "afternoon":
        prompt = f"""你是一个专业的财经分析师。基于以下今天的最新市场数据，写一篇A股收盘复盘和60秒口播稿。

当前时间是下午15:15，内容侧重：A股收盘总结

【市场数据】
{market_data}

{base_instruction}

要求：
1. **收盘复盘文章**（约500字）：基于全部数据，包含三大指数收盘情况、资金流向分析（尤其是北向资金）、新闻解读、港股实时动态、晚间美股盘前关注
2. **60秒口播稿**（约250-300字）：口语化、适合朗读，开头用"各位投资者朋友下午好"
3. 在文章末尾给出**后市展望**
4. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出内容，不要额外解释。"""
    elif mode == "evening":
        prompt = f"""你是一个专业的财经分析师。基于以下今天的最新市场数据，写一篇港股收盘复盘+美股盘前分析和60秒口播稿。

当前时间是下午16:30，内容侧重：港股收盘 + 美股盘前

【市场数据】
{market_data}

{base_instruction}

要求：
1. **复盘文章**（约500字）：基于全部数据，包含港股收盘总结、南向资金动态分析、A股收盘影响、新闻要闻点评、美股盘前分析和今晚展望
2. **60秒口播稿**（约250-300字）：口语化、适合朗读，开头用"各位投资者朋友晚上好"
3. 在文章末尾给出**对今晚美股开盘的走势预判**
4. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出内容，不要额外解释。"""
    elif mode == "weekly":
        prompt = f"""你是一个专业的财经分析师。基于以下本周的市场数据，写一篇下周市场展望报告。

当前时间是周五晚间，内容侧重：本周行情回顾 + 下周市场展望

【市场数据】
{market_data}

{base_instruction}

要求：
1. **下周展望报告**（约800字）：基于本周全部数据，包含：
   - 本周A股、港股、美股三大市场行情回顾
   - 资金流向趋势分析
   - 本周重大新闻事件解读
   - 下周各市场走势预测（分别给出A股、港股、美股的判断）
   - 关键风险提示和关注事件
2. **60秒口播稿**（约250-300字）：口语化、适合朗读，开头用"各位投资者朋友大家好"
3. 格式：先用"【文章】"标记文章，再用"【口播】"标记口播稿

注意：直接输出内容，不要额外解释。"""

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

    body = json.dumps(card, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            FEISHU_WEBHOOK,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode()
    except Exception as e:
        return f"发送失败: {str(e)}"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("morning", "afternoon", "evening", "weekly"):
        print("Usage: market_report.py <morning|afternoon|evening|weekly>")
        sys.exit(1)

    mode = sys.argv[1]
    titles = {
        "morning": "📊 每日财经早报 7:00",
        "afternoon": "📊 A股收盘复盘 15:15",
        "evening": "📊 港股收盘+美股盘前 16:30",
        "weekly": "📊 下周市场展望 每周五",
    }

    if not DEEPSEEK_API_KEY:
        print("❌ 未设置 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    if not FEISHU_WEBHOOK:
        print("❌ 未设置 FEISHU_WEBHOOK 环境变量")
        sys.exit(1)

    print(f"🤖 使用模型: {DEEPSEEK_MODEL}")
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 开始获取市场数据...")
    market_data = get_market_data(mode)
    print("✅ 数据获取完成")

    print("🤖 调用 DeepSeek 生成内容...")
    content = generate_content(market_data, mode)
    print("✅ 内容生成完成")

    print("📤 发送到飞书...")
    result = send_to_feishu(content, titles[mode])
    print(f"📬 飞书响应: {result[:200]}")


if __name__ == "__main__":
    main()
