"""Official SZSE monthly trading calendar with explicit unknown states."""

from __future__ import annotations

import calendar as calendar_module
from datetime import date, datetime, time, timedelta, timezone
from collections.abc import Mapping
import time as time_module
from typing import Any

from .cache import JsonCache
from .contracts import Result, ResultStatus, result_error, result_ok
from .http import HTTPClient, HTTPClientError
from .symbols import validate_ymd


SZSE_CALENDAR_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
CALENDAR_SOURCE = "szse_official_calendar"
BEIJING = timezone(timedelta(hours=8))
SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))


def _month(value: str | date | datetime) -> tuple[int, int]:
    if isinstance(value, datetime):
        return value.year, value.month
    if isinstance(value, date):
        return value.year, value.month
    normalized = validate_ymd(str(value))
    parsed = date.fromisoformat(normalized)
    return parsed.year, parsed.month


def _day(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return validate_ymd(raw[:10])
    return validate_ymd(raw)


def parse_calendar_payload(payload: Mapping[str, Any], *, year: int, month: int, source_url: str = SZSE_CALENDAR_URL) -> list[dict[str, Any]]:
    """Validate a complete natural month from SZSE's ``monthList`` payload."""
    if type(year) is not int or type(month) is not int or not 1 <= month <= 12:
        raise ValueError("year/month 必须是合法整数")
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list) or not data:
        raise ValueError("深交所未返回该月完整日历")
    last_day = calendar_module.monthrange(year, month)[1]
    expected = {date(year, month, day).isoformat() for day in range(1, last_day + 1)}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise ValueError("深交所日历含非对象记录")
        day = _day(item.get("jyrq") or item.get("date"))
        if day[:7] != f"{year:04d}-{month:02d}":
            raise ValueError(f"深交所日历月份错位: {day}")
        flag = str(item.get("jybz") if item.get("jybz") is not None else item.get("is_open") or "").strip()
        if flag not in {"0", "1", "True", "False", "true", "false"}:
            raise ValueError(f"深交所日历交易标记异常: {flag!r}")
        if day in seen:
            raise ValueError(f"深交所日历重复日期: {day}")
        seen.add(day)
        rows.append({
            "date": day,
            "is_open": flag in {"1", "True", "true"},
            "source": CALENDAR_SOURCE,
            "source_url": source_url,
        })
    if seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise ValueError(f"深交所日历不完整: 缺 {missing[:3]}，多 {extra[:3]}")
    return sorted(rows, key=lambda row: row["date"])


class TradingCalendarService:
    """Fetch/cache official months and expose date/session lookups."""

    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None, cache_ttl: int = 7 * 24 * 3600, request_timeout: float = 10.0):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("szse_calendar")
        self.cache_ttl = max(60, int(cache_ttl))
        self.request_timeout = max(0.5, float(request_timeout))

    def fetch_month(self, year: int, month: int, *, force: bool = False, deadline: float | None = None) -> Result:
        if type(year) is not int or type(month) is not int or not 1 <= month <= 12:
            return result_error(ResultStatus.UNSUPPORTED, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="invalid_month", message="year/month 不合法")
        key = self.cache.key({"year": year, "month": month})
        if not force:
            cached = self.cache.get(key, ttl=self.cache_ttl)
            if cached and isinstance(cached.value, dict) and isinstance(cached.value.get("rows"), list):
                return Result(
                    status=ResultStatus.OK,
                    data=cached.value["rows"],
                    source=CALENDAR_SOURCE,
                    source_url=SZSE_CALENDAR_URL,
                    data_date=f"{year:04d}-{month:02d}",
                    freshness="cached",
                    cache={"hit": True},
                )
        try:
            if deadline is not None and time_module.monotonic() >= deadline:
                return result_error(ResultStatus.UNAVAILABLE, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="background_budget_exceeded", message="交易日历查询超过背景预算", retryable=True)
            timeout = self.request_timeout
            if deadline is not None:
                timeout = min(timeout, max(0.1, deadline - time_module.monotonic()))
            response = self.client.get(SZSE_CALENDAR_URL, params={"month": f"{year}-{month:02d}"}, headers={"Referer": "https://www.szse.cn/"}, timeout=timeout, retries=1)
            if deadline is not None and time_module.monotonic() >= deadline:
                return result_error(ResultStatus.UNAVAILABLE, source=CALENDAR_SOURCE, source_url=response.url, code="background_budget_exceeded", message="交易日历查询超过背景预算", retryable=True)
            rows = parse_calendar_payload(response.json(), year=year, month=month, source_url=response.url)
            self.cache.set(key, {"rows": rows}, ttl=self.cache_ttl, data_date=f"{year:04d}-{month:02d}")
            return result_ok(rows, source=CALENDAR_SOURCE, source_url=response.url, data_date=f"{year:04d}-{month:02d}", cache={"hit": False}, request_count=self.client.request_count)
        except HTTPClientError as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code=exc.code, message=str(exc), retryable=exc.retryable)
        except (TypeError, ValueError, KeyError) as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="calendar_incomplete", message=str(exc), retryable=True)

    def is_open(self, value: str | date | datetime, *, force: bool = False, deadline: float | None = None) -> Result:
        try:
            normalized = validate_ymd(value.isoformat() if isinstance(value, (date, datetime)) else str(value))
            year, month = _month(normalized)
        except (TypeError, ValueError) as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="invalid_date", message=str(exc))
        month_result = self.fetch_month(year, month, force=force, deadline=deadline)
        if month_result.status != ResultStatus.OK.value:
            return month_result
        found = next((row for row in month_result.data if row.get("date") == normalized), None)
        if not isinstance(found, dict):
            return result_error(ResultStatus.UNAVAILABLE, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="date_missing", message=f"日历没有 {normalized}")
        return Result(status=ResultStatus.OK, data={"date": normalized, "is_open": bool(found["is_open"])}, source=CALENDAR_SOURCE, source_url=month_result.source_url, data_date=normalized, freshness=month_result.freshness, cache=month_result.cache)

    def next_trading_day(self, value: str | date | datetime, *, include_value: bool = False, max_days: int = 370, force: bool = False) -> Result:
        try:
            start = date.fromisoformat(validate_ymd(value.isoformat() if isinstance(value, (date, datetime)) else str(value)))
        except (TypeError, ValueError) as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="invalid_date", message=str(exc))
        current = start
        for _ in range(max(1, int(max_days))):
            check = self.is_open(current, force=force)
            if check.status != ResultStatus.OK.value:
                return check
            if check.data.get("is_open") and (include_value or current != start):
                return Result(status=ResultStatus.OK, data={"date": current.isoformat(), "is_open": True}, source=check.source, source_url=check.source_url, data_date=current.isoformat(), freshness=check.freshness, cache=check.cache)
            current += timedelta(days=1)
        return result_error(ResultStatus.UNAVAILABLE, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="next_day_limit", message="在搜索范围内没有找到下一交易日")

    def next_session(self, value: datetime | str | None = None, *, force: bool = False) -> Result:
        if value is None:
            moment = datetime.now(BEIJING)
        elif isinstance(value, datetime):
            moment = value if value.tzinfo else value.replace(tzinfo=BEIJING)
            moment = moment.astimezone(BEIJING)
        else:
            try:
                moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                moment = (moment if moment.tzinfo else moment.replace(tzinfo=BEIJING)).astimezone(BEIJING)
            except ValueError as exc:
                return result_error(ResultStatus.UNSUPPORTED, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="invalid_datetime", message=str(exc))
        day = moment.date()
        for _ in range(370):
            open_result = self.is_open(day, force=force)
            if open_result.status != ResultStatus.OK.value:
                return open_result
            if open_result.data.get("is_open"):
                for start, end in SESSIONS:
                    start_dt = datetime.combine(day, start, tzinfo=BEIJING)
                    end_dt = datetime.combine(day, end, tzinfo=BEIJING)
                    if moment < start_dt:
                        return Result(status=ResultStatus.OK, data={"date": day.isoformat(), "session": "morning" if start.hour < 12 else "afternoon", "start": start_dt.isoformat(), "end": end_dt.isoformat()}, source=CALENDAR_SOURCE, source_url=open_result.source_url, data_date=day.isoformat(), freshness=open_result.freshness, cache=open_result.cache)
                    if start_dt <= moment <= end_dt:
                        return Result(status=ResultStatus.OK, data={"date": day.isoformat(), "session": "morning" if start.hour < 12 else "afternoon", "start": start_dt.isoformat(), "end": end_dt.isoformat()}, source=CALENDAR_SOURCE, source_url=open_result.source_url, data_date=day.isoformat(), freshness=open_result.freshness, cache=open_result.cache)
            day += timedelta(days=1)
            moment = datetime.combine(day, time.min, tzinfo=BEIJING)
        return result_error(ResultStatus.UNAVAILABLE, source=CALENDAR_SOURCE, source_url=SZSE_CALENDAR_URL, code="next_session_limit", message="在搜索范围内没有找到下一交易时段")
