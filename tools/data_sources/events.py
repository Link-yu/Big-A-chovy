"""Structured, on-demand event evidence from Eastmoney data-center feeds."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any

from .cache import JsonCache, coalesced_fetch, result_cache_value, result_from_cache
from .contracts import Result, ResultStatus, result_error, result_ok
from .http import HTTPClient, HTTPClientError
from .symbols import SymbolError, normalize_security, validate_ymd


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EVENT_SOURCE = "eastmoney_events"
BEIJING = timezone(timedelta(hours=8))

EVENT_TYPES = {
    "unlock": {
        "name": "解禁",
        "report": "RPT_LIFT_STAGE",
        "filter_field": "SECURITY_CODE",
        "query_date_field": "FREE_DATE",
        "date_fields": ("EUTIME", "NOTICE_DATE", "ANN_DATE"),
        "disclosure_fields": ("EUTIME", "NOTICE_DATE", "ANN_DATE"),
        "effective_fields": ("FREE_DATE", "free_date", "DATE"),
        "title_fields": ("FREE_SHARES_TYPE", "LIMITED_STOCK_TYPE"),
        "shares_fields": ("FREE_SHARES", "ABLE_FREE_SHARES"),
        "ratio_fields": ("FREE_RATIO",),
        "shares_scale": 10_000,
        "shares_source_unit": "万股",
        "shares_unit": "股",
        "ratio_source_unit": "%",
        "ratio_unit": "%",
        "available_shares_field": "ABLE_FREE_SHARES",
    },
    "holder_trade": {
        "name": "股东增减持",
        "report": "RPT_SHARE_HOLDER_INCREASE",
        "filter_field": "SECURITY_CODE",
        "query_date_field": "NOTICE_DATE",
        "date_fields": ("NOTICE_DATE", "EITIME", "TRADE_DATE", "END_DATE"),
        "disclosure_fields": ("NOTICE_DATE", "EITIME"),
        "effective_fields": ("START_DATE", "END_DATE", "TRADE_DATE"),
        "title_fields": ("DIRECTION", "HOLDER_NAME", "MARKET"),
        "shares_fields": ("CHANGE_NUM_SYMBOL", "CHANGE_NUM"),
        # CHANGE_RATE is the quoted price-change field on this report; the
        # holder's change ratio is CHANGE_FREE_RATIO.
        "ratio_fields": ("CHANGE_FREE_RATIO", "AFTER_CHANGE_RATE"),
        "shares_scale": 10_000,
        "shares_source_unit": "万股",
        "shares_unit": "股",
        "ratio_source_unit": "%",
        "ratio_unit": "%",
    },
    "earnings_forecast": {
        "name": "业绩预告",
        "report": "RPT_PUBLIC_OP_NEWPREDICT",
        "date_fields": ("NOTICE_DATE",),
        "effective_fields": ("REPORT_DATE",),
        "title_fields": ("PREDICT_FINANCE", "PREDICT_TYPE"),
        "amount_fields": ("PREDICT_AMT_LOWER", "PREDICT_AMT_UPPER"),
        "ratio_fields": ("ADD_AMP_LOWER", "ADD_AMP_UPPER"),
        # ADD_AMP is the provider's percentage change.  PREDICT_AMT is kept
        # raw because its unit depends on PREDICT_FINANCE (元、元/股等).
        "ratio_source_unit": "%",
        "ratio_unit": "%",
    },
    "buyback": {
        "name": "回购",
        "report": "RPTA_WEB_GETHGLIST_NEW",
        "filter_field": "DIM_SCODE",
        "query_date_field": "DIM_DATE",
        "date_fields": ("NOTICEDATE", "SHMRSLTNOTICEDATE", "UPDATEDATE", "DIM_DATE"),
        "disclosure_fields": ("NOTICEDATE", "SHMRSLTNOTICEDATE", "UPDATEDATE", "DIM_DATE"),
        "effective_fields": ("REPURSTARTDATE", "REPURENDDATE", "FINISHDATE", "REPURADVANCEDATE"),
        "title_fields": ("REPUROBJECTIVE", "REPURPROGRESS", "SHARETYPE"),
        "shares_fields": ("REPURNUM", "REPURNUMLOWER", "REPURNUMCAP"),
        "amount_fields": ("REPURAMOUNT", "REPURAMOUNTLOWER", "REPURAMOUNTLIMIT"),
        "ratio_fields": ("ZJSZBL", "ZJLTBL"),
        "shares_scale": 1,
        "shares_source_unit": "股",
        "shares_unit": "股",
        "amount_scale": 1,
        "amount_source_unit": "元",
        "amount_unit": "元",
        "ratio_source_unit": "%",
        "ratio_unit": "%",
        "shares_actual_field": "REPURNUM",
        "shares_lower_field": "REPURNUMLOWER",
        "shares_upper_field": "REPURNUMCAP",
        "amount_actual_field": "REPURAMOUNT",
        "amount_lower_field": "REPURAMOUNTLOWER",
        "amount_upper_field": "REPURAMOUNTLIMIT",
    },
    "pledge": {
        "name": "股权质押",
        "report": "RPT_PLEDGE_STATISTICS",
        "date_fields": ("NOTICE_DATE", "PLEDGE_DATE", "END_DATE"),
        "effective_fields": ("PLEDGE_DATE", "END_DATE"),
        "title_fields": ("PLEDGE_STATUS", "PLEDGEE", "HOLDER_NAME"),
        "shares_fields": ("PLEDGE_NUM", "PLEDGE_SHARES"),
        "ratio_fields": ("PLEDGE_RATIO",),
    },
}


def _first(row: Mapping[str, Any], fields: Iterable[str]) -> Any:
    for field in fields:
        if field in row and row[field] not in (None, "", "-"):
            return row[field]
    return None


def _date_or_none(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    raw = str(value).strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    if len(raw) >= 8 and raw[:8].isdigit():
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8])).isoformat()
        except ValueError:
            return None
    return raw[:10] if len(raw) >= 10 else raw


def _number_or_none(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        result = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rows_from_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ValueError("事件响应不是对象")
    result = payload.get("result")
    envelopes = [payload] + ([result] if isinstance(result, Mapping) else [])
    for envelope in envelopes:
        if envelope.get("success") is False:
            # Eastmoney uses 9201/"返回数据为空" for a valid zero-row query,
            # including a security with no future unlock batch.  Other business
            # errors must remain unavailable rather than becoming empty.
            message = str(envelope.get("message") or payload.get("message") or "")
            if str(envelope.get("code")) == "9201" and ("空" in message or (isinstance(result, Mapping) and result.get("count") == 0)):
                return []
            raise ValueError(f"事件接口业务失败: {message or envelope.get('code')}")
        if "code" in envelope and envelope.get("code") not in (None, "", 0, "0", 200, "200"):
            raise ValueError(f"事件接口业务失败: {envelope.get('code')}")
    data = result.get("data") if isinstance(result, Mapping) else payload.get("data")
    if data is None:
        return [] if isinstance(result, Mapping) and result.get("count") == 0 else (_ for _ in ()).throw(ValueError("事件响应缺少 result.data"))
    if not isinstance(data, list):
        raise ValueError("事件 result.data 不是列表")
    if not all(isinstance(item, Mapping) for item in data):
        raise ValueError("事件结果含非对象记录")
    return list(data)


def normalize_event_rows(rows: Iterable[Mapping[str, Any]], *, event_type: str, code: str, source_url: str = DATACENTER_URL) -> list[dict[str, Any]]:
    """Normalize source rows without interpreting them as a trade signal."""
    config = EVENT_TYPES.get(event_type)
    if config is None:
        raise ValueError(f"未知事件类型: {event_type}")
    output: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("事件记录不是对象")
        returned_code = str(_first(raw, ("SECURITY_CODE", "SECUCODE", "DIM_SCODE", "DIM_SCODE2", "code", "CODE")) or "").strip()
        returned_code = returned_code.split(".", 1)[0]
        if returned_code and returned_code.zfill(6) != code:
            raise ValueError(f"事件接口返回其他证券: {returned_code}")
        disclosure_date = _date_or_none(_first(raw, config.get("disclosure_fields", config.get("date_fields", ()))))
        effective_date = _date_or_none(_first(raw, config.get("effective_fields", ())))
        title = _first(raw, config.get("title_fields", ()))
        shares_field = next((field for field in config.get("shares_fields", ()) if raw.get(field) not in (None, "", "-")), None)
        amount_field = next((field for field in config.get("amount_fields", ()) if raw.get(field) not in (None, "", "-")), None)
        ratio_field = next((field for field in config.get("ratio_fields", ()) if raw.get(field) not in (None, "", "-")), None)
        shares_raw = _number_or_none(raw.get(shares_field)) if shares_field else None
        amount_raw = _number_or_none(raw.get(amount_field)) if amount_field else None
        ratio_raw = _number_or_none(raw.get(ratio_field)) if ratio_field else None
        shares_scale = float(config.get("shares_scale") or 1)
        amount_scale = float(config.get("amount_scale") or 1)
        row = {
            "event_type": event_type,
            "event_type_name": config["name"],
            "code": code,
            "name": str(_first(raw, ("SECURITY_NAME_ABBR", "SECURITYSHORTNAME", "SECURITY_NAME", "name", "NAME")) or ""),
            "notice_date": disclosure_date,
            "disclosure_date": disclosure_date,
            "effective_date": effective_date,
            "title": str(title or ""),
            "shares": shares_raw * shares_scale if shares_raw is not None and config.get("shares_unit") else None,
            "shares_unit": config.get("shares_unit") if shares_field else None,
            "shares_raw": raw.get(shares_field) if shares_field else None,
            "shares_source_unit": config.get("shares_source_unit") if shares_field else None,
            "shares_source_field": shares_field,
            "amount": amount_raw * amount_scale if amount_raw is not None and config.get("amount_unit") else None,
            "amount_unit": config.get("amount_unit") if amount_field else None,
            "amount_raw": raw.get(amount_field) if amount_field else None,
            "amount_source_unit": config.get("amount_source_unit") if amount_field else None,
            "amount_source_field": amount_field,
            "ratio": ratio_raw if ratio_raw is not None and config.get("ratio_unit") else None,
            "ratio_unit": config.get("ratio_unit") if ratio_field else None,
            "ratio_raw": raw.get(ratio_field) if ratio_field else None,
            "ratio_source_unit": config.get("ratio_source_unit") if ratio_field else None,
            "ratio_source_field": ratio_field,
            "status": str(_first(raw, ("STATUS", "status", "PLAN_STATUS", "PLEDGE_STATUS")) or ""),
            "source": EVENT_SOURCE,
            "source_url": source_url,
            "raw": dict(raw),
        }
        available_field = config.get("available_shares_field")
        available_raw = _number_or_none(raw.get(available_field)) if available_field else None
        if available_raw is not None:
            row["available_shares"] = available_raw * shares_scale
            row["available_shares_raw"] = raw.get(available_field)
            row["available_shares_source_unit"] = config.get("shares_source_unit")
            row["available_shares_unit"] = config.get("shares_unit")
        for measure, prefix in (("shares", "shares"), ("amount", "amount")):
            scale = float(config.get(f"{measure}_scale") or 1)
            unit = config.get(f"{measure}_unit")
            source_unit = config.get(f"{measure}_source_unit")
            for role in ("actual", "lower", "upper"):
                field = config.get(f"{measure}_{role}_field")
                raw_value = _number_or_none(raw.get(field)) if field else None
                row[f"{prefix}_{role}"] = raw_value * scale if raw_value is not None and unit else None
                row[f"{prefix}_{role}_raw"] = raw.get(field) if field else None
                row[f"{prefix}_{role}_unit"] = unit if field else None
                row[f"{prefix}_{role}_source_unit"] = source_unit if field else None
        if event_type == "buyback":
            row["plan_actual_status"] = "actual_and_plan" if row.get("shares_actual") is not None or row.get("amount_actual") is not None else "plan_only"
            row["progress"] = str(raw.get("REPURPROGRESS") or "")
        row["evidence_key"] = hashlib.sha256(json.dumps({key: row.get(key) for key in ("event_type", "code", "notice_date", "effective_date", "title", "shares", "amount")}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        output.append(row)
    return output


class EastmoneyEventSource:
    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None, cache_ttl: int = 24 * 3600):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("eastmoney_events")
        self.cache_ttl = max(300, int(cache_ttl))

    def _fetch_one(self, code: str, event_type: str, *, start: str | None, end: str | None, limit: int) -> Result:
        config = EVENT_TYPES[event_type]
        filter_field = config.get("filter_field", "SECURITY_CODE")
        filters = [f'({filter_field}="{code}")']
        date_field = config.get("query_date_field") or config.get("date_fields", (None,))[0]
        if start and date_field:
            filters.append(f"({date_field}>='{start}')")
        if end and date_field:
            filters.append(f"({date_field}<='{end}')")
        filter_value = "".join(filters)
        page_size = max(1, min(int(limit), 500))
        params = {
            "reportName": config["report"],
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
            "filter": filter_value,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortColumns": date_field or "NOTICE_DATE",
            "sortTypes": "-1",
        }
        try:
            rows: list[dict[str, Any]] = []
            source_url = DATACENTER_URL
            page_number = 1
            total_pages: int | None = None
            while page_number <= 50 and len(rows) < max(1, int(limit)):
                params["pageNumber"] = str(page_number)
                response = self.client.get(DATACENTER_URL, params=params, headers={"Referer": "https://data.eastmoney.com/"}, retries=1)
                source_url = response.url
                payload = response.json()
                page_rows = _rows_from_payload(payload)
                rows.extend(normalize_event_rows(page_rows, event_type=event_type, code=code, source_url=response.url))
                result = payload.get("result") if isinstance(payload, Mapping) else None
                try:
                    total_pages = int(result.get("pages")) if isinstance(result, Mapping) and result.get("pages") is not None else total_pages
                except (TypeError, ValueError):
                    pass
                if not page_rows or total_pages is None or page_number >= total_pages:
                    break
                page_number += 1
            rows = rows[: max(1, int(limit))]
            return Result(status=ResultStatus.OK if rows else ResultStatus.EMPTY, data=rows, source=EVENT_SOURCE, source_url=source_url, freshness="fresh", request_count=self.client.request_count, cache={"pages": page_number})
        except HTTPClientError as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=EVENT_SOURCE, source_url=DATACENTER_URL, code=exc.code, message=str(exc), retryable=exc.retryable)
        except (TypeError, ValueError, KeyError) as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=EVENT_SOURCE, source_url=DATACENTER_URL, code="malformed_response", message=str(exc), retryable=True)

    @coalesced_fetch("events")
    def fetch(self, code: str, *, event_types: Iterable[str] | None = None, as_of: str | None = None, forward_days: int = 90, force: bool = False, limit: int = 100) -> Result:
        try:
            symbol = normalize_security(code)
            if as_of is None:
                as_of = datetime.now(BEIJING).date().isoformat()
            as_of = validate_ymd(as_of)
        except (SymbolError, ValueError) as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=EVENT_SOURCE, source_url=DATACENTER_URL, code="invalid_request", message=str(exc))
        selected = list(event_types or EVENT_TYPES)
        unknown = [item for item in selected if item not in EVENT_TYPES]
        if unknown:
            return result_error(ResultStatus.UNSUPPORTED, source=EVENT_SOURCE, source_url=DATACENTER_URL, code="unsupported_event_type", message=f"未知事件类型: {', '.join(unknown)}")
        end = (date.fromisoformat(as_of) + timedelta(days=max(0, int(forward_days)))).isoformat()
        key = self.cache.key({"code": symbol.code, "types": selected, "as_of": as_of, "forward_days": forward_days, "limit": limit})
        if not force:
            cached = self.cache.get(key, ttl=self.cache_ttl)
            restored = result_from_cache(cached.value, default_source=EVENT_SOURCE, default_source_url=DATACENTER_URL) if cached else None
            if restored is not None:
                return restored
        all_rows: list[dict[str, Any]] = []
        statuses: dict[str, str] = {}
        errors: list[str] = []
        source_errors: list[str] = []
        missing_disclosure: dict[str, int] = {}
        for event_type in selected:
            # Unlock is a forward-looking effective-date query.  For every
            # other type, the query is bounded by the disclosure date, while
            # the event's effective/implementation date is retained as
            # evidence.  A future effective date is not a future disclosure.
            start = as_of if event_type == "unlock" else None
            event_end = end if event_type == "unlock" else as_of
            result = self._fetch_one(symbol.code, event_type, start=start, end=event_end, limit=limit)
            statuses[event_type] = result.status
            if result.status in {ResultStatus.OK.value, ResultStatus.EMPTY.value}:
                for row in result.data or []:
                    effective = row.get("effective_date")
                    notice = row.get("notice_date")
                    # A historical view may only contain information that was
                    # already disclosed by its as-of date.  Missing disclosure
                    # dates are not evidence of "no event" and must make the
                    # result incomplete instead of silently disappearing.
                    if not notice:
                        missing_disclosure[event_type] = missing_disclosure.get(event_type, 0) + 1
                        continue
                    if notice > as_of:
                        continue
                    if event_type == "unlock":
                        if effective and as_of <= effective <= end:
                            enriched = dict(row)
                            enriched.update({"event_state": "future_unlock", "is_future_effective": True})
                            all_rows.append(enriched)
                    else:
                        if event_type == "earnings_forecast":
                            event_state = "forecast"
                        elif event_type == "buyback" and (
                            row.get("plan_actual_status") == "plan_only"
                            or (effective and effective > as_of)
                        ):
                            event_state = "future_plan"
                        elif effective and effective > as_of:
                            event_state = "future_scheduled"
                        else:
                            event_state = "historical_or_completed"
                        enriched = dict(row)
                        enriched.update({
                            "event_state": event_state,
                            "is_future_effective": bool(effective and effective > as_of),
                            "is_forecast": event_type == "earnings_forecast",
                        })
                        all_rows.append(enriched)
            else:
                message = f"{event_type}:{(result.error or {}).get('message', result.status)}"
                errors.append(message)
                source_errors.append(message)
        for event_type, count in missing_disclosure.items():
            warning = f"{event_type}: {count} 条记录缺少公告/已知披露日期，已排除；历史结果不完整，不能据此断言无事件"
            errors.append(warning)
            statuses[event_type] = ResultStatus.PARTIAL.value
        unique: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            unique[row["evidence_key"]] = row
        data = {"code": symbol.code, "as_of": as_of, "forward_days": forward_days, "rows": sorted(unique.values(), key=lambda row: (row.get("effective_date") or "9999-99-99", row.get("notice_date") or "9999-99-99")), "status_by_type": statuses, "coverage_note": "历史视图只保留 as_of 前已披露信息；非解禁事件的未来实施日/报告期可保留但会标注 event_state，不将计划/预告当作已实施或实绩；有可靠源单位的股数统一为股、金额统一为元、比例统一为百分数，raw/source_unit 保留源值；源指标单位依赖标题时不填猜测性的 canonical amount；解禁未来日期仍需已披露"}
        if not all_rows and source_errors and len(source_errors) == len(selected):
            failure = result_error(ResultStatus.UNAVAILABLE, source=EVENT_SOURCE, source_url=DATACENTER_URL, code="all_event_types_failed", message="事件源均不可用", warnings=errors, retryable=True, data_date=as_of, as_of=as_of)
            self.cache.set(key, result_cache_value(failure), ttl=min(self.cache_ttl, 60), data_date=as_of)
            return failure
        status = ResultStatus.PARTIAL if errors else ResultStatus.OK if all_rows else ResultStatus.EMPTY
        result = Result(status=status, data=data, source=EVENT_SOURCE, source_url=DATACENTER_URL, data_date=as_of, as_of=as_of, freshness="fresh", warnings=errors, cache={"hit": False}, request_count=self.client.request_count)
        self.cache.set(key, result_cache_value(result), ttl=self.cache_ttl, data_date=as_of)
        return result
