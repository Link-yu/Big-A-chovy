"""Offline tests for the calendar, sentiment and event evidence layer."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

from tools.data_sources.cache import JsonCache
from tools.data_sources.calendar import TradingCalendarService, parse_calendar_payload
from tools.data_sources.contracts import ResultStatus
from tools.data_sources.events import EastmoneyEventSource, normalize_event_rows
from tools.data_sources.http import HTTPClient, HTTPResponse
from tools.data_sources.sentiment import EastmoneySentimentSource, calculate_market_sentiment


def json_response(url: str, payload: object) -> HTTPResponse:
    return HTTPResponse(200, url, json.dumps(payload, ensure_ascii=False).encode(), {"Content-Type": "application/json"})


class CalendarTests(unittest.TestCase):
    def _month_payload(self, year: int, month: int, open_days: set[int]) -> dict:
        import calendar

        return {"data": [{"jyrq": f"{year}-{month:02d}-{day:02d}", "jybz": "1" if day in open_days else "0"} for day in range(1, calendar.monthrange(year, month)[1] + 1)]}

    def test_calendar_requires_complete_natural_month(self) -> None:
        payload = self._month_payload(2026, 10, {1, 2})
        rows = parse_calendar_payload(payload, year=2026, month=10)
        self.assertEqual(len(rows), 31)
        with self.assertRaises(ValueError):
            parse_calendar_payload({"data": payload["data"][:-1]}, year=2026, month=10)

    def test_calendar_cache_and_next_day_are_source_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = self._month_payload(2026, 10, {1, 2, 5})
            calls = []

            def transport(method, url, params, **kwargs):
                calls.append(params["month"])
                return json_response(url, payload)

            source = TradingCalendarService(client=HTTPClient(transport=transport), cache=JsonCache("calendar_b", path=Path(directory) / "cache.json"))
            self.assertFalse(source.is_open("2026-10-03").data["is_open"])
            self.assertEqual(source.next_trading_day("2026-10-02").data["date"], "2026-10-05")
            self.assertEqual(calls, ["2026-10"])


class SentimentTests(unittest.TestCase):
    def test_limit_rules_and_zero_denominator_are_explicit(self) -> None:
        rows = [
            {"code": "000001", "name": "主板", "change_pct": 10.0, "is_limit_up": True, "streak": 2},
            {"code": "300001", "name": "创业板", "change_pct": 20.0, "is_limit_up": True, "streak": 3},
            {"code": "688001", "name": "科创板", "change_pct": 20.0, "is_limit_up": True, "streak": 1},
            {"code": "000002", "name": "*ST样本", "change_pct": 5.0, "is_limit_up": True, "streak": 1},
            {"code": "000003", "name": "炸板", "change_pct": 5.0, "touched_limit": True, "is_limit_up": False, "pool_kind": "broken"},
            {"code": "000004", "name": "跌停", "change_pct": -10.0, "is_limit_down": True},
        ]
        metrics = calculate_market_sentiment(rows, as_of="2026-10-03T10:00:00+08:00", scope="fixture")
        self.assertEqual(metrics["limit_up_count"], 4)
        self.assertEqual(metrics["limit_down_count"], 1)
        self.assertEqual(metrics["broken_count"], 1)
        self.assertEqual(metrics["break_rate"], 20.0)
        self.assertEqual(metrics["max_streak"], 3.0)
        empty = calculate_market_sentiment([], scope="fixture")
        self.assertIsNone(empty["break_rate"])
        self.assertIsNone(empty["promotion_rate"])

    def test_pool_source_distinguishes_empty_from_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def transport(method, url, **kwargs):
                if "ZTPool" in url:
                    return json_response(url, {"data": {"pool": [{"c": "000001", "n": "主板", "zdp": 10.0}]}})
                if "ZBPool" in url:
                    return json_response(url, {"data": {"pool": []}})
                return json_response(url, {"data": {"pool": []}})

            source = EastmoneySentimentSource(client=HTTPClient(transport=transport), cache=JsonCache("sentiment_b", path=Path(directory) / "cache.json"))
            result = source.fetch("2026-10-03")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data["pool_status"]["broken"], "ok")
            self.assertEqual(result.data["metrics"]["limit_up_count"], 1)


class EventTests(unittest.TestCase):
    def test_event_normalization_uses_canonical_units_and_rejects_wrong_code(self) -> None:
        rows = normalize_event_rows([{
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "样本",
            "NOTICE_DATE": "20261001",
            "FREE_DATE": "20261020",
            "FREE_SHARES": "100",
            "FREE_RATIO": None,
            "FREE_SHARES_TYPE": "首发",
        }], event_type="unlock", code="600519")
        self.assertEqual(rows[0]["notice_date"], "2026-10-01")
        self.assertEqual(rows[0]["effective_date"], "2026-10-20")
        self.assertIsNone(rows[0]["ratio"])
        self.assertEqual(rows[0]["shares_unit"], "股")
        self.assertEqual(rows[0]["shares"], 1_000_000)
        self.assertEqual(rows[0]["shares_source_unit"], "万股")
        forecast = normalize_event_rows([{
            "SECURITY_CODE": "600519",
            "NOTICE_DATE": "2026-10-01",
            "REPORT_DATE": "2026-09-30",
            "PREDICT_FINANCE": "归属于上市公司股东的净利润",
            "PREDICT_AMT_LOWER": 100000000,
            "ADD_AMP_LOWER": "12.5",
        }], event_type="earnings_forecast", code="600519")
        self.assertEqual(forecast[0]["ratio"], 12.5)
        self.assertEqual(forecast[0]["ratio_unit"], "%")
        self.assertIsNone(forecast[0]["amount_unit"])
        with self.assertRaises(ValueError):
            normalize_event_rows([{"SECURITY_CODE": "000001"}], event_type="unlock", code="600519")

    def test_event_source_returns_empty_only_after_valid_empty_responses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def transport(method, url, **kwargs):
                return json_response(url, {"result": {"data": []}})

            result = EastmoneyEventSource(client=HTTPClient(transport=transport), cache=JsonCache("events_b", path=Path(directory) / "cache.json")).fetch("600519", event_types=["unlock"], as_of="2026-10-03")
            self.assertEqual(result.status, "empty")
            self.assertEqual(result.data["rows"], [])


if __name__ == "__main__":
    unittest.main()
