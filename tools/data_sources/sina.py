"""Sina finance-report adapter.

The public endpoint returns ``result.data.report_list``.  This module keeps
the report period and the provider's item values separate from the financial
interpretation layer: a missing or malformed report is never turned into a
profitable company by accident.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import re
from typing import Any

from .cache import JsonCache
from .contracts import Result, ResultStatus, result_empty, result_error, result_ok
from .http import HTTPClient, HTTPClientError
from .symbols import SymbolError, normalize_security


SINA_FINANCE_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
SINA_FINANCE_SOURCE = "sina_financial_report"
REPORT_TYPES = {"lrb": "利润表", "fzb": "资产负债表", "llb": "现金流量表"}


def _period(value: Any) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError as exc:
            raise ValueError(f"新浪报告期无效: {raw!r}") from exc
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    raise ValueError(f"新浪报告期格式异常: {raw!r}")


def parse_financial_payload(
    payload: Mapping[str, Any],
    *,
    report_type: str = "lrb",
    source_url: str = SINA_FINANCE_URL,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Normalize a Sina response while retaining raw line-item information."""
    if report_type not in REPORT_TYPES:
        raise ValueError(f"不支持的新浪财报类型: {report_type!r}")
    if not isinstance(payload, Mapping):
        raise ValueError("新浪财报响应不是对象")
    result = payload.get("result")
    data = result.get("data") if isinstance(result, Mapping) else None
    report_list = data.get("report_list") if isinstance(data, Mapping) else None
    if not isinstance(report_list, (Mapping, list)):
        raise ValueError("新浪财报响应缺少 result.data.report_list")

    items: list[tuple[str, Any]] = []
    if isinstance(report_list, Mapping):
        items = [(str(period), obj) for period, obj in report_list.items()]
    else:
        for obj in report_list:
            if not isinstance(obj, Mapping):
                raise ValueError("新浪财报 report_list 含非对象记录")
            period = obj.get("report_date") or obj.get("report_period") or obj.get("报告期")
            items.append((str(period or ""), obj))

    normalized: list[dict[str, Any]] = []
    for raw_period, obj in sorted(items, key=lambda item: item[0], reverse=True)[: max(0, int(limit))]:
        if not isinstance(obj, Mapping):
            raise ValueError(f"新浪财报 {raw_period!r} 记录不是对象")
        report_period = _period(raw_period or obj.get("report_period") or obj.get("report_date"))
        line_items = obj.get("data")
        if line_items is None:
            # A list-shaped fixture may already use a line-item list as data.
            line_items = obj.get("items", [])
        if not isinstance(line_items, list):
            raise ValueError(f"新浪财报 {report_period} 的 data 不是列表")
        row: dict[str, Any] = {
            "report_period": report_period,
            "报告期": report_period,
            "report_type": report_type,
            "report_type_name": REPORT_TYPES[report_type],
            "source": SINA_FINANCE_SOURCE,
            "source_url": source_url,
            "items": {},
        }
        for item in line_items:
            if not isinstance(item, Mapping):
                raise ValueError(f"新浪财报 {report_period} 含非对象行项")
            title = str(item.get("item_title") or item.get("title") or "").strip()
            if not title or item.get("item_value") is None and item.get("value") is None:
                continue
            value = item.get("item_value") if "item_value" in item else item.get("value")
            row[title] = value
            row["items"][title] = {
                "value": value,
                "同比": item.get("item_tongbi") if "item_tongbi" in item else item.get("yoy"),
            }
            yoy = item.get("item_tongbi") if "item_tongbi" in item else item.get("yoy")
            if yoy not in (None, ""):
                row[f"{title}_同比"] = yoy
        # Publication date is only carried through when the source supplies it;
        # report period must not be presented as a disclosure timestamp.
        for key in ("publish_date", "publishDate", "公告日期", "披露日期"):
            if obj.get(key):
                row["published_at"] = str(obj[key])
                break
        normalized.append(row)
    return normalized


class SinaFinancialSource:
    """Fetch one of Sina's three statement families."""

    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("sina_financial")

    def fetch_reports(self, code: str, *, report_type: str = "lrb", limit: int = 8, force: bool = False) -> Result:
        if report_type not in REPORT_TYPES:
            return result_error(
                ResultStatus.UNSUPPORTED,
                source=SINA_FINANCE_SOURCE,
                source_url=SINA_FINANCE_URL,
                code="unsupported_report_type",
                message=f"不支持的新浪财报类型: {report_type}",
            )
        try:
            symbol = normalize_security(code)
            key = self.cache.key({"code": symbol.code, "market": symbol.market, "type": report_type, "limit": limit})
            if not force:
                cached = self.cache.get(key, ttl=6 * 3600)
                if cached and isinstance(cached.value, dict) and isinstance(cached.value.get("rows"), list):
                    return Result(
                        status=ResultStatus.OK if cached.value["rows"] else ResultStatus.EMPTY,
                        data=cached.value["rows"],
                        source=SINA_FINANCE_SOURCE,
                        source_url=SINA_FINANCE_URL,
                        freshness="cached",
                        cache={"hit": True},
                    )
            prefix = "sh" if symbol.market == "sh" else "sz"
            response = self.client.get(
                SINA_FINANCE_URL,
                params={
                    "paperCode": f"{prefix}{symbol.code}",
                    "source": report_type,
                    "type": "0",
                    "page": "1",
                    "num": str(max(1, int(limit))),
                },
                headers={"Accept": "application/json"},
                retries=1,
            )
            rows = parse_financial_payload(response.json(), report_type=report_type, limit=limit)
            payload = {"code": symbol.code, "market": symbol.market, "report_type": report_type, "rows": rows}
            self.cache.set(key, payload, ttl=6 * 3600)
            if not rows:
                return result_empty(source=SINA_FINANCE_SOURCE, source_url=SINA_FINANCE_URL, data=[])
            return result_ok(rows, source=SINA_FINANCE_SOURCE, source_url=SINA_FINANCE_URL, cache={"hit": False}, request_count=self.client.request_count)
        except SymbolError as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=SINA_FINANCE_SOURCE, source_url=SINA_FINANCE_URL, code="invalid_security", message=str(exc))
        except HTTPClientError as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=SINA_FINANCE_SOURCE, source_url=SINA_FINANCE_URL, code=exc.code, message=str(exc), retryable=exc.retryable)
        except (TypeError, ValueError, KeyError) as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=SINA_FINANCE_SOURCE, source_url=SINA_FINANCE_URL, code="malformed_response", message=str(exc), retryable=True)

