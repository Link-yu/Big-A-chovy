"""Synthetic regression tests for the P2 data/workbench acceptance findings."""

from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tools.data_sources.background import build_market_background
from tools.data_sources.cache import JsonCache
from tools.data_sources.context import ContextSource, normalize_dragon_tiger, parse_theme_payload
from tools.data_sources.contracts import ResultStatus, result_ok
from tools.data_sources.events import EVENT_TYPES, EastmoneyEventSource
from tools.data_sources.http import HTTPClient, HTTPResponse
from tools.data_sources.sentiment import EastmoneySentimentSource
import realtime_dashboard as dashboard
import realtime_engine


def json_response(url: str, payload: object, *, status: int = 200) -> HTTPResponse:
    return HTTPResponse(status, url, json.dumps(payload, ensure_ascii=False).encode(), {"Content-Type": "application/json"})


class EventConfigAndScopeTests(unittest.TestCase):
    def test_event_configs_use_live_reports_and_date_specific_filters(self) -> None:
        self.assertEqual(EVENT_TYPES["holder_trade"]["report"], "RPT_SHARE_HOLDER_INCREASE")
        self.assertEqual(EVENT_TYPES["buyback"]["report"], "RPTA_WEB_GETHGLIST_NEW")
        calls = []

        def transport(method, url, **kwargs):
            params = kwargs.get("params") or {}
            calls.append(params.copy())
            report = params["reportName"]
            page = int(params["pageNumber"])
            if report == "RPT_SHARE_HOLDER_INCREASE":
                rows = [{"SECURITY_CODE": "600519", "NOTICE_DATE": "2026-10-02", "TRADE_DATE": "2026-10-01", "DIRECTION": "减持", "CHANGE_NUM_SYMBOL": -10}]
                if page == 2:
                    rows = [{"SECURITY_CODE": "600519", "NOTICE_DATE": "2026-09-01", "TRADE_DATE": "2026-08-31", "DIRECTION": "增持", "CHANGE_NUM_SYMBOL": 5}]
                return json_response(url, {"success": True, "result": {"pages": 2, "data": rows, "count": 2}})
            rows = [{"DIM_SCODE": "600519", "DIM_DATE": "2026-10-01", "REPURSTARTDATE": "2026-10-01", "REPURENDDATE": "2027-01-01", "REPUROBJECTIVE": "注销"}]
            return json_response(url, {"success": True, "result": {"pages": 1, "data": rows, "count": 1}})

        with tempfile.TemporaryDirectory() as directory:
            source = EastmoneyEventSource(
                client=HTTPClient(transport=transport),
                cache=JsonCache("p2_events", path=Path(directory) / "cache.json"),
            )
            result = source.fetch("600519", event_types=["holder_trade", "buyback"], as_of="2026-10-03", limit=2)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.data["rows"]), 3)
        self.assertTrue(all((row.get("notice_date") or row.get("effective_date") or "") <= "2026-10-03" for row in result.data["rows"]))
        holder_call = next(item for item in calls if item["reportName"] == "RPT_SHARE_HOLDER_INCREASE")
        buyback_call = next(item for item in calls if item["reportName"] == "RPTA_WEB_GETHGLIST_NEW")
        self.assertIn("NOTICE_DATE<='2026-10-03'", holder_call["filter"])
        self.assertIn("DIM_SCODE=\"600519\"", buyback_call["filter"])
        self.assertIn("DIM_DATE<='2026-10-03'", buyback_call["filter"])

    def test_historical_events_require_disclosure_before_as_of(self) -> None:
        def transport(method, url, **kwargs):
            report = (kwargs.get("params") or {}).get("reportName")
            if report == "RPT_LIFT_STAGE":
                rows = [
                    {"SECURITY_CODE": "600519", "EUTIME": "2026-10-05", "FREE_DATE": "2026-10-10", "FREE_SHARES": 100},
                    {"SECURITY_CODE": "600519", "EUTIME": "2026-10-02", "FREE_DATE": "2026-10-10", "FREE_SHARES": 200},
                ]
            elif report == "RPT_SHARE_HOLDER_INCREASE":
                rows = [
                    {"SECURITY_CODE": "600519", "NOTICE_DATE": "2026-10-05", "TRADE_DATE": "2026-10-01", "CHANGE_NUM_SYMBOL": 1},
                    {"SECURITY_CODE": "600519", "NOTICE_DATE": "2026-10-02", "TRADE_DATE": "2026-10-01", "CHANGE_NUM_SYMBOL": 2},
                ]
            else:
                rows = []
            return json_response(url, {"success": True, "result": {"pages": 1, "data": rows, "count": len(rows)}})

        with tempfile.TemporaryDirectory() as directory:
            source = EastmoneyEventSource(
                client=HTTPClient(transport=transport),
                cache=JsonCache("p2_events_as_of", path=Path(directory) / "cache.json"),
            )
            result = source.fetch("600519", event_types=["unlock", "holder_trade"], as_of="2026-10-03", force=True)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.data["rows"]), 2)
        self.assertTrue(all(row["notice_date"] <= "2026-10-03" for row in result.data["rows"]))

    def test_known_future_plan_and_forecast_are_retained_but_unknown_notice_is_partial(self) -> None:
        def transport(method, url, **kwargs):
            report = (kwargs.get("params") or {}).get("reportName")
            if report == "RPTA_WEB_GETHGLIST_NEW":
                rows = [{
                    "DIM_SCODE": "600519",
                    "DIM_DATE": "2026-10-01",
                    "NOTICEDATE": "2026-10-01",
                    "REPURSTARTDATE": "2026-10-10",
                    "REPURENDDATE": "2027-01-01",
                    "REPUROBJECTIVE": "注销",
                }]
            elif report == "RPT_PUBLIC_OP_NEWPREDICT":
                rows = [{
                    "SECURITY_CODE": "600519",
                    "NOTICE_DATE": "2026-10-01",
                    "REPORT_DATE": "2026-12-31",
                    "PREDICT_FINANCE": "归属于上市公司股东的净利润",
                    "PREDICT_AMT_LOWER": 100000000,
                    "ADD_AMP_LOWER": 12.5,
                }]
            elif report == "RPT_SHARE_HOLDER_INCREASE":
                rows = [{
                    "SECURITY_CODE": "600519",
                    "TRADE_DATE": "2026-10-01",
                    "CHANGE_NUM_SYMBOL": 1,
                }]
            else:
                rows = []
            return json_response(url, {"success": True, "result": {"pages": 1, "data": rows, "count": len(rows)}})

        with tempfile.TemporaryDirectory() as directory:
            source = EastmoneyEventSource(
                client=HTTPClient(transport=transport),
                cache=JsonCache("p3_future_events", path=Path(directory) / "cache.json"),
            )
            result = source.fetch("600519", event_types=["buyback", "earnings_forecast", "holder_trade"], as_of="2026-10-03", force=True)
        self.assertEqual(result.status, "partial")
        rows = result.data["rows"]
        self.assertEqual({row["event_type"] for row in rows}, {"buyback", "earnings_forecast"})
        self.assertTrue(all(row["notice_date"] <= "2026-10-03" for row in rows))
        self.assertTrue(any(row.get("event_state") == "future_plan" for row in rows))
        self.assertTrue(any(row.get("event_state") == "forecast" for row in rows))
        self.assertTrue(any("披露日期" in warning for warning in result.warnings))

    def test_dragon_tiger_business_failure_is_not_empty_and_filter_has_valid_operator(self) -> None:
        filters = []

        def transport(method, url, **kwargs):
            filters.append((kwargs.get("params") or {}).get("filter"))
            return json_response(url, {"success": False, "code": 429, "result": {"data": []}})

        with tempfile.TemporaryDirectory() as directory:
            source = ContextSource(
                client=HTTPClient(transport=transport),
                cache=JsonCache("p2_context_failure", path=Path(directory) / "cache.json"),
            )
            result = source.fetch("600519", topic="dragon_tiger", as_of="2026-10-03", force=True)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("TRADE_DATE<='2026-10-03'", filters[0])


class CacheContractTests(unittest.TestCase):
    def _sentiment_payload(self, kind: str) -> dict:
        if kind == "limit_up":
            return {"data": {"pool": [{"code": "600519", "name": "样本", "change_pct": 10}]}}
        return {"data": {"pool": []}}

    def test_partial_result_and_warnings_survive_cache_hit(self) -> None:
        calls = []

        def transport(method, url, **kwargs):
            calls.append(url)
            if "ZTPool" in url:
                return json_response(url, self._sentiment_payload("limit_up"))
            return json_response(url, {"data": None}, status=503)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            first = EastmoneySentimentSource(client=HTTPClient(transport=transport), cache=JsonCache("p2_sentiment", path=path)).fetch("2026-10-03")
            second_client = Mock()
            second = EastmoneySentimentSource(client=second_client, cache=JsonCache("p2_sentiment", path=path)).fetch("2026-10-03")
        self.assertEqual(first.status, "partial")
        self.assertEqual(second.status, "partial")
        self.assertEqual(second.warnings, first.warnings)
        second_client.get.assert_not_called()

    def test_failed_result_is_cached_for_short_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            client = HTTPClient(transport=lambda method, url, **kwargs: json_response(url, {"data": None}, status=503))
            first = EastmoneySentimentSource(client=client, cache=JsonCache("p2_failed", path=path)).fetch("2026-10-03")
            client2 = Mock()
            second = EastmoneySentimentSource(client=client2, cache=JsonCache("p2_failed", path=path)).fetch("2026-10-03")
        self.assertEqual(first.status, "unavailable")
        self.assertEqual(second.status, "unavailable")
        client2.get.assert_not_called()

    def test_two_instances_do_not_lose_keys_and_process_can_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache_a = JsonCache("p2_shared", path=path)
            cache_b = JsonCache("p2_shared", path=path)
            barrier = threading.Barrier(2)

            def put(cache, key):
                barrier.wait(timeout=5)
                cache.set(key, {"key": key})

            threads = [threading.Thread(target=put, args=(cache_a, "a")), threading.Thread(target=put, args=(cache_b, "b"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertIsNotNone(cache_a.get("a"))
            self.assertIsNotNone(cache_b.get("b"))

            child = "from_child"
            script = "from pathlib import Path; from tools.data_sources.cache import JsonCache; import sys; JsonCache('p2_shared', path=Path(sys.argv[1])).set('" + child + "', {'ok': True})"
            # Windows 修复（同步 v0.7.1 时补）：本机无 `python3` 命令（9009），
            # 统一用当前解释器，避免测试因可执行文件名而失败。
            child_env = dict(os.environ)
            child_env["PYTHONPATH"] = "."
            subprocess.run([sys.executable, "-c", script, str(path)], check=True, env=child_env)
            self.assertIsNotNone(cache_a.get(child))

    def test_same_key_requests_are_coalesced(self) -> None:
        calls = 0
        call_lock = threading.Lock()

        def transport(method, url, **kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
            time.sleep(0.05)
            return json_response(url, self._sentiment_payload("limit_up"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            source_a = EastmoneySentimentSource(client=HTTPClient(transport=transport), cache=JsonCache("p2_coalesce", path=path))
            source_b = EastmoneySentimentSource(client=HTTPClient(transport=transport), cache=JsonCache("p2_coalesce", path=path))
            results = []
            threads = [threading.Thread(target=lambda source: results.append(source.fetch("2026-10-03")), args=(source,)) for source in (source_a, source_b)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(calls, 3)
        self.assertEqual([result.status for result in results], ["ok", "ok"])


class ContextIdentityAndDateTests(unittest.TestCase):
    def test_theme_board_identity_is_not_filtered_as_stock_code(self) -> None:
        rows = parse_theme_payload({"data": {"diff": [{"f12": "BK1001", "f14": "人工智能"}]}}, code="600519")
        self.assertEqual(rows[0]["board_code"], "BK1001")
        self.assertEqual(rows[0]["code"], "600519")

    def test_dragon_tiger_filters_future_records_and_unmatched_seats(self) -> None:
        data = normalize_dragon_tiger(
            [
                {"SECURITY_CODE": "600519", "TRADE_DATE": "20261002", "EXPLANATION": "涨幅"},
                {"SECURITY_CODE": "600519", "TRADE_DATE": "20261005", "EXPLANATION": "未来"},
            ],
            [
                {"OPERATEDEPT_NAME": "同日席位", "TRADE_DATE": "20261002", "BUY": 100},
                {"OPERATEDEPT_NAME": "未来席位", "TRADE_DATE": "20261005", "BUY": 200},
                {"OPERATEDEPT_NAME": "无日期席位", "BUY": 300},
            ],
            [],
            code="600519",
            as_of="2026-10-03",
        )
        self.assertEqual(data["record_dates"], ["2026-10-02"])
        self.assertEqual([row["name"] for row in data["seats"]["buy"]], ["同日席位"])


class BackgroundBudgetTests(unittest.TestCase):
    def test_background_budget_does_not_wait_for_stalled_sources(self) -> None:
        class SlowCalendar:
            def is_open(self, value):
                time.sleep(1.0)
                return result_ok({}, source="calendar", source_url="calendar")

        class SlowSentiment:
            def fetch(self, value):
                time.sleep(1.0)
                return result_ok({}, source="sentiment", source_url="sentiment")

        started = time.monotonic()
        result = build_market_background("2026-10-03", calendar_service=SlowCalendar(), sentiment_source=SlowSentiment(), budget_seconds=0.1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.6)
        self.assertEqual(result["calendar"]["status"], "unavailable")
        self.assertEqual(result["sentiment"]["status"], "unavailable")

    def test_background_refresh_does_not_hold_screening_lock(self) -> None:
        scheduler = dashboard.ScreeningScheduler()
        started = threading.Event()
        release = threading.Event()

        def slow_background(*args, **kwargs):
            started.set()
            release.wait(timeout=3)
            return {"calendar": {"status": "ok"}, "sentiment": {"status": "ok"}}

        result = {"meta": {"timestamp": "2026-10-03 10:00:00", "market_fetch_complete": True, "market_data_degraded": False}, "strict_ultra": []}
        with (
            patch.object(dashboard.network_path, "has_working_path", return_value=True),
            patch.object(dashboard, "_inject_proxy_to_session"),
            patch.object(dashboard, "is_trading_hours", return_value=False),
            patch.object(dashboard, "build_market_background", side_effect=slow_background),
            patch.object(dashboard, "SCREENING_TIMEOUT", 1),
            patch.object(scheduler, "_save_markdown"),
            patch.object(scheduler, "_save_last_valid"),
            patch.object(realtime_engine, "run_screening", return_value=result),
        ):
            self.assertTrue(scheduler.run_screening())
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(scheduler.run_screening())
            release.set()
            thread = scheduler._background_thread
            if thread is not None:
                thread.join(timeout=2)

    def test_project_http_client_cold_start_process_exit_is_budgeted(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = r'''
import sys
import time
from unittest.mock import patch

sys.path.insert(0, "daily-stock-analysis/scripts")
from tools.data_sources import http
import network_path

def slow_probe(label, proxy):
    time.sleep(1.5)
    return None

with patch.object(network_path, "candidate_paths", return_value=[("直连", None)]), \
     patch.object(network_path, "_probe_one", side_effect=slow_probe):
    started = time.monotonic()
    client = http.project_http_client(deadline=started + 0.5)
    print(type(client).__name__)
    print(time.monotonic() - started)
'''
        started = time.monotonic()
        completed = subprocess.run(
            # Windows 修复（同步 v0.7.1 时补）：本机无 `python3` 命令，用当前解释器。
            [sys.executable, "-c", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        elapsed = time.monotonic() - started
        # 关键断言：客户端在 1.5s 慢探测完成前就返回——用子进程自报的内部耗时判定。
        # 不再断言父进程墙钟 <1.0s：本机实测子进程 启动 0.15s + 导入 0.73s + 客户端
        # 0.70s ≈ 1.6s，其中约 0.9s 与被测性质无关；套件并行负载下 3s 超时也会误杀。
        # 「退出不被慢探测拖住」由墙钟上界（10s）+ 子进程超时共同覆盖。
        reported = float(completed.stdout.strip().splitlines()[-1])
        self.assertLess(reported, 1.5, completed.stdout)
        self.assertLess(elapsed, 10.0, completed.stderr or completed.stdout)
        self.assertIn("HTTPClient", completed.stdout)


if __name__ == "__main__":
    unittest.main()
