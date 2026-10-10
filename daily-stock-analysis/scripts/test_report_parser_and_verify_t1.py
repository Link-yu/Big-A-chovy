import io
import json
import sys
import tempfile
import unittest
from datetime import datetime as RealDateTime
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.report_parser import get_report_files  # noqa: E402
from tools import verify_t1  # noqa: E402
import query_quote  # noqa: E402


class ReportParserArchiveOrderingTests(unittest.TestCase):
    @staticmethod
    def _touch(root: Path, relative: str) -> str:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic report\n", encoding="utf-8")
        return str(path)

    def test_requested_date_merges_flat_and_archived_reports_by_report_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flat = self._touch(root, "A股筛选结果_20261008_0910.md")
            archived_early = self._touch(root, "20261008/A股筛选结果_20261008_0905.md")
            archived_late = self._touch(root, "20261008/A股筛选结果_20261008_1135.md")

            got = get_report_files(str(root), "20261008")

            self.assertEqual(got, [archived_early, flat, archived_late])

    def test_default_latest_date_ignores_empty_newer_date_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = self._touch(root, "A股筛选结果_20261007_1450.md")
            latest = self._touch(root, "20261008/A股筛选结果_20261008_1135.md")
            (root / "20261009").mkdir()

            self.assertEqual(get_report_files(str(root)), [latest])
            self.assertEqual(get_report_files(str(root), "20261007"), [older])

    def test_empty_or_missing_report_directory_returns_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "20261008").mkdir()
            self.assertEqual(get_report_files(str(root)), [])
            self.assertEqual(get_report_files(str(root / "missing")), [])


class T1MinuteEvidenceTests(unittest.TestCase):
    def test_legacy_code_only_call_is_explicitly_unavailable(self):
        with patch.object(verify_t1, "fetch_minute_data") as fetch:
            result = verify_t1.get_morning_945_price("600000")

        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["target_date"])
        self.assertIsNone(result["price_945"])
        self.assertIn("缺少报告日期", result["reason"])
        fetch.assert_not_called()

    def test_historical_target_never_uses_today_minute_data(self):
        with patch.object(verify_t1, "_verified_next_trading_date", return_value="20260924"), \
             patch.object(verify_t1, "_current_market_date", return_value="20261008"), \
             patch.object(verify_t1, "fetch_minute_data") as fetch:
            result = verify_t1.get_morning_945_price("600000", "20260923")

        self.assertFalse(result["verified"])
        self.assertEqual(result["target_date"], "20260924")
        self.assertIsNone(result["price_945"])
        self.assertIn("当前交易日数据", result["reason"])
        fetch.assert_not_called()

    def test_missing_exact_target_time_is_not_replaced_by_previous_bar(self):
        bars = ["09:30 10.00 1 1", "09:44 10.40 1 1"]
        with patch.object(verify_t1, "_verified_next_trading_date", return_value="20261008"), \
             patch.object(verify_t1, "_current_market_date", return_value="20261008"), \
             patch.object(verify_t1, "fetch_minute_data", return_value=bars) as fetch:
            result = verify_t1.get_morning_945_price("600000", "20261007")

        self.assertFalse(result["verified"])
        self.assertFalse(result["target_snapshot_found"])
        self.assertIsNone(result["price_945"])
        self.assertIn("精确 09:45", result["reason"])
        fetch.assert_called_once_with("600000", expected_date="20261008")

    def test_t1_does_not_fallback_to_an_unbound_one_argument_adapter(self):
        calls = []

        def legacy_minute_adapter(code):
            calls.append(code)
            return ["09:30 10.00 1 1", "09:45 10.50 1 1"]

        with patch.object(verify_t1, "_verified_next_trading_date", return_value="20261008"), \
             patch.object(verify_t1, "_current_market_date", return_value="20261008"), \
             patch.object(verify_t1, "fetch_minute_data", new=legacy_minute_adapter):
            result = verify_t1.get_morning_945_price("600000", "20261007")

        self.assertFalse(result["verified"])
        self.assertIsNone(result["price_945"])
        self.assertEqual(calls, [])

    def test_exact_target_time_is_accepted_with_verified_date(self):
        bars = ["09:30 10.00 1 1", "09:44 10.40 1 1", "09:45 10.50 1 1"]
        with patch.object(verify_t1, "_verified_next_trading_date", return_value="20261008"), \
             patch.object(verify_t1, "_current_market_date", return_value="20261008"), \
             patch.object(verify_t1, "fetch_minute_data", return_value=bars):
            result = verify_t1.get_morning_945_price("600000", "20261007")

        self.assertTrue(result["verified"])
        self.assertEqual(result["target_date"], "20261008")
        self.assertEqual(result["target_time"], "09:45")
        self.assertEqual(result["price_945"], 10.5)


class T1RealMinuteAdapterEvidenceTests(unittest.TestCase):
    """Exercise the real Tencent adapter through the T+1 diagnostic boundary."""

    FIXED_NOW = RealDateTime(2026, 10, 8, 18, 0, tzinfo=verify_t1.SHANGHAI_TZ)

    @classmethod
    def _freeze_query_quote_clock(cls):
        class FrozenDateTime(RealDateTime):
            @classmethod
            def now(inner_cls, tz=None):
                if tz is None:
                    return cls.FIXED_NOW.replace(tzinfo=None)
                return cls.FIXED_NOW.astimezone(tz)

        return patch.object(query_quote, "datetime", FrozenDateTime)

    def _run_with_response(
        self,
        *,
        response_date="20261008",
        lines=None,
        response_symbol="sh600000",
    ):
        minute_data = {"data": list(lines or [])}
        if response_date is not None:
            minute_data["date"] = response_date
        payload = {"data": {response_symbol: {"data": minute_data}}}
        raw = json.dumps(payload).encode("utf-8")
        with patch.object(verify_t1, "_verified_next_trading_date", return_value="20261008"), \
             patch.object(verify_t1, "_current_market_date", return_value="20261008"), \
             self._freeze_query_quote_clock(), \
             patch.object(query_quote.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            return verify_t1.get_morning_945_price("600000", "20261007")

    def test_matching_response_date_is_verified(self):
        result = self._run_with_response(
            lines=["0930 10.00 1 10", "0945 10.50 1 10"],
        )

        self.assertTrue(result["verified"])
        self.assertEqual(result["data_date"], "20261008")
        self.assertEqual(result["price_945"], 10.5)

    def test_wrong_response_date_is_not_verified(self):
        result = self._run_with_response(
            response_date="20260930",
            lines=["0930 10.00 1 10", "0945 10.50 1 10"],
        )

        self.assertFalse(result["verified"])
        self.assertIsNone(result["price_945"])
        self.assertIsNone(result["data_date"])

    def test_missing_response_date_is_not_verified(self):
        result = self._run_with_response(
            response_date=None,
            lines=["0930 10.00 1 10", "0945 10.50 1 10"],
        )

        self.assertFalse(result["verified"])
        self.assertIsNone(result["price_945"])

    def test_wrong_response_security_key_is_not_verified(self):
        result = self._run_with_response(
            response_symbol="sz600000",
            lines=["0930 10.00 1 10", "0945 10.50 1 10"],
        )

        self.assertFalse(result["verified"])
        self.assertIsNone(result["price_945"])

    def test_missing_exact_target_time_is_not_verified(self):
        result = self._run_with_response(
            lines=["0930 10.00 1 10", "0944 10.40 1 10"],
        )

        self.assertFalse(result["verified"])
        self.assertFalse(result["target_snapshot_found"])
        self.assertIsNone(result["price_945"])

    def test_invalid_target_prices_are_not_verified(self):
        for invalid_price in ("nan", "inf", "-inf", "0", "-1"):
            with self.subTest(price=invalid_price):
                result = self._run_with_response(
                    lines=["0930 10.00 1 10", f"0945 {invalid_price} 1 10"],
                )

                self.assertFalse(result["verified"])
                self.assertFalse(result["target_snapshot_found"])
                self.assertIsNone(result["price_945"])


if __name__ == "__main__":
    unittest.main()
