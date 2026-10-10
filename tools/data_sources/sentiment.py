"""Market-wide sentiment evidence, kept separate from screening permissions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import math
import time
from typing import Any

from .cache import JsonCache, coalesced_fetch, result_cache_value, result_from_cache
from .contracts import Result, ResultStatus, result_empty, result_error, result_ok
from .http import HTTPClient, HTTPClientError
from .symbols import validate_ymd


SENTIMENT_SOURCE = "eastmoney_sentiment_pools"
SENTIMENT_BASE = "https://push2ex.eastmoney.com"
POOL_URLS = {
    "limit_up": f"{SENTIMENT_BASE}/getTopicZTPool",
    "broken": f"{SENTIMENT_BASE}/getTopicZBPool",
    "limit_down": f"{SENTIMENT_BASE}/getTopicDTPool",
}
BEIJING = timezone(timedelta(hours=8))


def _number(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _flag(value: Any) -> bool | None:
    if value in (True, 1, "1", "true", "True", "Y", "是"):
        return True
    if value in (False, 0, "0", "false", "False", "N", "否"):
        return False
    return None


def _limit_pct(code: str, name: str) -> float:
    if "ST" in name.upper() or "*ST" in name.upper():
        return 5.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("4", "8", "92")):
        return 30.0
    return 10.0


def normalize_pool_row(row: Mapping[str, Any], *, pool_kind: str, data_date: str | None = None) -> dict[str, Any]:
    code = str(row.get("code") or row.get("SECURITY_CODE") or row.get("f12") or row.get("c") or "").strip()
    name = str(row.get("name") or row.get("SECURITY_NAME_ABBR") or row.get("f14") or row.get("n") or "").strip()
    change = _number(row.get("change_pct") if "change_pct" in row else row.get("f3") if "f3" in row else row.get("zdp"))
    current = _number(row.get("price") if "price" in row else row.get("f2"))
    previous = _number(row.get("pre_close") if "pre_close" in row else row.get("f60"))
    high = _number(row.get("high") if "high" in row else row.get("f15"))
    low = _number(row.get("low") if "low" in row else row.get("f16"))
    limit = _number(row.get("limit_pct")) or _limit_pct(code, name)
    explicit_up = _flag(row.get("is_limit_up"))
    explicit_down = _flag(row.get("is_limit_down"))
    touched = _flag(row.get("touched_limit"))
    if touched is None and high is not None and previous and previous > 0:
        touched = high >= previous * (1.0 + limit / 100.0) - max(0.01, previous * 0.0005)
    if explicit_up is None:
        explicit_up = pool_kind == "limit_up" or change is not None and change >= limit - 0.05
    if explicit_down is None:
        explicit_down = pool_kind == "limit_down" or change is not None and change <= -limit + 0.05
    output = {
        "code": code,
        "name": name,
        "change_pct": change,
        "price": current,
        "pre_close": previous,
        "high": high,
        "low": low,
        "limit_pct": limit,
        "pool_kind": pool_kind,
        "is_limit_up": bool(explicit_up),
        "is_limit_down": bool(explicit_down),
        "touched_limit": bool(touched) if touched is not None else None,
        "yesterday_limit_up": _flag(row.get("yesterday_limit_up") if "yesterday_limit_up" in row else row.get("prev_limit_up")),
        "streak": _number(row.get("streak") if "streak" in row else row.get("lbc")),
        "data_date": data_date or row.get("data_date"),
        "raw": dict(row),
    }
    return output


def _pool_list(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ValueError("情绪接口响应不是对象")
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        for key in ("pool", "diff", "list", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        # A valid response can explicitly contain an empty pool.
        if any(key in data for key in ("pool", "diff", "list", "rows")):
            return []
    for key in ("pool", "diff", "list", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    raise ValueError("情绪接口响应缺少涨跌停池字段")


def parse_sentiment_pool_payload(payload: Mapping[str, Any], *, pool_kind: str, data_date: str | None = None) -> list[dict[str, Any]]:
    if pool_kind not in {"limit_up", "broken", "limit_down"}:
        raise ValueError(f"未知情绪池类型: {pool_kind}")
    return [normalize_pool_row(row, pool_kind=pool_kind, data_date=data_date) for row in _pool_list(payload)]


def calculate_market_sentiment(rows: Sequence[Mapping[str, Any]], *, as_of: str | None = None, scope: str = "provided_rows", source: str = "calculated") -> dict[str, Any]:
    """Calculate transparent metrics from supplied rows without a score."""
    normalized = [normalize_pool_row(row, pool_kind=str(row.get("pool_kind") or "market"), data_date=row.get("data_date")) if "is_limit_up" not in row else dict(row) for row in rows if isinstance(row, Mapping)]
    valid = [row for row in normalized if isinstance(row.get("change_pct"), (int, float))]
    zt = sum(1 for row in normalized if row.get("is_limit_up"))
    dt = sum(1 for row in normalized if row.get("is_limit_down"))
    touched = sum(1 for row in normalized if row.get("touched_limit"))
    broken = sum(1 for row in normalized if row.get("pool_kind") == "broken" or row.get("touched_limit") and not row.get("is_limit_up"))
    denominator = zt + broken
    changes = [float(row["change_pct"]) for row in valid]
    streaks = [float(row["streak"]) for row in normalized if isinstance(row.get("streak"), (int, float))]
    yesterday = [row for row in normalized if row.get("yesterday_limit_up") is True]
    progressed = sum(1 for row in yesterday if row.get("is_limit_up"))
    today_change = [float(row["change_pct"]) for row in yesterday if isinstance(row.get("change_pct"), (int, float))]
    ladder: dict[str, int] = {}
    for streak in streaks:
        label = str(int(streak)) if float(streak).is_integer() else str(streak)
        ladder[label] = ladder.get(label, 0) + 1
    return {
        "scope": scope,
        "as_of": as_of,
        "source": source,
        "row_count": len(normalized),
        "valid_change_count": len(valid),
        "missing_change_count": len(normalized) - len(valid),
        "advancing_count": sum(1 for value in changes if value > 0),
        "declining_count": sum(1 for value in changes if value < 0),
        "flat_count": sum(1 for value in changes if value == 0),
        "limit_up_count": zt,
        "limit_down_count": dt,
        "touched_limit_count": touched,
        "broken_count": broken,
        "break_rate": round(broken / denominator * 100.0, 2) if denominator else None,
        "max_streak": max(streaks) if streaks else None,
        "ladder": ladder,
        "yesterday_limit_up_count": len(yesterday),
        "yesterday_limit_up_today_limit_up_count": progressed,
        "promotion_rate": round(progressed / len(yesterday) * 100.0, 2) if yesterday else None,
        "yesterday_limit_up_today_average_change_pct": round(sum(today_change) / len(today_change), 2) if today_change else None,
        "coverage": {
            "scope": scope,
            "denominator": len(normalized),
            "is_full_market": scope == "full_market",
            "note": "仅统计传入范围；接口失败或覆盖不足不等同于全市场空池",
        },
        "metric_definitions": {
            "break_rate": "炸板数 / (涨停数 + 炸板数)；分母为0时为null",
            "promotion_rate": "昨日涨停且今日仍涨停 / 有昨日涨停标记的样本；没有样本时为null",
            "limit_rule": "ST 5%，创业板/科创板20%，北交所30%，其余主板10%；优先使用源标记",
        },
    }


class EastmoneySentimentSource:
    """Low-frequency pool fetcher; failed pools are reported independently."""

    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None, cache_ttl: int = 90, request_timeout: float = 10.0):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("eastmoney_sentiment")
        self.cache_ttl = max(30, int(cache_ttl))
        self.request_timeout = max(0.5, float(request_timeout))

    @coalesced_fetch("sentiment")
    def fetch(self, data_date: str | None = None, *, force: bool = False, page_size: int = 1000, deadline: float | None = None) -> Result:
        if data_date is None:
            data_date = datetime.now(BEIJING).date().isoformat()
        try:
            data_date = validate_ymd(data_date)
        except ValueError as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=SENTIMENT_SOURCE, source_url=SENTIMENT_BASE, code="invalid_date", message=str(exc))
        key = self.cache.key({"date": data_date, "page_size": page_size})
        if not force:
            cached = self.cache.get(key, ttl=self.cache_ttl)
            restored = result_from_cache(cached.value, default_source=SENTIMENT_SOURCE, default_source_url=SENTIMENT_BASE) if cached else None
            if restored is not None:
                return restored
        pools: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        urls: dict[str, str] = {}
        for pool_kind, url in POOL_URLS.items():
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                timeout = self.request_timeout
                if deadline is not None:
                    timeout = min(timeout, max(0.1, deadline - time.monotonic()))
                response = self.client.get(url, params={"ut": "7eea3edcaed734bea9c7b7f85d5b38", "dpt": "wz.ztzt", "pageindex": "1", "pagesize": str(page_size), "sort": "fbt:asc", "date": data_date.replace("-", "")}, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=timeout, retries=1)
                if deadline is not None and time.monotonic() >= deadline:
                    break
                pools[pool_kind] = parse_sentiment_pool_payload(response.json(), pool_kind=pool_kind, data_date=data_date)
                urls[pool_kind] = response.url
            except HTTPClientError as exc:
                errors.append(f"{pool_kind}:{exc.code}")
            except (TypeError, ValueError, KeyError) as exc:
                errors.append(f"{pool_kind}:malformed:{exc}")
        if deadline is not None and time.monotonic() >= deadline:
            return result_error(ResultStatus.UNAVAILABLE, source=SENTIMENT_SOURCE, source_url=SENTIMENT_BASE, code="background_budget_exceeded", message="情绪查询超过背景预算", warnings=errors or ["情绪查询已限时"], retryable=True, data_date=data_date)
        if not pools:
            failure = result_error(ResultStatus.UNAVAILABLE, source=SENTIMENT_SOURCE, source_url=SENTIMENT_BASE, code="all_pools_failed", message="涨停/炸板/跌停池均不可用", warnings=errors, retryable=True, data_date=data_date)
            self.cache.set(key, result_cache_value(failure), ttl=min(self.cache_ttl, 30), data_date=data_date)
            return failure
        combined = [row for rows in pools.values() for row in rows]
        # A stock can appear in more than one source list; retain pool_kind in
        # the evidence rows but calculate distinct counts by code where useful.
        metrics = calculate_market_sentiment(combined, as_of=datetime.now(BEIJING).isoformat(timespec="seconds"), scope="eastmoney_pools", source=SENTIMENT_SOURCE)
        data = {"pools": pools, "metrics": metrics, "pool_status": {kind: "ok" if kind in pools else "unavailable" for kind in POOL_URLS}}
        status = ResultStatus.PARTIAL if errors else ResultStatus.OK
        result = Result(status=status, data=data, source=SENTIMENT_SOURCE, source_url=SENTIMENT_BASE, data_date=data_date, as_of=metrics["as_of"], freshness="fresh", warnings=errors, cache={"hit": False}, request_count=self.client.request_count)
        self.cache.set(key, result_cache_value(result), ttl=self.cache_ttl, data_date=data_date)
        return result
