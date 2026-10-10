"""CNINFO announcement adapter with a dynamic official stock/orgId map."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from typing import Any

from .cache import JsonCache
from .contracts import Result, ResultStatus, result_empty, result_error, result_ok
from .http import HTTPClient, HTTPClientError
from .symbols import SymbolError, normalize_security


CNINFO_MAP_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_SOURCE = "cninfo_announcements"
CNINFO_TZ = timezone(timedelta(hours=8))


def _announcement_date(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=CNINFO_TZ).date().isoformat()
    raw = str(value or "").strip()
    return raw[:10] if raw else ""


def parse_org_map(payload: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("stockList"), list):
        raise ValueError("巨潮股票映射缺少 stockList")
    output: dict[str, str] = {}
    for item in payload["stockList"]:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        org_id = str(item.get("orgId") or "").strip()
        if code and org_id:
            output[code] = org_id
    if not output:
        raise ValueError("巨潮股票映射为空")
    return output


def parse_announcement_payload(payload: Mapping[str, Any], *, code: str, source_url: str = CNINFO_ANNOUNCEMENT_URL, page_size: int = 30) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or "announcements" not in payload:
        raise ValueError("巨潮公告响应缺少 announcements")
    success = payload.get("success")
    if success is not None and success not in (True, 1, "1", "true", "True", "ok", "OK"):
        raise ValueError(f"巨潮公告业务失败: success={success!r}, code={payload.get('code')!r}")
    if "code" in payload and payload.get("code") not in (None, "", 0, "0", 200, "200"):
        raise ValueError(f"巨潮公告业务失败: code={payload.get('code')!r}")
    announcements = payload.get("announcements")
    if announcements is None:
        raise ValueError("巨潮公告响应 announcements 为 null")
    if not isinstance(announcements, list):
        raise ValueError("巨潮 announcements 不是列表")
    total = next((payload.get(key) for key in ("totalAnnouncement", "total", "totalCount", "count") if payload.get(key) is not None), None)
    if total is not None:
        try:
            expected = min(int(total), max(1, int(page_size)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"巨潮公告 total 不是非负整数: {total!r}") from exc
        if int(total) < 0 or len(announcements) != expected:
            raise ValueError(f"巨潮公告页不完整: total={total}, page_size={page_size}, rows={len(announcements)}")
    rows: list[dict[str, Any]] = []
    for item in announcements:
        if not isinstance(item, Mapping):
            raise ValueError("巨潮公告含非对象记录")
        returned_code = str(item.get("secCode") or item.get("securityCode") or item.get("stockCode") or "").strip()
        if returned_code and returned_code != code:
            raise ValueError(f"巨潮公告返回了其他证券: {returned_code}")
        announcement_id = str(item.get("announcementId") or item.get("announcement_id") or "").strip()
        row = {
            "code": code,
            "title": str(item.get("announcementTitle") or item.get("title") or "").strip(),
            "type": str(item.get("announcementTypeName") or item.get("type") or "").strip(),
            "date": _announcement_date(item.get("announcementTime") or item.get("date")),
            "announcement_id": announcement_id,
            "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={announcement_id}" if announcement_id else "",
            "source": CNINFO_SOURCE,
            "source_url": source_url,
        }
        if not row["title"]:
            raise ValueError("巨潮公告非空页缺少可解析标题")
        rows.append(row)
    return rows


class CNInfoAnnouncementSource:
    """CNINFO full-text announcement search.

    The orgId map is fetched dynamically.  If it cannot be obtained, this
    source returns unavailable instead of guessing an orgId and reporting a
    false clean announcement screen.
    """

    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("cninfo_announcements")
        self._org_map: dict[str, str] | None = None

    def _org_id(self, code: str, *, force: bool = False) -> str:
        if self._org_map and not force:
            found = self._org_map.get(code)
            if found:
                return found
        map_key = self.cache.key({"kind": "org_map"})
        if not force:
            cached = self.cache.get(map_key, ttl=24 * 3600)
            if cached and isinstance(cached.value, dict):
                self._org_map = {str(k): str(v) for k, v in cached.value.items()}
        if not self._org_map:
            response = self.client.get(CNINFO_MAP_URL, headers={"Accept": "application/json"}, retries=1)
            self._org_map = parse_org_map(response.json())
            self.cache.set(map_key, self._org_map, ttl=24 * 3600)
        org_id = self._org_map.get(code)
        if not org_id:
            raise ValueError(f"巨潮股票映射中没有 {code}")
        return org_id

    def fetch(self, code: str, *, page_size: int = 30, force: bool = False) -> Result:
        try:
            symbol = normalize_security(code)
            if symbol.market == "bj":
                return result_error(ResultStatus.UNSUPPORTED, source=CNINFO_SOURCE, source_url=CNINFO_ANNOUNCEMENT_URL, code="unsupported_market", message="巨潮公告适配器暂不支持北交所")
            org_id = self._org_id(symbol.code, force=force)
            response = self.client.post(
                CNINFO_ANNOUNCEMENT_URL,
                data={
                    "stock": f"{symbol.code},{org_id}",
                    "tabName": "fulltext",
                    "pageSize": str(max(1, int(page_size))),
                    "pageNum": "1",
                    "column": "",
                    "category": "",
                    "plate": "",
                    "seDate": "",
                    "searchkey": "",
                    "secid": "",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.cninfo.com.cn",
                    "Referer": "https://www.cninfo.com.cn/new/disclosure",
                },
                retries=1,
            )
            rows = parse_announcement_payload(response.json(), code=symbol.code, page_size=page_size)
            if not rows:
                return result_empty(source=CNINFO_SOURCE, source_url=CNINFO_ANNOUNCEMENT_URL, data=[])
            return result_ok(rows, source=CNINFO_SOURCE, source_url=CNINFO_ANNOUNCEMENT_URL, request_count=self.client.request_count)
        except SymbolError as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=CNINFO_SOURCE, source_url=CNINFO_ANNOUNCEMENT_URL, code="invalid_security", message=str(exc))
        except HTTPClientError as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=CNINFO_SOURCE, source_url=CNINFO_ANNOUNCEMENT_URL, code=exc.code, message=str(exc), retryable=exc.retryable)
        except (TypeError, ValueError, KeyError) as exc:
            return result_error(ResultStatus.UNAVAILABLE, source=CNINFO_SOURCE, source_url=CNINFO_ANNOUNCEMENT_URL, code="malformed_response", message=str(exc), retryable=True)
