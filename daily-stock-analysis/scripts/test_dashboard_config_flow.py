import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import dashboard_settings
import realtime_dashboard as dash


def _bare_scheduler() -> dash.ScreeningScheduler:
    """只初始化配置相关字段，避开真实网络/快照恢复分支。"""
    s = dash.ScreeningScheduler.__new__(dash.ScreeningScheduler)
    s.latest_result = None
    s.is_running = False
    s.is_prewarming = False
    s.prewarm_progress = {"done": 0, "total": 0, "failed": 0}
    s.last_run_time = None
    s.last_run_duration = None
    s.latest_md_path = None
    s.preserve_snapshot = False
    s.preserved_from = None
    s.proxy_unavailable = False
    s.settings = {
        "skip_capital_ranking": False,
        "network_mode": "auto", "auto_refresh": True, "auto_shutdown": True,
        "interval": 90, "top": 15,
    }
    s.config, s.config_error = dashboard_settings.load()
    s._apply_config_to_settings()
    return s


class SchedulerConfigFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dashboard_settings.json"
        patcher = mock.patch.object(dashboard_settings, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_view_is_strict_and_params_applied(self):
        s = _bare_scheduler()
        public = s.config_public()
        self.assertEqual(public["revision"], 1)
        self.assertEqual(public["dashboard"]["negative_super_view"], dashboard_settings.VIEW_STRICT)
        self.assertFalse(public["pending"])
        self.assertEqual(s.settings["top"], 15)

    def test_apply_config_persists_and_marks_pending_against_snapshot(self):
        s = _bare_scheduler()
        s.latest_result = {"meta": {"config_revision": 1, "negative_super_view": "strict"}}
        result = s.apply_config({
            "revision": 1,
            "dashboard": {**dashboard_settings.DEFAULT_DASHBOARD,
                          "negative_super_view": dashboard_settings.VIEW_OBSERVE, "top": 20},
        })
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(s.config["revision"], 2)
        self.assertEqual(s.settings["top"], 20)
        self.assertTrue(self.path.exists())

        public = s.config_public()
        self.assertTrue(public["pending"])  # 快照仍按 v1 解读
        self.assertEqual(public["snapshot_view"], "strict")

    def test_apply_config_revision_conflict_keeps_previous(self):
        s = _bare_scheduler()
        s.apply_config({"revision": 1, "dashboard": dict(dashboard_settings.DEFAULT_DASHBOARD)})
        result = s.apply_config({
            "revision": 1,
            "dashboard": {**dashboard_settings.DEFAULT_DASHBOARD, "top": 30},
        })
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        self.assertEqual(s.config["revision"], 2)
        self.assertEqual(s.settings["top"], 15)

    def test_legacy_settings_cannot_bypass_config_validation(self):
        s = _bare_scheduler()
        with self.assertRaises(ValueError):
            s.update_settings({"top": 999})
        # 拒绝后运行参数与配置均未改变
        self.assertEqual(s.settings["top"], 15)
        self.assertEqual(s.config["revision"], 1)

    def test_legacy_settings_valid_config_key_persists(self):
        s = _bare_scheduler()
        s.update_settings({"top": 25})
        self.assertEqual(s.settings["top"], 25)
        reloaded, error = dashboard_settings.load(self.path)
        self.assertIsNone(error)
        self.assertEqual(reloaded["dashboard"]["top"], 25)

    def test_legacy_settings_session_keys_stay_in_memory(self):
        s = _bare_scheduler()
        s.update_settings({"skip_capital_ranking": True})
        self.assertTrue(s.settings["skip_capital_ranking"])
        self.assertFalse(self.path.exists())

    def test_announcement_gate_cannot_be_disabled_via_legacy_settings(self):
        """公告检查是一票否决门禁：旧接口既不能打开也不能关闭它。"""
        s = _bare_scheduler()
        s.update_settings({"skip_announcements": True})
        self.assertNotIn("skip_announcements", s.settings)


class AnnouncementSkippedStatusTests(unittest.TestCase):
    """状态条防呆读「快照实际执行口径」，而不是当前设置。"""

    def _scheduler(self, meta: dict):
        s = _bare_scheduler()
        s.latest_result = {"meta": meta}
        return s

    def test_status_reports_snapshot_execution(self):
        s = self._scheduler({"timestamp": "2026-09-30 11:00:00",
                             "announcement_check_skipped": True})
        self.assertTrue(s.get_status()["announcement_check_skipped"])

    def test_legacy_snapshot_falls_back_to_source_text(self):
        """老快照没有该字段时回退看来源文案，避免漏报已有的跳过快照。"""
        s = self._scheduler({
            "timestamp": "2026-09-30 11:00:00",
            "source": "东方财富push2实时/快照 + 腾讯前复权日K (公告已跳过)",
        })
        self.assertTrue(s.get_status()["announcement_check_skipped"])

    def test_normal_snapshot_is_not_flagged(self):
        s = self._scheduler({"timestamp": "2026-09-30 11:00:00",
                             "announcement_check_skipped": False,
                             "source": "东方财富push2实时/快照 + 腾讯前复权日K"})
        self.assertFalse(s.get_status()["announcement_check_skipped"])

    def test_settings_no_longer_carry_announcement_switch(self):
        s = _bare_scheduler()
        self.assertNotIn("skip_announcements", s.get_status()["settings"])


class ShadowBadgeRefreshTests(unittest.TestCase):
    def setUp(self):
        import tools.detect_divergence_leader as detector
        self.detector = detector

    def test_badge_backfilled_per_code_and_unknown_is_undetermined(self):
        result = {
            "meta": {"timestamp": "2026-09-30 11:00:00"},
            "negative_super_observations": [
                {"code": "000001", "shadow_badge": None},
                {"code": "000002", "shadow_badge": None},
                {"code": "000003", "shadow_badge": None},
            ],
        }
        badges = {
            "000001": {"status": "triggered", "trigger_time": "09:55"},
            "000002": {"status": "not_triggered"},
        }
        with mock.patch.object(self.detector, "evaluate_day_badges", return_value=badges):
            ok = dash.ScreeningScheduler.refresh_shadow_badges(result)
        self.assertTrue(ok)
        rows = {r["code"]: r["shadow_badge"] for r in result["negative_super_observations"]}
        self.assertEqual(rows["000001"]["status"], "triggered")
        self.assertEqual(rows["000002"]["status"], "not_triggered")
        # 不在判定器低吸表中 -> 未完成判定，绝不是“不符合”
        self.assertEqual(rows["000003"]["status"], "undetermined")
        self.assertIn("shadow_badge_updated_at", result["meta"])

    def test_no_observations_means_no_refresh(self):
        self.assertFalse(dash.ScreeningScheduler.refresh_shadow_badges({"meta": {}}))
        self.assertFalse(dash.ScreeningScheduler.refresh_shadow_badges(
            {"meta": {"timestamp": "bad"}, "negative_super_observations": []}))


if __name__ == "__main__":
    unittest.main()
