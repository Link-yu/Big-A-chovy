#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/query_chips.py - 主力筹码与分时价格-成交量分布 (Volume-by-Price / Chip Distribution) 实时查询工具

用法：
    python3 tools/query_chips.py 600522
    python3 tools/query_chips.py 600522 600722 603897
    python3 tools/query_chips.py 600522 --buckets 15
"""

import sys
import json
import urllib.request
import argparse
import csv
import math
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "daily-stock-analysis" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import tls_context  # noqa: E402  TLS 校验上下文唯一来源（默认校验证书）

ssl_ctx = tls_context.build_context()

def normalize_code(code: str) -> str:
    code_clean = code.strip().lower()
    if code_clean.startswith("sh") or code_clean.startswith("sz") or code_clean.startswith("bj"):
        return code_clean
    if code_clean.startswith("6") or code_clean.startswith("9"):
        return f"sh{code_clean}"
    elif code_clean.startswith("0") or code_clean.startswith("3"):
        return f"sz{code_clean}"
    return f"sh{code_clean}"

def fetch_minute_data(sym: str):
    """通过腾讯金融 API 获取全天分钟明细"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sub_key = list(data.get("data", {}).keys())[0]
        m_lines = data["data"][sub_key]["data"]["data"]
        # Also get name and pre_close from qt API
        return m_lines
    except Exception as e:
        return None

def fetch_quote_info(sym: str):
    """获取股票基本信息与昨收"""
    url = f"https://qt.gtimg.cn/q={sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as resp:
            content = resp.read().decode("gbk", errors="ignore")
        parts = content.split("~")
        if len(parts) > 35:
            return {
                "name": parts[1],
                "code": parts[2],
                "price": float(parts[3]),
                "pre_close": float(parts[4]),
                "high": float(parts[33]),
                "low": float(parts[34]),
                "turnover_pct": float(parts[38]) if parts[38] else 0.0,
                "amount_wan": float(parts[37]) if parts[37] else 0.0,
            }
    except Exception:
        pass
    return None


def _history_value(row: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row:
            return row[name]
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _history_number(value: Any, field: str) -> float:
    if value in (None, "", "-", "--", "null", "None"):
        raise ValueError(f"历史筹码估算缺少 {field}")
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"历史筹码估算 {field} 不是数字: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"历史筹码估算 {field} 不是有限数值")
    return number


def _history_date(value: Any) -> str:
    raw = str(value or "").strip()[:10]
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError(f"历史筹码估算日期异常: {value!r}") from exc


def _triangular_weights(grid: list[float], low: float, high: float, close: float) -> list[float]:
    """Spread one day's turnover across its OHLC price interval."""
    if high - low <= 1e-12:
        nearest = min(range(len(grid)), key=lambda index: abs(grid[index] - close))
        weights = [0.0] * len(grid)
        weights[nearest] = 1.0
        return weights
    radius = max(high - low, (grid[1] - grid[0]) if len(grid) > 1 else 0.01)
    values = []
    for price in grid:
        if price < low - 1e-12 or price > high + 1e-12:
            values.append(0.0)
        else:
            values.append(max(0.0, 1.0 - abs(price - close) / radius))
    total = sum(values)
    if total <= 0:
        nearest = min(range(len(grid)), key=lambda index: abs(grid[index] - close))
        values[nearest] = 1.0
        total = 1.0
    return [value / total for value in values]


def _distribution_quantile(grid: list[float], weights: list[float], quantile: float) -> float:
    target = min(1.0, max(0.0, quantile))
    running = 0.0
    for price, weight in zip(grid, weights):
        running += weight
        if running + 1e-12 >= target:
            return price
    return grid[-1]


def estimate_historical_chips(
    rows: Iterable[Mapping[str, Any]],
    *,
    grid_size: int = 300,
    decay: float = 1.0,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Estimate a cross-day chip distribution from OHLC and turnover.

    This is a transparent research model, not a broker's real position or
    "main-force cost" feed.  The first day seeds the existing float with a
    triangular price distribution.  Each later day retains the previous
    distribution after turnover decay and adds that day's triangular turnover
    distribution.  Rows are sorted by date and can be explicitly capped by
    ``as_of`` so later input cannot leak into an earlier research snapshot.
    """
    if not isinstance(grid_size, int) or grid_size < 20 or grid_size > 5000:
        raise ValueError("grid_size 必须在 20–5000 之间")
    try:
        decay_value = float(decay)
    except (TypeError, ValueError) as exc:
        raise ValueError("decay 必须是数字") from exc
    if not math.isfinite(decay_value) or not 0.0 <= decay_value <= 1.0:
        raise ValueError("decay 必须在 0–1 之间")
    cutoff = _history_date(as_of) if as_of else None

    prepared: list[dict[str, Any]] = []
    adjustment_values: list[str] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("历史筹码输入必须是对象数组")
        row_date = _history_date(_history_value(raw, ("date", "data_date", "交易日期")))
        if cutoff and row_date > cutoff:
            continue
        high = _history_number(_history_value(raw, ("high", "最高", "最高价")), "high")
        low = _history_number(_history_value(raw, ("low", "最低", "最低价")), "low")
        close = _history_number(_history_value(raw, ("close", "收盘", "收盘价")), "close")
        turn = _history_number(_history_value(raw, ("turn", "turnover_pct", "turnover", "换手率")), "换手率(turn)")
        if low <= 0 or high < low or not low <= close <= high:
            raise ValueError(f"历史筹码估算 {row_date} 的 OHLC 关系异常")
        if turn < 0 or turn > 100:
            raise ValueError(f"历史筹码估算 {row_date} 的换手率超出百分数范围: {turn}")
        adjustment = _history_value(raw, ("adjustment", "adjust", "adjustment_type", "复权"))
        if adjustment not in (None, ""):
            adjustment_values.append(str(adjustment).strip().lower())
        prepared.append({"date": row_date, "high": high, "low": low, "close": close, "turn": turn, "adjustment": adjustment})
    if not prepared:
        raise ValueError("历史筹码估算没有可用日期")
    if adjustment_values and len(adjustment_values) != len(prepared):
        raise ValueError("历史筹码估算的复权口径只在部分行提供，拒绝混用")
    if len(set(adjustment_values)) > 1:
        raise ValueError(f"历史筹码估算复权口径不一致: {sorted(set(adjustment_values))}")
    prepared.sort(key=lambda row: row["date"])
    if len({row["date"] for row in prepared}) != len(prepared):
        raise ValueError("历史筹码估算包含重复交易日")

    minimum = min(row["low"] for row in prepared)
    maximum = max(row["high"] for row in prepared)
    if maximum - minimum <= 1e-12:
        pad = max(0.005, minimum * 0.001)
        minimum, maximum = max(0.01, minimum - pad), minimum + pad
    step = (maximum - minimum) / (grid_size - 1)
    grid = [minimum + step * index for index in range(grid_size)]
    distribution = [0.0] * grid_size

    daily_trace: list[dict[str, Any]] = []
    for index, row in enumerate(prepared):
        day_distribution = _triangular_weights(grid, row["low"], row["high"], row["close"])
        if index == 0:
            distribution = day_distribution
            retained = 1.0
        else:
            turnover_fraction = min(1.0, max(0.0, row["turn"] / 100.0))
            retained = max(0.0, min(1.0, (1.0 - turnover_fraction) * decay_value))
            distribution = [
                old * retained + added * turnover_fraction
                for old, added in zip(distribution, day_distribution)
            ]
            total = sum(distribution)
            if total <= 0:
                raise ValueError(f"历史筹码估算 {row['date']} 无法形成有效分布")
            distribution = [value / total for value in distribution]
        daily_trace.append({"date": row["date"], "turnover_pct": row["turn"], "retained_weight": round(retained, 6)})

    last_close = prepared[-1]["close"]
    cost70_low = _distribution_quantile(grid, distribution, 0.15)
    cost70_high = _distribution_quantile(grid, distribution, 0.85)
    cost90_low = _distribution_quantile(grid, distribution, 0.05)
    cost90_high = _distribution_quantile(grid, distribution, 0.95)
    profit_ratio = sum(weight for price, weight in zip(grid, distribution) if price <= last_close + 1e-12)
    average_cost = sum(price * weight for price, weight in zip(grid, distribution))
    concentration = (cost70_high - cost70_low) / last_close * 100.0 if last_close > 0 else None
    peaks = sorted(
        ({"price": round(price, 4), "weight_pct": round(weight * 100.0, 4)} for price, weight in zip(grid, distribution) if weight > 0),
        key=lambda row: row["weight_pct"],
        reverse=True,
    )[:10]
    adjustment = adjustment_values[0] if adjustment_values else None
    return {
        "label": "筹码估算",
        "model": "ohlc_turnover_seed_decay_v1",
        "source": "local_estimate",
        "input_window": {
            "start_date": prepared[0]["date"],
            "end_date": prepared[-1]["date"],
            "row_count": len(prepared),
            "as_of": cutoff,
            "processed_dates": [row["date"] for row in prepared],
        },
        "parameters": {
            "grid_size": grid_size,
            "decay": decay_value,
            "turnover_unit": "percent",
            "price_unit": "yuan",
            "adjustment": adjustment,
            "adjustment_consistency": "consistent" if adjustment else "not_provided",
            "initialization": "first_day_float_seeded_by_triangular_OHLC",
        },
        "last_close": round(last_close, 4),
        "average_cost": round(average_cost, 4),
        "cost_70": {"low": round(cost70_low, 4), "high": round(cost70_high, 4)},
        "cost_90": {"low": round(cost90_low, 4), "high": round(cost90_high, 4)},
        "profit_ratio_pct": round(profit_ratio * 100.0, 4),
        "concentration_70_pct": round(concentration, 4) if concentration is not None else None,
        "peaks": peaks,
        "daily_trace": daily_trace,
        "warnings": [
            "结果为筹码估算，不是主力真实成本或持仓；不能替代分笔、五档或资金证据",
            "估算不进入正式评分、买点、状态机或真实仓权限",
        ],
    }

def query_stock_chips(code: str, num_buckets: int = 12):
    sym = normalize_code(code)
    quote_info = fetch_quote_info(sym)
    m_lines = fetch_minute_data(sym)

    if not m_lines:
        print(f"❌ 股票 {code} 获取分时成交数据失败")
        return

    name = quote_info["name"] if quote_info else code
    pre_close = quote_info["pre_close"] if quote_info else 0.0

    price_vol = {}
    total_vol = 0
    total_amt = 0
    high_p = -1e9
    low_p = 1e9
    latest_p = 0.0
    
    prev_vol = 0
    prev_amt = 0.0

    for l in m_lines:
        # Format: '0930 35.70 28590 102066300.00' (time, price, cum_vol_lots, cum_amt_yuan)
        parts = l.split()
        if len(parts) >= 4:
            p = float(parts[1])
            cum_v = int(parts[2])
            cum_a = float(parts[3])
            
            inc_v = cum_v - prev_vol
            inc_a = cum_a - prev_amt
            prev_vol = cum_v
            prev_amt = cum_a

            price_vol[p] = price_vol.get(p, 0) + inc_v
            total_vol += inc_v
            total_amt += inc_a
            
            if p > high_p: high_p = p
            if p < low_p: low_p = p
            latest_p = p

    if total_vol == 0:
        print(f"⚠️ {name}({code}) 今日暂无有效成交量")
        return

    vwap = (total_amt / (total_vol * 100)) if total_vol else 0.0
    chg_pct = ((latest_p - pre_close) / pre_close * 100) if pre_close > 0 else 0.0

    print("\n" + "=" * 80)
    print(f"📊 【{name} ({code})】主力筹码与分时价格-成交量分布透视 (Volume-by-Price)")
    print(f"  现价: {latest_p:.2f}元 ({chg_pct:+.2f}%) | VWAP均价线: {vwap:.2f}元 | 日内高低: {high_p:.2f} ~ {low_p:.2f}元")
    print(f"  总成交量: {total_vol:,} 手 | 总成交金额: {total_amt/1e8:.2f} 亿元")
    print("=" * 80)

    # 1. 价格区间分桶 (Bucketing)
    p_range = high_p - low_p
    if p_range <= 0.02:
        bucket_size = 0.01
    else:
        bucket_size = max(0.02, round(p_range / num_buckets, 2))

    buckets = {}
    for p, v in price_vol.items():
        b_idx = round(p / bucket_size) * bucket_size
        b_idx = round(b_idx, 2)
        buckets[b_idx] = buckets.get(b_idx, 0) + v

    sorted_b = sorted(buckets.keys(), reverse=True)
    max_b_vol = max(buckets.values()) if buckets else 1
    
    print("\n【📈 今日价格 - 筹码堆积分布带】")
    for b in sorted_b:
        v = buckets[b]
        pct = (v / total_vol) * 100
        amt_b = (v * b * 100) / 1e8
        bar_len = int((v / max_b_vol) * 26)
        bar = "█" * bar_len
        
        mark = ""
        if abs(latest_p - b) <= bucket_size / 2:
            mark += " 👈[现价]"
        if abs(vwap - b) <= bucket_size / 2:
            mark += " ⭐[VWAP均线]"
            
        print(f"  {b-bucket_size/2:5.2f} ~ {b+bucket_size/2:5.2f}元 | 成交 {v:7,d}手 ({pct:5.2f}%) | 沉淀 {amt_b:5.2f}亿 | {bar}{mark}")

    # 2. 单点绝对最大成交量 Top 5 筹码峰
    print("\n【🎯 今日单点绝对成交量 Top 5 核心筹码峰】")
    sorted_single = sorted(price_vol.items(), key=lambda x: x[1], reverse=True)
    for rank, (p, v) in enumerate(sorted_single[:5], 1):
        pct = (v / total_vol) * 100
        amt_single = (v * p * 100) / 1e8
        nature = ""
        if p < vwap - 0.20:
            nature = "【低吸承接/铁底支撑峰】"
        elif abs(p - vwap) <= 0.15:
            nature = "【多空中枢/均价平衡峰】"
        else:
            nature = "【高位分歧/冲高套牢峰】"
        print(f"  {rank}. 🎯 {p:5.2f}元 : 成交 {v:7,d}手 ({pct:5.2f}%) | 沉淀 {amt_single:5.2f}亿元 | {nature}")

    # 3. 三层筹码结构剖析
    low_zone_amt = sum(p * v * 100 for p, v in price_vol.items() if p < vwap - 0.1) / 1e8
    mid_zone_amt = sum(p * v * 100 for p, v in price_vol.items() if abs(p - vwap) <= 0.1) / 1e8
    high_zone_amt = sum(p * v * 100 for p, v in price_vol.items() if p > vwap + 0.1) / 1e8
    
    print("\n【💡 主力三层筹码沉淀总结】")
    print(f"  • 底层支撑吸筹区 (<{vwap-0.1:.2f}元): 沉淀资金 {low_zone_amt:.2f} 亿元 ({(low_zone_amt/(total_amt/1e8))*100:.1f}%)")
    print(f"  • 中层多空中枢区 ({vwap-0.1:.2f}~{vwap+0.1:.2f}元): 沉淀资金 {mid_zone_amt:.2f} 亿元 ({(mid_zone_amt/(total_amt/1e8))*100:.1f}%)")
    print(f"  • 顶层阻力套牢区 (>{vwap+0.1:.2f}元): 沉淀资金 {high_zone_amt:.2f} 亿元 ({(high_zone_amt/(total_amt/1e8))*100:.1f}%)")
    print("=" * 80 + "\n")

def _load_history_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payload = payload["data"]
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("历史 JSON 必须是对象数组，或包含 data 对象数组")
        return payload
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError("历史输入只支持 .json 或 .csv")


def _write_history_output(path: str | None, payload: Any) -> None:
    if not path:
        return
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _history_mode(args: argparse.Namespace) -> int:
    history_path = Path(args.history_json or args.history_csv).expanduser()
    try:
        rows = _load_history_rows(history_path)
        requested_codes = [str(code).strip() for code in args.codes]
        if requested_codes:
            codes = requested_codes
        else:
            codes = sorted({str(row.get("code") or "").split(".")[-1] for row in rows if row.get("code")})
            if not codes:
                codes = [""]
        results = []
        for requested in codes:
            selected = rows
            if requested:
                bare = requested.lower().replace("sh", "").replace("sz", "").replace("bj", "")
                selected = [row for row in rows if str(row.get("code") or "").split(".")[-1] == bare]
            if not selected:
                raise ValueError(f"历史输入中没有 {requested or '目标证券'} 的日线")
            result = estimate_historical_chips(selected, grid_size=args.grid_size, decay=args.decay, as_of=args.as_of)
            if requested:
                result["code"] = requested
            results.append(result)
        payload = {"status": "ok", "source": "local_estimate", "data": results}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        payload = {"status": "unsupported", "source": "local_estimate", "error": {"code": "invalid_history", "message": str(exc)}}
        _write_history_output(args.output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    _write_history_output(args.output, payload)
    if args.json or args.output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for result in results:
            print(
                f"{result.get('code', '') or 'history'} {result['label']} "
                f"{result['input_window']['start_date']}~{result['input_window']['end_date']} "
                f"成本70%={result['cost_70']['low']:.2f}~{result['cost_70']['high']:.2f} "
                f"获利比例={result['profit_ratio_pct']:.2f}% 集中度={result['concentration_70_pct']:.2f}%"
            )
    return 0


def main():
    parser = argparse.ArgumentParser(description="A股主力筹码与分时成交量分布查询工具；可选跨日筹码估算")
    parser.add_argument("codes", nargs="*", help="股票代码，如 600522 600722；历史模式可省略")
    parser.add_argument("--buckets", type=int, default=12, help="价格区间分桶数量，默认12")
    history_group = parser.add_mutually_exclusive_group()
    history_group.add_argument("--history-json", help="历史 OHLC+换手率 JSON 输入，显式启用筹码估算")
    history_group.add_argument("--history-csv", help="历史 OHLC+换手率 CSV 输入，显式启用筹码估算")
    parser.add_argument("--decay", type=float, default=1.0, help="历史筹码换手衰减参数，0–1，默认1.0")
    parser.add_argument("--grid-size", type=int, default=300, help="历史筹码价格网格，默认300")
    parser.add_argument("--as-of", help="历史估算截止日；晚于该日的输入行会被排除")
    parser.add_argument("--json", action="store_true", help="历史模式输出 JSON")
    parser.add_argument("--output", help="历史模式写入明确的本地 JSON 路径")
    args = parser.parse_args()

    if args.history_json or args.history_csv:
        return _history_mode(args)
    if not args.codes:
        parser.error("实时模式至少需要一个股票代码；历史模式请提供 --history-json 或 --history-csv")
    for code in args.codes:
        query_stock_chips(code, num_buckets=args.buckets)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
