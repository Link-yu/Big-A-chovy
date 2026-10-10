"""Offline tests for historical daily packages and cross-day chip estimates."""

from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
from urllib.error import HTTPError

from tools.data_sources.cache import JsonCache
from tools.data_sources.contracts import ResultStatus
from tools.data_sources.http import HTTPClient, HTTPResponse
from tools.query_chips import estimate_historical_chips
from tools.query_history import (
    TDX_COD_RECORD_SIZE,
    TDX_MD1_BLOCK_SIZE,
    TDXHistorySource,
    industry_as_of,
    parse_industry_rows,
    parse_tdx_daily_package,
    parse_valuation_rows,
)


def _fixture_zip(ymd: str = "20261003", *, dangerous_name: str | None = None, omit_bj: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        if dangerous_name:
            archive.writestr(dangerous_name, b"not safe")
        for market, code, name, close in (
            ("sh", "600519", "样本沪", 100.25),
            ("sz", "000001", "样本深", 20.5),
            ("bj", "920001", "样本北", 8.75),
        ):
            if market == "bj" and omit_bj:
                continue
            cod_name = f"{market}{ymd[2:]}.cod"
            md1_name = f"{market}{ymd[2:]}.md1"
            cod = bytearray(TDX_COD_RECORD_SIZE)
            cod[0:6] = code.encode("ascii")
            struct.pack_into("<H", cod, 32, 0)
            cod[40:40 + len(name.encode("gbk"))] = name.encode("gbk")
            block = bytearray(TDX_MD1_BLOCK_SIZE)
            struct.pack_into("<d", block, 4, close - 1)
            struct.pack_into("<4d", block, 12, close, close + 1, close - 1, close)
            struct.pack_into("<Q", block, 56, 123456)
            struct.pack_into("<d", block, 72, close * 123456)
            archive.writestr(cod_name, cod)
            archive.writestr(md1_name, block)
    return output.getvalue()


class TDXParserTests(unittest.TestCase):
    def test_http_client_keeps_urllib_status(self) -> None:
        class BrokenOpener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 404, "Not Found", {}, None)

        from tools.data_sources.http import HTTPClient, HTTPClientError

        with self.assertRaises(HTTPClientError) as captured:
            HTTPClient(opener=BrokenOpener()).get("https://example.invalid/missing", retries=0)
        self.assertEqual(captured.exception.code, "http_status")
        self.assertEqual(captured.exception.status, 404)
        cause = captured.exception.__cause__
        if cause is not None and hasattr(cause, "close"):
            cause.close()

    def test_parser_reads_identity_units_and_market_rows(self) -> None:
        rows = parse_tdx_daily_package(
            _fixture_zip(),
            "2026-10-03",
            min_priced={"sh": 1, "sz": 1, "bj": 1},
        )
        self.assertEqual({row["market"] for row in rows}, {"sh", "sz", "bj"})
        sh = next(row for row in rows if row["code"] == "600519")
        self.assertEqual(sh["name"], "样本沪")
        self.assertEqual(sh["volume_shares"], 123456)
        self.assertEqual(sh["volume_unit"], "shares")
        self.assertEqual(sh["amount_unit"], "yuan")
        self.assertEqual(sh["data_date"], "2026-10-03")

    def test_parser_rejects_bad_lengths_missing_market_and_unsafe_path(self) -> None:
        with self.assertRaises(ValueError):
            parse_tdx_daily_package(_fixture_zip(dangerous_name="../evil"), "20261003", min_priced={"sh": 1, "sz": 1, "bj": 1})
        broken = bytearray(_fixture_zip())
        # The fixture is a compressed ZIP; use a direct malformed ZIP for the
        # structural check rather than trying to mutate compressed offsets.
        with zipfile.ZipFile(io.BytesIO(broken), "r") as archive:
            rebuilt = io.BytesIO()
            with zipfile.ZipFile(rebuilt, "w") as output:
                for info in archive.infolist():
                    payload = archive.read(info.filename)
                    if info.filename.startswith("sh") and info.filename.endswith(".md1"):
                        payload += b"x"
                    output.writestr(info.filename, payload)
        with self.assertRaisesRegex(ValueError, "整块"):
            parse_tdx_daily_package(rebuilt.getvalue(), "20261003", min_priced={"sh": 1, "sz": 1, "bj": 1})
        with self.assertRaisesRegex(ValueError, "缺少 bj"):
            parse_tdx_daily_package(_fixture_zip(omit_bj=True), "20261003", min_priced={"sh": 1, "sz": 1, "bj": 1})

    def test_source_distinguishes_missing_date_and_uses_cache(self) -> None:
        calls: list[str] = []
        package = _fixture_zip()

        def transport(method, url, **kwargs):
            calls.append(url)
            return HTTPResponse(200, url, package, {"Content-Type": "application/zip"})

        with tempfile.TemporaryDirectory() as directory:
            source = TDXHistorySource(
                client=HTTPClient(transport=transport),
                cache=JsonCache("tdx_d", path=Path(directory) / "cache.json"),
            )
            first = source.fetch("20261003", min_priced={"sh": 1, "sz": 1, "bj": 1})
            second = source.fetch("20261003", min_priced={"sh": 1, "sz": 1, "bj": 1})
            self.assertEqual(first.status, ResultStatus.OK.value)
            self.assertEqual(second.freshness, "cache")
            self.assertEqual(len(calls), 1)

        def missing_transport(method, url, **kwargs):
            from tools.data_sources.http import HTTPClientError

            raise HTTPClientError("404", code="http_status", status=404)

        missing = TDXHistorySource(client=HTTPClient(transport=missing_transport), cache=JsonCache("tdx_d_missing", path=Path(tempfile.mkdtemp()) / "cache.json")).fetch("20261004")
        self.assertEqual(missing.status, ResultStatus.UNAVAILABLE.value)
        self.assertEqual(missing.error["code"], "missing_date")


class HistoricalNormalizationTests(unittest.TestCase):
    def test_valuation_and_industry_normalization_keep_unknowns(self) -> None:
        valuation = parse_valuation_rows([
            {"date": "2026-10-02", "code": "sh.600519", "close": "100", "peTTM": "20", "turn": "1.2", "isST": "0"},
        ], code="600519")
        self.assertEqual(valuation[0]["code"], "600519")
        self.assertEqual(valuation[0]["turnover_pct"], 1.2)
        self.assertEqual(valuation[0]["pb_mrq"], None)
        rows = parse_industry_rows([
            {"股票代码": "000001", "计入日期": "2013-01-01", "行业代码": "440101"},
            {"股票代码": "000001", "计入日期": "2021-07-30", "行业代码": "480301"},
        ])
        picked = industry_as_of(rows, "000001", "2022-01-01")
        self.assertEqual(picked["industry_code"], "480301")
        self.assertIsNone(picked["industry_name"])


class ChipEstimateTests(unittest.TestCase):
    def _rows(self):
        return [
            {"date": "2026-09-28", "high": 10.2, "low": 9.8, "close": 10.0, "turn": 1.0, "adjustment": "qfq"},
            {"date": "2026-09-29", "high": 10.1, "low": 9.9, "close": 10.0, "turn": 1.0, "adjustment": "qfq"},
            {"date": "2026-09-30", "high": 10.4, "low": 10.0, "close": 10.3, "turn": 2.0, "adjustment": "qfq"},
        ]

    def test_estimate_has_explicit_label_window_and_no_future_rows(self) -> None:
        result = estimate_historical_chips(self._rows(), grid_size=40, decay=0.9, as_of="2026-09-29")
        self.assertEqual(result["label"], "筹码估算")
        self.assertEqual(result["input_window"]["row_count"], 2)
        self.assertNotIn("2026-09-30", result["input_window"]["processed_dates"])
        self.assertEqual(result["parameters"]["decay"], 0.9)
        self.assertIn("cost_70", result)
        self.assertIsInstance(result["profit_ratio_pct"], float)

    def test_estimate_rejects_missing_turn_and_mixed_adjustment(self) -> None:
        missing = self._rows()
        missing[1] = {key: value for key, value in missing[1].items() if key != "turn"}
        with self.assertRaisesRegex(ValueError, "换手率"):
            estimate_historical_chips(missing)
        mixed = self._rows()
        mixed[2]["adjustment"] = "hfq"
        with self.assertRaisesRegex(ValueError, "复权口径"):
            estimate_historical_chips(mixed)

    def test_estimate_handles_narrow_price_range_and_rejects_future_or_bad_rows(self) -> None:
        narrow = [
            {"date": "2026-09-28", "high": 10, "low": 10, "close": 10, "turn": 0.5},
            {"date": "2026-09-29", "high": 10, "low": 10, "close": 10, "turn": 0.5},
        ]
        result = estimate_historical_chips(narrow, grid_size=30)
        self.assertGreaterEqual(result["cost_70"]["high"], result["cost_70"]["low"])
        bad = self._rows()
        bad[0]["turn"] = None
        with self.assertRaises(ValueError):
            estimate_historical_chips(bad)


if __name__ == "__main__":
    unittest.main()
