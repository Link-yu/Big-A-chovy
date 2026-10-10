#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T+1 纪律与样本验证工具
对照前日候选标的/观察池与次日 09:45 卖出窗口走势，统计盈亏比与规则兑现率。
"""

import os
import sys
import argparse
import inspect
import math
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
for import_path in (PROJECT_ROOT, TOOLS_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from report_parser import parse_screening_report, get_report_files
from query_quote import fetch_minute_data, fetch_realtime_quotes, normalize_code
from tools.rule_config import RULE_CONFIG, normalize_hhmm
from tools.data_sources.calendar import TradingCalendarService

BASE_REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_TRADING_CALENDAR: Optional[TradingCalendarService] = None


def _normalize_date(value: Any) -> Optional[str]:
    """Normalize YYYYMMDD/YYYY-MM-DD and reject invalid calendar dates."""
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        return None


def _target_time_text() -> str:
    target_time = normalize_hhmm(RULE_CONFIG["execution"]["t1_exit_window"]["target"])
    return f"{target_time[:2]}:{target_time[2:]}"


def _trading_calendar() -> TradingCalendarService:
    global _TRADING_CALENDAR
    if _TRADING_CALENDAR is None:
        _TRADING_CALENDAR = TradingCalendarService()
    return _TRADING_CALENDAR


def _verified_next_trading_date(report_date: str) -> Optional[str]:
    """Return the official next exchange date, or None when it is unverified."""
    normalized = _normalize_date(report_date)
    if not normalized:
        return None
    try:
        start = datetime.strptime(normalized, "%Y%m%d").date()
        result = _trading_calendar().next_trading_day(start, max_days=370)
        if getattr(result, "status", "") != "ok":
            return None
        data = getattr(result, "data", None)
        if not isinstance(data, dict) or data.get("is_open") is not True:
            return None
        target = _normalize_date(data.get("date"))
        if not target or target == normalized:
            return None
        return target
    except Exception:
        return None


def _current_market_date() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")


def _unavailable_t1_result(
    *,
    report_date: Optional[str],
    target_date: Optional[str],
    reason: str,
    target_date_verified: bool = False,
    data_date: Optional[str] = None,
    morning_prices: Optional[List[float]] = None,
) -> Dict[str, Any]:
    prices = morning_prices or []
    return {
        "status": "unavailable",
        "verified": False,
        "reason": reason,
        "report_date": report_date,
        "target_date": target_date,
        "target_date_verified": target_date_verified,
        "data_date": data_date,
        "target_time": _target_time_text(),
        "target_snapshot_found": False,
        "price_945": None,
        "morning_high": max(prices) if prices else None,
        "morning_low": min(prices) if prices else None,
        "open_price": prices[0] if prices else None,
    }


def _fetch_minute_data_for_date(code: str, target_date: str) -> List[str]:
    """Call only a minute adapter that can enforce the requested date."""
    try:
        parameters = inspect.signature(fetch_minute_data).parameters.values()
        supports_expected_date = any(
            parameter.name == "expected_date"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_expected_date = True
    if not supports_expected_date:
        # Ordinary quote callers may still use the historical one-argument
        # API, but T+1 evidence must never fall back to an unbound adapter.
        return []
    return fetch_minute_data(code, expected_date=target_date)


def get_morning_945_price(code: str, report_date: Optional[str] = None) -> Dict[str, Any]:
    """获取已验证 T+1 交易日的精确目标时刻价格与早盘极值。

    The one-argument form remains callable for compatibility, but it now
    returns an explicit unavailable result because a code alone cannot
    establish which report date or T+1 session is being verified.
    """
    target_time = normalize_hhmm(RULE_CONFIG["execution"]["t1_exit_window"]["target"])
    window_start = normalize_hhmm(RULE_CONFIG["execution"]["t1_exit_window"]["start"])
    window_end = normalize_hhmm(RULE_CONFIG["execution"]["t1_exit_window"]["end"])

    if report_date is None:
        return _unavailable_t1_result(
            report_date=None,
            target_date=None,
            reason="缺少报告日期，无法确认 T+1 目标交易日",
        )

    normalized_report_date = _normalize_date(report_date)
    if not normalized_report_date:
        return _unavailable_t1_result(
            report_date=str(report_date),
            target_date=None,
            reason="报告日期无效，无法确认 T+1 目标交易日",
        )

    target_date = _verified_next_trading_date(normalized_report_date)
    if not target_date:
        return _unavailable_t1_result(
            report_date=normalized_report_date,
            target_date=None,
            reason="无法通过可靠交易日历确认下一交易日",
        )

    current_date = _current_market_date()
    if target_date != current_date:
        return _unavailable_t1_result(
            report_date=normalized_report_date,
            target_date=target_date,
            target_date_verified=True,
            reason=(
                f"分钟行情接口仅提供当前交易日数据，目标日 {target_date} "
                f"与当前交易日 {current_date} 不一致"
            ),
        )

    try:
        m_lines = _fetch_minute_data_for_date(code, target_date)
    except Exception as exc:
        return _unavailable_t1_result(
            report_date=normalized_report_date,
            target_date=target_date,
            target_date_verified=True,
            reason=f"获取目标交易日分钟行情失败: {type(exc).__name__}: {exc}",
        )

    if not m_lines:
        return _unavailable_t1_result(
            report_date=normalized_report_date,
            target_date=target_date,
            target_date_verified=True,
            reason=f"未获取到目标交易日 {target_date} 的分钟行情",
        )

    prices_morning: List[float] = []
    price_945: Optional[float] = None
    for line in m_lines:
        parts = str(line).strip().split()
        if len(parts) < 2:
            continue
        try:
            t = normalize_hhmm(parts[0])
            price = float(parts[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        if window_start <= t <= window_end:
            prices_morning.append(price)
            if t == target_time and price_945 is None:
                price_945 = price

    if price_945 is None:
        return _unavailable_t1_result(
            report_date=normalized_report_date,
            target_date=target_date,
            target_date_verified=True,
            data_date=target_date,
            morning_prices=prices_morning,
            reason=f"目标交易日缺少精确 {_target_time_text()} 分钟快照，不可验证",
        )

    return {
        "status": "verified",
        "verified": True,
        "reason": "",
        "report_date": normalized_report_date,
        "target_date": target_date,
        "target_date_verified": True,
        "data_date": target_date,
        "target_time": _target_time_text(),
        "target_snapshot_found": True,
        "price_945": price_945,
        "morning_high": max(prices_morning),
        "morning_low": min(prices_morning),
        "open_price": prices_morning[0],
    }

def verify_watchlist_t1(date_str: str):
    """验证某日收盘观察池在次日早盘的表现"""
    normalized_date = _normalize_date(date_str)
    lookup_date = normalized_date or date_str
    files = get_report_files(BASE_REPORTS_DIR, lookup_date)
    if not files:
        print(f"未找到报告: {lookup_date}")
        return
    
    last_file = files[-1]
    rep = parse_screening_report(last_file)
    watchlist = rep["tables"]["tomorrow_watchlist"]
    
    if not watchlist:
        print(f"[{lookup_date}] 收盘报告中无明日观察池标的。")
        return
        
    print(f"=== 验证 [{lookup_date}] 收盘明日观察池标的 T+1 表现 ===")
    print(f"报告: {rep['file']}\n")
    
    for row in watchlist:
        code = row.get("代码", "")
        name = row.get("名称", "")
        price_str = row.get("当前价", "0")
        lowzone = row.get("低吸区", "")
        trig = row.get("触发价", "")
        
        try:
            buy_price = float(price_str)
        except ValueError:
            buy_price = 0.0
            
        t1_info = get_morning_945_price(code, lookup_date)
        p945 = t1_info.get("price_945")
        m_high = t1_info.get("morning_high")
        
        if p945 is not None and buy_price > 0 and t1_info.get("verified") is True:
            diff_pct = (p945 - buy_price) / buy_price * 100.0
            max_pct = ((m_high - buy_price) / buy_price * 100.0) if m_high else diff_pct
            print(f"标的: {code} {name}")
            print(f"  前日收盘: {buy_price:.2f} | 低吸区: {lowzone} | 触发价: {trig}")
            print(f"  T+1 {t1_info['target_date']} {t1_info['target_time']}分时价: {p945:.2f} ({diff_pct:+.2f}%) | 早盘最高: {m_high:.2f} ({max_pct:+.2f}%)")
            status = "✅ 达标+2%止盈" if max_pct >= 2.0 else ("⚠️ 浮亏/止损" if diff_pct < -3.0 else "➖ 震荡平出")
            print(f"  判定: {status}\n")
        else:
            reason = t1_info.get("reason") or "目标时刻证据不足"
            print(f"标的: {code} {name} (前日收: {price_str}, T+1不可验证: {reason})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="T+1 表现与早盘窗口验证")
    parser.add_argument("date", type=str, help="基准日期 YYYYMMDD")
    args = parser.parse_args()

    verify_watchlist_t1(args.date)
