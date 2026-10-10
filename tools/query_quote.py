#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情、五档盘口与分时K线快速查询工具
数据源：腾讯金融 API (qt.gtimg.cn / 腾讯日K多主机，见 tencent_kline)
"""

import sys
import json
import urllib.request
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from zoneinfo import ZoneInfo

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "daily-stock-analysis" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import tencent_kline  # noqa: E402  腾讯日 K 主机列表单一来源
import tls_context  # noqa: E402  TLS 校验上下文唯一来源（默认校验证书）

ssl_ctx = tls_context.build_context()
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

def normalize_code(code: str) -> str:
    """自动添加市场前缀 sh / sz"""
    code_clean = code.strip().lower()
    if code_clean.startswith("sh") or code_clean.startswith("sz"):
        return code_clean
    if code_clean.startswith("6") or code_clean.startswith("9"):
        return f"sh{code_clean}"
    return f"sz{code_clean}"


def _quote_number(parts: List[str], index: int) -> float:
    """Read one optional numeric Tencent field without shifting its sibling."""
    if index >= len(parts):
        return 0.0
    value = parts[index].strip().strip('"')
    if not value:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_realtime_quotes(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量查询实时行情与五档买卖盘口"""
    symbols = [normalize_code(c) for c in codes]
    url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
    
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            content = resp.read().decode("gbk", errors="ignore")
    except Exception as e:
        print(f"请求失败: {e}", file=sys.stderr)
        return {}

    results = {}
    lines = content.strip().split(";")
    for line in lines:
        line = line.strip()
        if not line or "~" not in line:
            continue
        parts = line.split("~")
        if len(parts) < 40:
            continue
        
        name = parts[1]
        code = parts[2]
        price = float(parts[3]) if parts[3] else 0.0
        pre_close = float(parts[4]) if parts[4] else 0.0
        open_price = float(parts[5]) if parts[5] else 0.0
        pct = float(parts[32]) if parts[32] else 0.0
        high = float(parts[33]) if parts[33] else 0.0
        low = float(parts[34]) if parts[34] else 0.0
        amount_wan = float(parts[37]) if parts[37] else 0.0
        time_str = parts[30]
        
        # 买卖五档
        buy_orders = []
        for b_idx in range(5):
            p = float(parts[9 + b_idx * 2]) if parts[9 + b_idx * 2] else 0.0
            v = int(parts[10 + b_idx * 2]) if parts[10 + b_idx * 2] else 0
            buy_orders.append((p, v))

        sell_orders = []
        for s_idx in range(5):
            p = float(parts[19 + s_idx * 2]) if parts[19 + s_idx * 2] else 0.0
            v = int(parts[20 + s_idx * 2]) if parts[20 + s_idx * 2] else 0
            sell_orders.append((p, v))

        results[code] = {
            "name": name,
            "code": code,
            "price": price,
            "pre_close": pre_close,
            "open": open_price,
            "pct": pct,
            "high": high,
            "low": low,
            "amount_wan": amount_wan,
            "time": time_str,
            "buy_orders": buy_orders,
            "sell_orders": sell_orders,
            # 腾讯行情零基字段：47 为涨停价，48 为跌停价。
            "zt": _quote_number(parts, 47),
            "dt": _quote_number(parts, 48),
        }

    return results

def fetch_minute_data(code: str, expected_date: Optional[str] = None) -> List[str]:
    """查询当前交易日 1 分钟分时明细。

    ``expected_date`` is an optional guard for callers that have a known
    market date.  Tencent's minute endpoint is current-day-only, so a
    request for another date must return no evidence rather than silently
    handing today's bars to a historical caller.  The one-argument calling
    convention remains unchanged for the interactive quote tools.
    """
    sym = normalize_code(code)
    normalized_expected_date: Optional[str] = None
    if expected_date is not None:
        normalized_expected_date = str(expected_date).strip().replace("-", "")
        if len(normalized_expected_date) != 8 or not normalized_expected_date.isdigit():
            return []
        try:
            normalized_expected_date = datetime.strptime(
                normalized_expected_date, "%Y%m%d"
            ).strftime("%Y%m%d")
        except ValueError:
            return []
        if datetime.now(SHANGHAI_TZ).strftime("%Y%m%d") != normalized_expected_date:
            return []

    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        securities = data.get("data")
        if not isinstance(securities, dict):
            return []
        security = securities.get(sym)
        if not isinstance(security, dict):
            return []
        minute_payload = security.get("data")
        if not isinstance(minute_payload, dict):
            return []
        # expected_date 模式下，电脑自然日只是第一道护栏；响应本身的
        # 证券交易日也必须存在且完全匹配，不能把旧缓存标成目标日证据。
        if normalized_expected_date is not None:
            response_date = str(minute_payload.get("date") or "").strip().replace("-", "")
            if len(response_date) != 8 or not response_date.isdigit():
                return []
            try:
                response_date = datetime.strptime(response_date, "%Y%m%d").strftime("%Y%m%d")
            except ValueError:
                return []
            if response_date != normalized_expected_date:
                return []
        m_lines = minute_payload.get("data")
        if not isinstance(m_lines, list):
            return []
        return m_lines
    except Exception as e:
        print(f"获取分时失败: {e}", file=sys.stderr)
        return []

def fetch_daily_kline(code: str, count: int = 10) -> List[Dict[str, Any]]:
    """查询近期前复权日K线（主机列表共用 tencent_kline，且要求确实是前复权数据）"""
    sym = normalize_code(code)
    try:
        # require_qfq=True：本函数对外承诺"前复权"，因此拒绝腾讯的未复权 day 回退，
        # 宁可报错也不把未复权价当复权价展示。
        payload, _ = tencent_kline.fetch_kline_json(sym, count + 1, require_qfq=True, timeout=10)
        days = tencent_kline.kline_rows(payload, sym, require_qfq=True)
        # format: [date, open, close, high, low, volume, ...]
        out = []
        for i in range(len(days)):
            d = days[i]
            close = float(d[2])
            prev_close = float(days[i-1][2]) if i > 0 else float(d[1])
            pct = ((close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
            out.append({
                "date": d[0],
                "open": float(d[1]),
                "close": close,
                "high": float(d[3]),
                "low": float(d[4]),
                "pct": pct,
                "vol": float(d[5])
            })
        return out[-count:]
    except Exception as e:
        print(f"获取日K失败: {e}", file=sys.stderr)
        return []

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="实时行情/分时查询工具")
    parser.add_argument("codes", nargs="+", help="股票代码列表，如 600188 000768")
    parser.add_argument("--minute", action="store_true", help="显示尾盘分时明细")
    parser.add_argument("--kline", action="store_true", help="显示近期日K")
    args = parser.parse_args()

    quotes = fetch_realtime_quotes(args.codes)
    for code, q in quotes.items():
        print(f"==================================================")
        print(f"【{q['name']}】({q['code']}) 现价: {q['price']:.2f} ({q['pct']:+.2f}%)")
        print(f"昨收: {q['pre_close']:.2f} | 今开: {q['open']:.2f} | 最高: {q['high']:.2f} | 最低: {q['low']:.2f}")
        print(f"成交额: {q['amount_wan']:.0f} 万元 | 时间: {q['time']}")
        print(f"\n[买卖五档盘口]")
        for i in range(4, -1, -1):
            sp, sv = q['sell_orders'][i]
            print(f"  卖{i+1}: {sp:.2f}  ({sv}手)")
        print("  -------------------")
        for i in range(5):
            bp, bv = q['buy_orders'][i]
            print(f"  买{i+1}: {bp:.2f}  ({bv}手)")

        if args.minute:
            m_data = fetch_minute_data(code)
            print(f"\n[尾盘分时末尾 8 分钟]")
            for line in m_data[-8:]:
                print(f"  {line}")

        if args.kline:
            k_data = fetch_daily_kline(code, count=6)
            print(f"\n[近期日K]")
            for k in k_data:
                print(f"  {k['date']}: 收盘 {k['close']:.2f} ({k['pct']:+.2f}%) 开 {k['open']:.2f} 高 {k['high']:.2f} 低 {k['low']:.2f}")
