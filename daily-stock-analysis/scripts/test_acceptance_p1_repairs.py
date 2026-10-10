"""Synthetic regression tests for the independent P1 acceptance findings."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import a_share_daily_screen as screen
import testing_fixtures as fx
from tools import query_financials
import realtime_dashboard as dashboard
from tools.data_sources.cache import JsonCache
from tools.data_sources.announcements import fetch_announcement_evidence
from tools.data_sources.contracts import Result, ResultStatus, result_error, result_ok
from tools.data_sources.tencent import TencentTickSource, aggregate_ticks


class AnnouncementBusinessFailureTests(unittest.TestCase):
    COUNT_CONFLICTS = (
        # Same-layer total aliases disagree.
        {"success": 1, "data": {"list": [], "total": 0, "total_hits": 8}},
        # The outer and nested total_hits fields were previously merged by overwrite.
        {"success": 1, "total_hits": 8, "data": {"list": [], "total_hits": 0}},
    )

    def _fallback(self, rows):
        fallback = Mock()
        fallback.fetch.return_value = result_ok(rows, source="cninfo", source_url="cninfo")
        return fallback

    def test_http_200_business_failure_uses_cninfo_fallback(self) -> None:
        with patch.object(screen, "fetch_json", return_value={"success": False, "code": 429, "data": None}):
            fallback = self._fallback([{"title": "关于重大诉讼的公告"}])
            with patch.object(screen, "CNInfoAnnouncementSource", return_value=fallback):
                self.assertEqual(screen.fetch_announcements("600519"), ["关于重大诉讼的公告"])
        fallback.fetch.assert_called_once()

    def test_valid_empty_primary_does_not_use_fallback(self) -> None:
        with patch.object(screen, "fetch_json", return_value={"success": True, "data": {"list": [], "total": 0}}):
            fallback = self._fallback([{"title": "不应读取"}])
            with patch.object(screen, "CNInfoAnnouncementSource", return_value=fallback):
                self.assertEqual(screen.fetch_announcements("600519"), [])
        fallback.fetch.assert_not_called()

    def test_total_count_conflict_and_missing_title_use_fallback(self) -> None:
        for payload in (
            {"success": True, "data": {"list": [], "total": 3}},
            {"success": 1, "error": "failed", "data": {"list": [], "total_hits": 0}},
            {"success": 1, "data": {"list": [], "total_hits": 8}},
            {"success": 1, "data": {"list": []}},
            {"success": 1, "data": {"list": [], "total_hits": 1.5}},
            {"success": True, "data": {"list": [{"code": "600519"}], "total": 1}},
            *self.COUNT_CONFLICTS,
        ):
            with self.subTest(payload=payload):
                with patch.object(screen, "fetch_json", return_value=payload):
                    fallback = self._fallback([{"title": "备用公告"}])
                    with patch.object(screen, "CNInfoAnnouncementSource", return_value=fallback):
                        self.assertEqual(screen.fetch_announcements("600519"), ["备用公告"])
                    fallback.fetch.assert_called_once()

    def test_page_count_can_differ_from_provider_total(self) -> None:
        rows = [{"title": f"公告 {index}"} for index in range(8)]
        payload = {"success": 1, "data": {"list": rows, "total": 10, "count": 8}}
        with patch.object(screen, "fetch_json", return_value=payload):
            fallback = self._fallback([{"title": "不应读取"}])
            with patch.object(screen, "CNInfoAnnouncementSource", return_value=fallback):
                titles = screen.fetch_announcements("600519", page_size=8)
        self.assertEqual(len(titles), 8)
        fallback.fetch.assert_not_called()

    def _run_real_pipeline(self, primary_payload, fallback_result, risk_cache=None):
        """Run the real screening core with only its external data sources replaced."""
        code = "600519"
        env = fx.SyntheticEnvironment([fx.make_enriched(code, "公告回归")])
        cache = risk_cache if risk_cache is not None else {}
        real_fetch = screen.fetch_announcements
        fallback = Mock()
        fallback.fetch.return_value = fallback_result
        with env.active():
            with (
                patch.object(screen, "fetch_announcements", real_fetch),
                patch.object(screen, "fetch_json", return_value=primary_payload),
                patch.object(screen, "CNInfoAnnouncementSource", return_value=fallback),
                patch.object(screen, "_load_announcement_risk_cache", return_value=cache),
            ):
                result = screen.run_screening_core(
                    screen.ScreeningParams(modes=frozenset({"strict", "low", "watchlist"}), workers=1, top=50),
                    screen.ScreeningHooks(),
                )
        return result, fallback, cache

    def test_business_error_and_incomplete_empty_page_fail_closed_through_pipeline(self) -> None:
        payloads = (
            {"success": 1, "error": "failed", "data": {"list": [], "total_hits": 0}},
            {"success": 1, "data": {"list": [], "total_hits": 8}},
            {"success": 1, "data": {"list": []}},
            *self.COUNT_CONFLICTS,
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result, fallback, cache = self._run_real_pipeline(
                    payload,
                    result_error(ResultStatus.UNAVAILABLE, source="cninfo", source_url="fixture", code="offline", message="fallback offline"),
                )
                self.assertEqual(cache, {}, "双源失败不得写入 clean 缓存")
                self.assertEqual(result["announcement_risk_map"]["600519"], "unknown")
                self.assertIn("600519", result["announcement_unknown_codes"])
                self.assertNotIn("600519", {row["code"] for row in result["dual_pool"]})
                self.assertNotIn("600519", {row["code"] for row in result["strict_trend"]})
                self.assertNotIn("600519", {row["code"] for row in result["capital_rank"]})
                state_rows = [row for row in result["intersection_states"] if row["code"] == "600519"]
                if state_rows:
                    self.assertFalse(state_rows[0]["new_open_eligible"])
                    self.assertFalse(state_rows[0]["actionable"])
                fallback.fetch.assert_called_once()

    def test_successful_cninfo_fallback_is_classified_before_rank_and_state(self) -> None:
        for payload in self.COUNT_CONFLICTS:
            with self.subTest(payload=payload):
                result, fallback, cache = self._run_real_pipeline(
                    payload,
                    result_ok([{"title": "关于重大诉讼的公告"}], source="cninfo", source_url="fixture"),
                )
                self.assertEqual(cache["600519"]["status"], "avoid")
                self.assertEqual(result["announcement_risk_map"]["600519"], "avoid")
                self.assertNotIn("600519", {row["code"] for row in result["dual_pool"]})
                self.assertNotIn("600519", {row["code"] for row in result["strict_trend"]})
                self.assertNotIn("600519", {row["code"] for row in result["capital_rank"]})
                state_rows = [row for row in result["intersection_states"] if row["code"] == "600519"]
                if state_rows:
                    self.assertFalse(state_rows[0]["new_open_eligible"])
                    self.assertFalse(state_rows[0]["actionable"])
                fallback.fetch.assert_called_once()

    def test_conflicting_empty_page_never_persists_clean_to_disk_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "announcement-cache.json"
            cache_path.write_text("{}", encoding="utf-8")
            rows = [{"code": "600519", "name": "测试", "class": "A", "risk": "无"}]
            result = {section: [dict(rows[0])] for section in screen.ANNOUNCEMENT_SECTIONS}
            result["trend_diagnostics"] = []
            fallback = self._fallback_error()
            with (
                patch.object(screen, "ANNOUNCEMENT_CACHE_FILE", cache_path),
                patch.object(screen, "fetch_json", return_value=self.COUNT_CONFLICTS[0]),
                patch.object(screen, "CNInfoAnnouncementSource", return_value=fallback),
            ):
                errors = screen.attach_announcement_risks(result, page_size=8, workers=1)
            self.assertEqual(errors, ["600519"])
            self.assertEqual(result["announcement_risk_map"]["600519"], "unknown")
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8")), {})
            fallback.fetch.assert_called_once()

    def _fallback_error(self):
        fallback = Mock()
        fallback.fetch.return_value = result_error(
            ResultStatus.UNAVAILABLE, source="cninfo", source_url="fixture",
            code="offline", message="fallback offline",
        )
        return fallback

    def test_nested_business_failure_does_not_become_empty_primary(self) -> None:
        for payload in (
            {"success": True, "result": {"success": False, "code": 429, "data": []}},
            {"success": 1, "data": {"error": "failed", "list": [], "total_hits": 0}},
            *self.COUNT_CONFLICTS,
        ):
            with self.subTest(payload=payload):
                fallback = self._fallback([{"title": "备用公告"}])
                result = fetch_announcement_evidence("600519", primary=lambda: payload, fallback=fallback)
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.data["rows"][0]["title"], "备用公告")
                fallback.fetch.assert_called_once()

    def test_shared_adapter_accepts_explicit_empty_page_and_separates_row_count(self) -> None:
        fallback = self._fallback([{"title": "不应读取"}])
        empty = fetch_announcement_evidence(
            "600519",
            primary=lambda: {"success": 1, "data": {"list": [], "total": 0}},
            fallback=fallback,
        )
        self.assertEqual(empty.status, "empty")
        fallback.fetch.assert_not_called()

        rows = [{"title": f"公告 {index}"} for index in range(8)]
        page = fetch_announcement_evidence(
            "600519",
            primary=lambda: {"success": 1, "data": {"list": rows, "total": 10, "count": 8}},
            fallback=fallback,
            page_size=8,
        )
        self.assertEqual(page.status, "ok")
        self.assertEqual(len(page.data["rows"]), 8)
        fallback.fetch.assert_not_called()

    def test_both_sources_fail_and_stale_avoid_is_preserved(self) -> None:
        payloads = (
            {"success": 1, "error": "failed", "data": {"list": [], "total_hits": 0}},
            *self.COUNT_CONFLICTS,
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                stale = {"600519": {"status": "avoid", "keywords": ["立案"], "titles": ["立案调查"], "checked_at": 0}}
                result, fallback, cache = self._run_real_pipeline(
                    payload,
                    result_error(ResultStatus.UNAVAILABLE, source="cninfo", source_url="fixture", code="offline", message="fallback offline"),
                    risk_cache=stale,
                )
                self.assertEqual(result["announcement_risk_map"]["600519"], "avoid")
                self.assertTrue(any(row["code"] == "600519" and row["risk_status"] == "avoid" for row in result["strict_ultra"]))
                self.assertNotIn("600519", {row["code"] for row in result["capital_rank"]})
                self.assertEqual(cache, stale)
                fallback.fetch.assert_called_once()


class FinancialEvidenceTests(unittest.TestCase):
    def test_eastmoney_requests_eps_snapshot_field(self) -> None:
        client = Mock()
        client.get.return_value.json.return_value = {"data": {"f55": "1.25"}}
        _, error = query_financials._eastmoney_snapshot(client, query_financials.normalize_security("600519"))
        self.assertIsNone(error)
        fields = client.get.call_args.kwargs["params"]["fields"].split(",")
        self.assertIn("f55", fields)

    def test_snapshot_uses_eps_and_net_profit_growth_not_profit_margins(self) -> None:
        snapshot = {"f43": "1000", "f162": "1000", "f55": "1.25", "f185": "-8.5", "f186": "42", "f187": "18"}
        with (
            patch.object(query_financials, "_eastmoney_snapshot", return_value=(snapshot, None)),
            patch.object(query_financials, "_tencent_snapshot", return_value=({}, None)),
            patch.object(query_financials, "_ytd", return_value=(0.0, None, "fixture")),
            patch.object(
                query_financials.SinaFinancialSource,
                "fetch_reports",
                return_value=result_ok([], source="sina", source_url="fixture"),
            ),
        ):
            profile = query_financials.query_financial_profile("600519", client=Mock())

        self.assertEqual(profile["eps_snapshot"], 1.25)
        self.assertEqual(profile["net_profit_growth"], -8.5)
        self.assertIsNone(profile["eps"], "快照 EPS 不得冒充最新披露 EPS")
        self.assertEqual(profile["profit_evidence_status"], "unknown")
        self.assertEqual(profile["real_warehouse_financial_gate"], "review_missing_or_conflicting_profit")

    def test_disclosed_profit_growth_takes_precedence_over_snapshot(self) -> None:
        snapshot = {"f43": "1000", "f162": "1000", "f55": "1.25", "f185": "22", "f186": "42", "f187": "18"}
        row = [{
            "report_period": "2026-06-30",
            "基本每股收益": "0.50",
            "归属于母公司所有者的净利润": "100",
            "归属于母公司所有者的净利润_同比": "-3.5",
        }]
        with (
            patch.object(query_financials, "_eastmoney_snapshot", return_value=(snapshot, None)),
            patch.object(query_financials, "_tencent_snapshot", return_value=({}, None)),
            patch.object(query_financials, "_ytd", return_value=(0.0, None, "fixture")),
            patch.object(
                query_financials.SinaFinancialSource,
                "fetch_reports",
                return_value=result_ok(row, source="sina", source_url="fixture"),
            ),
        ):
            profile = query_financials.query_financial_profile("600519", client=Mock())

        self.assertEqual(profile["net_profit_growth"], -3.5)
        self.assertEqual(profile["eps"], 0.5)
        self.assertEqual(profile["real_warehouse_financial_gate"], "eligible_financial_evidence")

    def test_missing_snapshot_fields_remain_unknown_and_do_not_use_margins(self) -> None:
        for missing in (None, "", "-"):
            with self.subTest(missing=missing):
                snapshot = {"f43": "1000", "f162": "1000", "f55": missing, "f185": missing, "f186": "42", "f187": "18"}
                with (
                    patch.object(query_financials, "_eastmoney_snapshot", return_value=(snapshot, None)),
                    patch.object(query_financials, "_tencent_snapshot", return_value=({}, None)),
                    patch.object(query_financials, "_ytd", return_value=(0.0, None, "fixture")),
                    patch.object(
                        query_financials.SinaFinancialSource,
                        "fetch_reports",
                        return_value=result_ok([], source="sina", source_url="fixture"),
                    ),
                ):
                    profile = query_financials.query_financial_profile("600519", client=Mock())
                self.assertIsNone(profile["eps_snapshot"])
                self.assertIsNone(profile["net_profit_growth"])
                self.assertEqual(profile["profit_evidence_status"], "unknown")

    def test_yoy_fields_are_not_used_as_actual_values(self) -> None:
        result = Result(
            status=ResultStatus.OK,
            data=[{"report_period": "2026-06-30", "基本每股收益_同比": "-10", "归母净利润_同比": "-30"}],
            source="fixture",
            source_url="fixture",
        )
        disclosed = query_financials._disclosed_financials(result, as_of="2026-10-04")
        self.assertIsNone(disclosed["eps_disclosed"])
        self.assertIsNone(disclosed["net_profit_disclosed"])
        self.assertEqual(disclosed["profit_evidence_status"], "unknown")

    def test_conflicting_eps_and_profit_and_old_period_cannot_be_safe(self) -> None:
        current = Result(
            status=ResultStatus.OK,
            data=[{
                "report_period": "2026-06-30",
                "基本每股收益": "-0.20",
                "归属于母公司所有者的净利润": "100",
            }],
            source="fixture",
            source_url="fixture",
        )
        disclosed = query_financials._disclosed_financials(current, as_of="2026-10-04")
        self.assertEqual(disclosed["profit_evidence_status"], "unknown")
        self.assertIn("矛盾", disclosed["profit_basis"])

        old = Result(
            status=ResultStatus.OK,
            data=[{"report_period": "2025-12-31", "基本每股收益": "1.20", "归属于母公司所有者的净利润": "100"}],
            source="fixture",
            source_url="fixture",
        )
        old_disclosed = query_financials._disclosed_financials(old, as_of="2026-10-04")
        self.assertEqual(old_disclosed["profit_evidence_status"], "unknown")

    def test_non_positive_eps_never_gets_safe_advice(self) -> None:
        row = [{"report_period": "2026-06-30", "基本每股收益": "0", "归属于母公司所有者的净利润": "100"}]
        with (
            patch.object(query_financials, "_eastmoney_snapshot", return_value=({"f43": "1000", "f162": "1000"}, None)),
            patch.object(query_financials, "_tencent_snapshot", return_value=({}, None)),
            patch.object(query_financials, "_ytd", return_value=(0.0, None, "fixture")),
            patch.object(query_financials.SinaFinancialSource, "fetch_reports", return_value=result_ok(row, source="sina", source_url="sina")),
        ):
            profile = query_financials.query_financial_profile("600519", client=Mock())
        self.assertNotIn("安全", profile["safety_advice"])
        self.assertIn("真实仓暂不开", profile["safety_advice"])

    def test_loss_advice_is_an_explicit_real_warehouse_gate(self) -> None:
        row = [{"report_period": "2026-06-30", "基本每股收益": "-0.20", "归属于母公司所有者的净利润": "-100"}]
        with (
            patch.object(query_financials, "_eastmoney_snapshot", return_value=({"f43": "1000", "f162": "1000"}, None)),
            patch.object(query_financials, "_tencent_snapshot", return_value=({}, None)),
            patch.object(query_financials, "_ytd", return_value=(0.0, None, "fixture")),
            patch.object(query_financials.SinaFinancialSource, "fetch_reports", return_value=result_ok(row, source="sina", source_url="sina")),
        ):
            profile = query_financials.query_financial_profile("600519", client=Mock())
        self.assertIn("真实仓暂不开", profile["safety_advice"])
        self.assertNotIn("不宜重仓", profile["safety_advice"])
        self.assertEqual(profile["real_warehouse_financial_gate"], "blocked_loss_or_negative_pe")

    def test_positive_financial_evidence_has_no_unwritten_pe60_cutoff(self) -> None:
        row = [{"report_period": "2026-06-30", "基本每股收益": "1.00", "归属于母公司所有者的净利润": "100"}]
        for pe_raw in ("5999", "6000"):
            with (
                self.subTest(pe_raw=pe_raw),
                patch.object(query_financials, "_eastmoney_snapshot", return_value=({"f43": "1000", "f162": pe_raw}, None)),
                patch.object(query_financials, "_tencent_snapshot", return_value=({}, None)),
                patch.object(query_financials, "_ytd", return_value=(0.0, None, "fixture")),
                patch.object(query_financials.SinaFinancialSource, "fetch_reports", return_value=result_ok(row, source="sina", source_url="sina")),
            ):
                profile = query_financials.query_financial_profile("600519", client=Mock())
            self.assertEqual(profile["real_warehouse_financial_gate"], "eligible_financial_evidence")
            self.assertEqual(profile["profit_evidence_status"], "profit")


class TickCoverageTests(unittest.TestCase):
    def test_two_ticks_one_minute_apart_do_not_cover_fifteen_minute_window(self) -> None:
        result = aggregate_ticks(
            [
                {"time": "10:00:00", "amount": 100, "side": "B"},
                {"time": "10:01:00", "amount": 100, "side": "S"},
            ],
            window_minutes=15,
            as_of="100100",
        )
        self.assertFalse(result["data_sufficient"])
        self.assertIn("覆盖不足", result["reason"])

    def test_sparse_rows_do_not_prove_a_five_minute_window_later_in_session(self) -> None:
        result = aggregate_ticks(
            [
                {"time": "10:00:00", "amount": 100, "side": "B"},
                {"time": "10:01:00", "amount": 100, "side": "S"},
            ],
            window_minutes=5,
            as_of="100100",
        )
        self.assertFalse(result["data_sufficient"])

    def test_snapshot_close_time_is_normalized_before_amount_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = TencentTickSource(cache=JsonCache("p1_ticks", path=Path(directory) / "cache.json"), sleep_seconds=0)
            source._snapshot = Mock(return_value={
                "data_date": "2026-10-04", "as_of": "153000", "amount": 100_000,
                "name": "测试股", "price": 10.0,
            })
            source._page = Mock(side_effect=[
                [{"seq": 1, "time": "09:30:00", "price": 10.0, "change": 0.0, "volume": 1, "amount": 100, "side": "B"}],
                None,
            ])
            result = source.fetch("600519", max_pages=4)
        self.assertTrue(any("成交额" in warning for warning in result.warnings))

    def test_amount_check_uses_auction_plus_continuous_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = TencentTickSource(cache=JsonCache("p1_ticks_auction", path=Path(directory) / "cache.json"), sleep_seconds=0)
            source._snapshot = Mock(return_value={
                "data_date": "2026-10-04", "as_of": "153000", "amount": 30_000,
                "name": "测试股", "price": 10.0,
            })
            source._page = Mock(side_effect=[
                [
                    {"seq": 1, "time": "09:25:00", "price": 10.0, "change": 0.0, "volume": 1, "amount": 10_000, "side": "M"},
                    {"seq": 2, "time": "09:30:00", "price": 10.0, "change": 0.0, "volume": 1, "amount": 10_000, "side": "B"},
                    {"seq": 3, "time": "09:31:00", "price": 10.0, "change": 0.0, "volume": 1, "amount": 10_000, "side": "S"},
                ],
                None,
            ])
            result = source.fetch("600519", max_pages=4)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["auction_amount"], 10_000)
        self.assertEqual(result.data["session_amount"], 30_000)
        self.assertFalse(any("成交额" in warning for warning in result.warnings))


class TradingDayRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard._TRADING_DAY_CACHE.update(date=None, value=True, source=None, checked_at=0.0)

    def tearDown(self) -> None:
        dashboard._TRADING_DAY_CACHE.update(date=None, value=True, source=None, checked_at=0.0)

    def test_unavailable_official_calendar_is_retried(self) -> None:
        now = dashboard.datetime(2026, 10, 2, 10, 0)
        with patch.object(dashboard, "_fetch_official_calendar_day", side_effect=[None, (False, "szse_official")]), patch.object(dashboard, "_fetch_index_kline_dates", return_value=[]):
            self.assertTrue(dashboard.is_trading_day(now))
            dashboard._TRADING_DAY_CACHE["checked_at"] -= dashboard.TRADING_DAY_RETRY_SECONDS + 1
            self.assertFalse(dashboard.is_trading_day(now))


if __name__ == "__main__":
    unittest.main()
