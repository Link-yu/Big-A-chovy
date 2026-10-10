"""Tencent quote snapshot and approximately three-second tick adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from typing import Any, Iterable

from .cache import JsonCache
from .contracts import Result, ResultStatus, result_empty, result_error, result_ok
from .http import HTTPClient, HTTPClientError
from .symbols import SecuritySymbol, SymbolError, normalize_security


TICK_URL = "https://stock.gtimg.cn/data/index.php"
QUOTE_URL = "https://qt.gtimg.cn/q="
MAX_PAGES = 300
SESSION_END = "15:00:59"
SESSION_START = "09:30:00"
AUCTION_START = "09:25:00"
TICK_SOURCE = "tencent_ticks"


def _number(value: Any, field: str) -> float:
    if value in (None, "", "-", "--"):
        raise ValueError(f"腾讯分笔字段 {field} 缺失")
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"腾讯分笔字段 {field} 不是数字: {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"腾讯分笔字段 {field} 非有限数值")
    return number


def _time_seconds(value: str) -> int:
    value = str(value or "").strip()
    if re.fullmatch(r"\d{6}", value):
        value = f"{value[:2]}:{value[2:4]}:{value[4:]}"
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        raise ValueError(f"分笔时间格式异常: {value!r}")
    h, m, s = (int(x) for x in value.split(":"))
    if h > 23 or m > 59 or s > 59:
        raise ValueError(f"分笔时间无效: {value!r}")
    return h * 3600 + m * 60 + s


def _validate_raw_tick_rows(rows: Iterable[dict[str, Any]], *, first_page: bool = False) -> list[str]:
    """Check provider order before the merge-by-sequence step can hide it."""
    materialized = list(rows)
    warnings: list[str] = []
    if not materialized:
        return warnings
    seqs = [int(row["seq"]) for row in materialized]
    if len(seqs) != len(set(seqs)):
        warnings.append("分笔原始序号重复")
    if any(current < previous for previous, current in zip(seqs, seqs[1:])):
        warnings.append("分笔原始序号倒退")
    previous_seconds: int | None = None
    for row in materialized:
        seconds = _time_seconds(str(row.get("time") or ""))
        if previous_seconds is not None and seconds < previous_seconds:
            warnings.append("分笔原始时间倒退")
        previous_seconds = seconds
    if first_page and min(seqs) > 1 and _time_seconds(str(materialized[0].get("time") or "")) <= _time_seconds(SESSION_END):
        warnings.append(f"连续竞价缺序号 1–{min(seqs) - 1}")
    return list(dict.fromkeys(warnings))


def parse_snapshot(text: str, symbol: str) -> dict[str, Any]:
    """Parse ``qt.gtimg.cn`` snapshot without silently accepting another code."""
    match = re.search(rf'v_{re.escape(symbol)}="([^"]*)"', text)
    if not match:
        if "v_pv_none_match" in text:
            raise ValueError(f"腾讯没有 {symbol} 这个代码")
        raise RuntimeError(f"腾讯行情快照 {symbol} 未返回预期变量")
    fields = match.group(1).split("~")
    if len(fields) < 36 or not re.fullmatch(r"\d{14}", fields[30] or ""):
        raise RuntimeError(f"腾讯行情快照 {symbol} 字段数/时间字段异常: {len(fields)}")
    parts = (fields[35] or "").split("/")
    if len(parts) != 3:
        raise RuntimeError(f"腾讯行情快照 {symbol} 的价量额字段异常: {fields[35]!r}")
    return {
        "symbol": symbol,
        "code": symbol[2:],
        "name": fields[1],
        "data_date": f"{fields[30][:4]}-{fields[30][4:6]}-{fields[30][6:8]}",
        "as_of": fields[30][8:],
        "price": _number(fields[3], "price"),
        "amount": _number(parts[2], "amount"),
    }


def parse_tick_page(text: str, symbol: str, page: int) -> list[dict[str, Any]] | None:
    """Parse one 70-row page; ``None`` means the provider's end marker."""
    text = text.strip()
    if not text:
        return None
    match = re.fullmatch(rf'v_detail_data_{re.escape(symbol)}=\[(\d+),"([^"]*)"\];?', text)
    if not match or int(match.group(1)) != page:
        raise RuntimeError(f"腾讯逐笔 {symbol} 第 {page} 页返回结构异常: {text[:100]!r}")
    payload = match.group(2)
    if not payload:
        return None
    rows: list[dict[str, Any]] = []
    for raw in payload.split("|"):
        fields = raw.split("/")
        if len(fields) != 7:
            raise RuntimeError(f"腾讯逐笔字段数改变: {raw!r}")
        seq_raw, clock, price, change, volume, amount, side = fields
        _time_seconds(clock)
        if side not in {"B", "S", "M"}:
            raise RuntimeError(f"腾讯逐笔方向未知: {side!r}")
        try:
            seq = int(seq_raw)
        except ValueError as exc:
            raise RuntimeError(f"腾讯逐笔序号不是整数: {seq_raw!r}") from exc
        rows.append({
            "seq": seq,
            "time": clock,
            "price": _number(price, "price"),
            "change": _number(change, "change"),
            "volume": _number(volume, "volume_hand"),
            "amount": _number(amount, "amount_yuan"),
            "side": side,
        })
    return rows


def _format_seconds(value: int) -> str:
    return f"{value // 3600:02d}:{(value % 3600) // 60:02d}:{value % 60:02d}"


def aggregate_ticks(
    rows: Iterable[dict[str, Any]],
    *,
    window_minutes: int = 5,
    as_of: str | None = None,
    source_complete: bool | None = None,
    source_start: str | None = None,
    source_end: str | None = None,
    source_warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Aggregate B/S/M ticks in continuous auction time only.

    The returned ratio is ``B amount / S amount``.  A zero sell amount is
    represented by ``None`` rather than infinity so it cannot accidentally
    satisfy a trading gate.
    """
    prepared = []
    for row in rows:
        clock = str(row.get("time") or "")
        seconds = _time_seconds(clock)
        if 9 * 3600 + 30 * 60 <= seconds <= 15 * 3600 + 59:
            prepared.append((seconds, row))
    prepared.sort(key=lambda item: item[0])
    requested_minutes = max(1, int(window_minutes))
    if not prepared:
        return {
            "window_minutes": requested_minutes,
            "as_of": as_of,
            "row_count": 0,
            "coverage_minutes": 0.0,
            "required_coverage_minutes": 0.0,
            "buy_amount": 0.0,
            "sell_amount": 0.0,
            "neutral_amount": 0.0,
            "net_amount": 0.0,
            "buy_sell_ratio": None,
            "data_sufficient": False,
            "source_coverage_proven": False,
            "coverage_basis": "none",
            "reason": "连续竞价窗口没有有效分笔",
        }
    session_open = _time_seconds(SESSION_START)
    session_close = _time_seconds(SESSION_END)
    as_of_seconds = min(_time_seconds(as_of), session_close) if as_of else min(prepared[-1][0], session_close)
    latest = as_of_seconds
    effective_window_minutes = min(float(requested_minutes), max(0.0, (latest - session_open) / 60.0))
    requested_start = max(session_open, latest - requested_minutes * 60)
    cutoff = requested_start
    selected = [row for seconds, row in prepared if cutoff <= seconds <= latest]
    if not selected:
        return {
            "window_minutes": requested_minutes,
            "as_of": as_of,
            "row_count": 0,
            "coverage_minutes": 0.0,
            "required_coverage_minutes": round(effective_window_minutes, 2),
            "buy_amount": 0.0,
            "sell_amount": 0.0,
            "neutral_amount": 0.0,
            "net_amount": 0.0,
            "buy_sell_ratio": None,
            "data_sufficient": False,
            "source_coverage_proven": False,
            "coverage_basis": "no_selected_rows",
            "source_range_start": source_start,
            "source_range_end": source_end,
            "source_complete": source_complete,
            "reason": f"所选窗口 { _format_seconds(requested_start) }–{_format_seconds(latest)} 没有分笔记录",
        }
    buy = sum(float(row.get("amount") or 0) for row in selected if row.get("side") == "B")
    sell = sum(float(row.get("amount") or 0) for row in selected if row.get("side") == "S")
    neutral = sum(float(row.get("amount") or 0) for row in selected if row.get("side") == "M")
    selected_times = [_time_seconds(str(row["time"])) for row in selected]
    coverage = ((max(selected_times) - min(selected_times)) / 60.0) if len(selected_times) > 1 else 0.0
    observed_start = min(selected_times)
    observed_end = max(selected_times)
    source_start_seconds = _time_seconds(source_start) if source_start else None
    source_end_seconds = min(_time_seconds(source_end), session_close) if source_end else None
    warning_list = [str(item) for item in source_warnings if str(item)]
    source_range_covers = (
        source_complete is True
        and not warning_list
        and source_start_seconds is not None
        and source_end_seconds is not None
        and source_start_seconds <= requested_start
        and source_end_seconds >= latest
    )
    observed_range_covers = (
        source_complete is None
        and observed_start <= requested_start
        and observed_end >= latest
    )
    sufficient = source_range_covers or observed_range_covers
    if source_range_covers:
        coverage_basis = "source_range_complete"
    elif observed_range_covers:
        coverage_basis = "observed_timestamps"
    else:
        coverage_basis = "insufficient_source_range"
    if sufficient:
        reason = ""
    elif source_complete is False:
        reason = "源分笔范围未完整收取，不能证明所选窗口覆盖"
    elif warning_list:
        reason = "源分笔存在完整性警告，不能作为真实仓盘口核验充分证据"
    elif source_complete is True and source_start_seconds is not None:
        reason = f"源覆盖范围不足（观察 {coverage:.2f} 分钟；需要从 {_format_seconds(requested_start)} 起有源证据）"
    else:
        reason = f"窗口覆盖不足且缺少边界成交/源范围证明（观察 {coverage:.2f} 分钟；需要 {effective_window_minutes:.2f} 分钟）"
    return {
        "window_minutes": requested_minutes,
        "as_of": as_of or selected[-1].get("time"),
        "row_count": len(selected),
        "coverage_minutes": round(coverage, 2),
        "required_coverage_minutes": round(effective_window_minutes, 2),
        "buy_amount": round(buy, 2),
        "sell_amount": round(sell, 2),
        "neutral_amount": round(neutral, 2),
        "net_amount": round(buy - sell, 2),
        "buy_sell_ratio": round(buy / sell, 4) if sell > 0 else None,
        "data_sufficient": sufficient,
        "source_coverage_proven": source_range_covers,
        "coverage_basis": coverage_basis,
        "source_range_start": source_start,
        "source_range_end": source_end,
        "source_complete": source_complete,
        "reason": reason,
    }


class TencentTickSource:
    """Fetch and incrementally cache Tencent tick pages."""

    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None, sleep_seconds: float = 0.1):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("tencent_ticks")
        self.sleep_seconds = max(0.0, float(sleep_seconds))

    def _snapshot(self, symbol: SecuritySymbol) -> dict[str, Any]:
        response = self.client.get(
            QUOTE_URL + symbol.tencent,
            headers={"Referer": "https://gu.qq.com/", "Accept": "text/plain"},
            retries=1,
        )
        return parse_snapshot(response.text, symbol.tencent)

    def _page(self, symbol: SecuritySymbol, page: int) -> list[dict[str, Any]] | None:
        response = self.client.get(
            TICK_URL,
            params={"appn": "detail", "action": "data", "c": symbol.tencent, "p": page},
            headers={"Referer": "https://gu.qq.com/", "Accept": "text/plain"},
            retries=0,
        )
        return parse_tick_page(response.text, symbol.tencent, page)

    @staticmethod
    def _merge_rows(base: list[dict[str, Any]], incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        by_seq = {int(row["seq"]): row for row in base}
        for row in incoming:
            by_seq[int(row["seq"])] = row
        return [by_seq[key] for key in sorted(by_seq)]

    def fetch(self, code: str, *, max_pages: int = MAX_PAGES, verify_amount: bool = True, force: bool = False) -> Result:
        try:
            symbol = normalize_security(code)
            if symbol.market == "bj":
                return result_error(ResultStatus.UNSUPPORTED, source=TICK_SOURCE, source_url=TICK_URL, code="unsupported_market", message="腾讯分笔不支持北交所")
            snapshot = self._snapshot(symbol)
            cache_key = self.cache.key({"symbol": symbol.tencent})
            entry = None if force else self.cache.get(cache_key, allow_stale=True)
            cached = entry.value if entry else None
            if not isinstance(cached, dict) or cached.get("data_date") != snapshot["data_date"]:
                cached = None
            base_rows = list(cached.get("rows") or []) if cached else []
            next_page = int(cached.get("next_page") or 0) if cached else 0
            # Re-read the boundary page so an intraday page that grew since the
            # last call is merged without downloading the entire day again.
            start_page = max(0, next_page - 1) if cached else 0
            # Keep all completed pages before the boundary.  The first row of
            # the page being re-read is normally ``start_page * 70 + 1``;
            # retaining the preceding page's final sequence avoids inventing a
            # gap when page sizes are exactly 70.
            rows = [row for row in base_rows if int(row.get("seq", -1)) <= start_page * 70] if cached else []
            missing_seq: list[int] = []
            integrity_warnings: list[str] = []
            complete = False
            page = start_page
            for _ in range(max(1, int(max_pages))):
                page_rows = self._page(symbol, page)
                if page_rows is None:
                    complete = True
                    next_page = page
                    break
                integrity_warnings.extend(_validate_raw_tick_rows(page_rows, first_page=(page == 0)))
                rows = self._merge_rows(rows, page_rows)
                page += 1
                if self.sleep_seconds:
                    time.sleep(self.sleep_seconds)
            else:
                return result_error(ResultStatus.PARTIAL, source=TICK_SOURCE, source_url=TICK_URL, code="page_limit", message=f"翻页超过 {max_pages} 页，结果不完整", data={"rows": rows})

            if not rows:
                status = ResultStatus.EMPTY if _time_seconds(snapshot["as_of"]) < _time_seconds("09:25:00") else ResultStatus.PARTIAL
                return Result(
                    status=status,
                    data={"code": symbol.code, "symbol": symbol.tencent, "rows": [], "windows": {}},
                    source=TICK_SOURCE,
                    source_url=TICK_URL,
                    data_date=snapshot["data_date"],
                    as_of=snapshot["as_of"],
                    freshness="fresh",
                    warnings=["腾讯快照有成交额但分笔为空" if status == ResultStatus.PARTIAL else "尚未撮合"],
                    request_count=self.client.request_count,
                )

            # Validate sequence and time order.  Gaps after the continuous
            # auction are recorded, not treated as a missing live transaction.
            ordered = sorted(rows, key=lambda row: int(row["seq"]))
            warnings: list[str] = list(integrity_warnings)
            seen: set[int] = set()
            last_time_seconds: int | None = None
            expected = 1
            for row in ordered:
                seq = int(row["seq"])
                if seq in seen:
                    warnings.append(f"重复序号 {seq}")
                seen.add(seq)
                if seq > expected:
                    gap = list(range(expected, seq))
                    if _time_seconds(str(row["time"])) <= _time_seconds(SESSION_END):
                        warnings.append(f"连续竞价缺序号 {gap[0]}–{gap[-1]}")
                    else:
                        missing_seq.extend(gap)
                current_time_seconds = _time_seconds(str(row["time"]))
                if last_time_seconds is not None and current_time_seconds < last_time_seconds:
                    warnings.append("分笔时间倒退")
                expected = seq + 1
                last_time_seconds = current_time_seconds

            auction_and_continuous = [
                row for row in ordered
                if _time_seconds(str(row["time"])) >= _time_seconds(AUCTION_START)
                and _time_seconds(str(row["time"])) <= _time_seconds(SESSION_END)
            ]
            continuous = [
                row for row in ordered
                if _time_seconds(str(row["time"])) >= _time_seconds(SESSION_START)
                and _time_seconds(str(row["time"])) <= _time_seconds(SESSION_END)
            ]
            auction_amount = sum(float(row.get("amount") or 0) for row in auction_and_continuous if _time_seconds(str(row["time"])) < _time_seconds(SESSION_START))
            session_amount = sum(float(row.get("amount") or 0) for row in auction_and_continuous)
            continuous_amount = sum(float(row.get("amount") or 0) for row in continuous)
            if verify_amount and snapshot["amount"] > 0 and _time_seconds(snapshot["as_of"]) >= _time_seconds("15:01:00"):
                tolerance = snapshot["amount"] * 0.001 + 1000
                if abs(session_amount - snapshot["amount"]) > tolerance:
                    warnings.append(
                        f"交易时段成交额 {session_amount:.0f}（集合竞价 {auction_amount:.0f} + 连续竞价 {continuous_amount:.0f}）与全日快照 {snapshot['amount']:.0f} 不符，分笔可能不完整"
                    )

            continuous_times = [_time_seconds(str(row["time"])) for row in continuous]
            source_start = _format_seconds(min(continuous_times)) if continuous_times else None
            source_end = snapshot["as_of"] if complete else (_format_seconds(max(continuous_times)) if continuous_times else None)
            source_complete_for_windows = complete and not warnings
            windows = {
                str(minutes): aggregate_ticks(
                    ordered,
                    window_minutes=minutes,
                    as_of=snapshot["as_of"],
                    source_complete=source_complete_for_windows,
                    source_start=source_start,
                    source_end=source_end,
                    source_warnings=warnings,
                )
                for minutes in (5, 15)
            }
            data = {
                "code": symbol.code,
                "symbol": symbol.tencent,
                "name": snapshot.get("name", ""),
                "data_date": snapshot["data_date"],
                "as_of": snapshot["as_of"],
                "rows": ordered,
                "row_count": len(ordered),
                "source_complete": complete,
                "source_sequence_start": int(ordered[0]["seq"]),
                "source_sequence_end": int(ordered[-1]["seq"]),
                "source_range_start": source_start,
                "source_range_end": source_end,
                "auction_amount": round(auction_amount, 2),
                "continuous_amount": round(continuous_amount, 2),
                "session_amount": round(session_amount, 2),
                "snapshot_amount": snapshot["amount"],
                "missing_seq": sorted(set(missing_seq)),
                "windows": windows,
                "integrity_warnings": list(dict.fromkeys(warnings)),
                "note": "腾讯约3秒聚合分笔，不是交易所 Level-2 原始逐笔委托/成交",
            }
            self.cache.set(cache_key, {"data_date": snapshot["data_date"], "rows": ordered, "next_page": next_page, "complete": complete}, ttl=24 * 3600, data_date=snapshot["data_date"])
            warnings = list(dict.fromkeys(warnings))
            if warnings:
                status = ResultStatus.PARTIAL
            else:
                status = ResultStatus.OK
            return Result(
                status=status,
                data=data,
                source=TICK_SOURCE,
                source_url=TICK_URL,
                data_date=snapshot["data_date"],
                as_of=snapshot["as_of"],
                freshness="fresh",
                warnings=warnings,
                request_count=self.client.request_count,
                cache={"hit": bool(cached), "incremental": bool(cached), "next_page": next_page},
            )
        except SymbolError as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=TICK_SOURCE, source_url=TICK_URL, code="invalid_security", message=str(exc))
        except HTTPClientError as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=TICK_SOURCE, source_url=TICK_URL, code=exc.code, message=str(exc), retryable=exc.retryable)
        except (ValueError, RuntimeError) as exc:
            return result_error(ResultStatus.PARTIAL, source=TICK_SOURCE, source_url=TICK_URL, code="parse_or_integrity_error", message=str(exc), retryable=True)
