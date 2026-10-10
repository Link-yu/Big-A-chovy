"""Offline contract tests for the first public-data adapter tranche."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from tools.data_sources.announcements import fetch_announcement_evidence
from tools.data_sources.cache import JsonCache
from tools.data_sources.cninfo import CNInfoAnnouncementSource
from tools.data_sources.contracts import Result, ResultStatus, result_error
from tools.data_sources.http import HTTPClient, HTTPResponse
from tools.data_sources.sina import SinaFinancialSource, parse_financial_payload
from tools.data_sources.symbols import SymbolError, normalize_security
from tools.data_sources.tencent import TencentTickSource, aggregate_ticks, parse_tick_page


def response(url: str, payload: object, *, status: int = 200, content_type: str = "application/json") -> HTTPResponse:
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return HTTPResponse(status=status, url=url, body=body, headers={"Content-Type": content_type})


class DataSourceContractTests(unittest.TestCase):
    def test_result_enum_is_accepted_by_error_factory(self) -> None:
        result = result_error(ResultStatus.UNAVAILABLE, source="x", source_url="u", code="down", message="down")
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.usable)

    def test_symbol_prefix_contradiction_is_rejected(self) -> None:
        self.assertEqual(normalize_security("SH600519").tencent, "sh600519")
        self.assertEqual(normalize_security("600519.SH").secid, "1.600519")
        with self.assertRaises(SymbolError):
            normalize_security("SZ600519")
        with self.assertRaises(SymbolError):
            normalize_security("000001", kind="index")

    def test_http_retry_is_bounded_and_success_is_returned(self) -> None:
        calls = []

        def transport(method, url, **kwargs):
            calls.append(method)
            if len(calls) == 1:
                return response(url, {"error": "busy"}, status=503)
            return response(url, {"ok": True})

        client = HTTPClient(transport=transport)
        result = client.get("https://example.test", retries=1)
        self.assertEqual(result.json(), {"ok": True})
        self.assertEqual(len(calls), 2)


class TencentAdapterTests(unittest.TestCase):
    def _snapshot(self, symbol: str = "sh600519") -> str:
        fields = [""] * 36
        fields[1] = "测试股"
        fields[3] = "10.00"
        fields[30] = "20261003100000"
        fields[35] = "10.00/100/1000000"
        return f'v_{symbol}="' + "~".join(fields) + '";'

    def test_parse_and_aggregate_keeps_zero_sell_as_unknown_ratio(self) -> None:
        rows = parse_tick_page(
            'v_detail_data_sh600519=[0,"1/09:30:00/10/0/1/100/B|2/09:31:00/10/0/1/200/M"];',
            "sh600519",
            0,
        )
        self.assertEqual(len(rows or []), 2)
        aggregate = aggregate_ticks(rows or [], as_of="093100")
        self.assertIsNone(aggregate["buy_sell_ratio"])
        self.assertTrue(aggregate["data_sufficient"])

    def test_incremental_cache_re_reads_only_the_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_state = os.environ.get("A_SHARE_STATE_DIR")
            os.environ["A_SHARE_STATE_DIR"] = directory
            try:
                pages = {
                    page: 'v_detail_data_sh600519=[' + str(page) + ',"' + "|".join(
                        f"{page * 70 + seq}/09:30:00/10/0/1/100/B" for seq in range(1, 71)
                    ) + '"];'
                    for page in range(3)
                }
                page_calls = []

                def transport(method, url, params, **kwargs):
                    if url.startswith("https://qt.gtimg.cn"):
                        return HTTPResponse(200, url, self._snapshot().encode(), {"Content-Type": "text/plain; charset=GBK"})
                    page = int(params.get("p", 0))
                    page_calls.append(page)
                    text = pages.get(page, f'v_detail_data_sh600519=[{page},""];')
                    return HTTPResponse(200, url, text.encode(), {"Content-Type": "text/plain"})

                client = HTTPClient(transport=transport)
                source = TencentTickSource(client=client, cache=JsonCache("ticks_a"), sleep_seconds=0)
                first = source.fetch("600519", max_pages=10)
                self.assertEqual(first.status, "ok")
                self.assertEqual(page_calls, [0, 1, 2, 3])
                page_calls.clear()
                second = source.fetch("600519", max_pages=10)
                self.assertEqual(second.status, "ok")
                self.assertEqual(page_calls, [2, 3])
                self.assertTrue(second.cache["incremental"])
            finally:
                if old_state is None:
                    os.environ.pop("A_SHARE_STATE_DIR", None)
                else:
                    os.environ["A_SHARE_STATE_DIR"] = old_state


class SinaAndCNInfoTests(unittest.TestCase):
    def test_sina_report_list_is_normalized_and_empty_is_distinct(self) -> None:
        payload = {"result": {"data": {"report_list": {"20260930": {"data": [
            {"item_title": "基本每股收益", "item_value": "1.20", "item_tongbi": "10%"},
            {"item_title": "归属于母公司所有者的净利润", "item_value": "100"},
        ]}}}}}
        rows = parse_financial_payload(payload)
        self.assertEqual(rows[0]["report_period"], "2026-09-30")
        self.assertEqual(rows[0]["基本每股收益"], "1.20")
        with self.assertRaises(ValueError):
            parse_financial_payload({"result": {"data": {"report_list": None}}})

        def transport(method, url, **kwargs):
            return response(url, {"result": {"data": {"report_list": {}}}})

        result = SinaFinancialSource(client=HTTPClient(transport=transport), cache=JsonCache("sina_a_empty", path=Path(tempfile.mkdtemp()) / "cache.json")).fetch_reports("600519")
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.data, [])

    def test_cninfo_uses_dynamic_map_and_preserves_date_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def transport(method, url, **kwargs):
                calls.append((method, url))
                if url.endswith("szse_stock.json"):
                    return response(url, {"stockList": [{"code": "600519", "orgId": "org-519"}]})
                return response(url, {"announcements": [{"secCode": "600519", "announcementTitle": "测试公告", "announcementTime": 0, "announcementId": "a1"}]})

            source = CNInfoAnnouncementSource(
                client=HTTPClient(transport=transport),
                cache=JsonCache("cninfo_a", path=Path(directory) / "cache.json"),
            )
            result = source.fetch("600519")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data[0]["announcement_id"], "a1")
            self.assertEqual(result.data[0]["url"].split("=")[-1], "a1")
            self.assertEqual([item[0] for item in calls], ["GET", "POST"])

    def test_announcement_fallback_never_turns_two_failures_into_empty(self) -> None:
        class FailedFallback:
            def fetch(self, code, page_size=30):
                return result_error(ResultStatus.UNAVAILABLE, source="cninfo", source_url="u", code="down", message="down")

        result = fetch_announcement_evidence("600519", primary=lambda: (_ for _ in ()).throw(RuntimeError("primary down")), fallback=FailedFallback())
        self.assertEqual(result.status, "unavailable")
        self.assertNotEqual(result.status, "empty")

    def test_valid_primary_empty_is_not_rewritten_by_fallback(self) -> None:
        class ShouldNotRun:
            def fetch(self, code, page_size=30):
                raise AssertionError("valid primary empty must be retained")

        result = fetch_announcement_evidence("600519", primary=lambda: {"rows": [], "total": 0, "source_url": "primary"}, fallback=ShouldNotRun())
        self.assertEqual(result.status, "empty")

    def test_nested_valid_primary_empty_is_not_rewritten_by_fallback(self) -> None:
        class ShouldNotRun:
            def fetch(self, code, page_size=30):
                raise AssertionError("valid nested primary empty must be retained")

        result = fetch_announcement_evidence(
            "600519",
            primary=lambda: {"success": True, "data": {"list": [], "total": 0}, "source_url": "primary"},
            fallback=ShouldNotRun(),
        )
        self.assertEqual(result.status, "empty")


if __name__ == "__main__":
    unittest.main()
