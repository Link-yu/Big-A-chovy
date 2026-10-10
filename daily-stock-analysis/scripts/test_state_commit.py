# -*- coding: utf-8 -*-
"""回合状态提交门与看板超时行为的回归（R1）。

要求：
- 看板宣布一轮超时/失败之后，该轮不能再提交运行状态；
- 后一轮不与旧轮并发交叉覆盖同一状态；
- 降级/不完整快照不得清空上一份有效状态；
- 状态文件始终可解析（原子写）。

全部走内存与临时目录，不读写真实状态。
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import state_commit
import runtime_paths

import realtime_dashboard as dash
import realtime_engine
import a_share_daily_screen as screen


class CommitGateTests(unittest.TestCase):
    def test_abort_drops_staged_write_and_blocks_late_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            rc = state_commit.RoundCommit("t")
            self.assertTrue(rc.stage(path, lambda: state_commit.atomic_write_text(path, "LATE")))
            self.assertTrue(rc.abort())
            # 中止后再次 stage / commit 都是空操作
            self.assertFalse(rc.stage(path, lambda: state_commit.atomic_write_text(path, "LATE2")))
            self.assertFalse(rc.commit())
            self.assertFalse(path.exists(), "被中止的轮次不得写正式状态")

    def test_commit_applies_staged_writes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            rc = state_commit.RoundCommit("t")
            rc.stage(path, lambda: state_commit.atomic_write_text(path, json.dumps({"ok": 1})))
            self.assertTrue(rc.commit())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": 1})
            # 提交后不可再次提交
            self.assertFalse(rc.commit())

    def test_abort_during_commit_is_serialized(self):
        """stage/commit/abort 由同一把锁保护：要么整轮提交，要么整轮丢弃。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            rc = state_commit.RoundCommit("t")
            started = threading.Event()
            release = threading.Event()

            def slow_write():
                started.set()
                release.wait(timeout=5)
                state_commit.atomic_write_text(path, "X")

            rc.stage(path, slow_write)
            committing = threading.Thread(target=rc.commit)
            committing.start()
            self.assertTrue(started.wait(timeout=5))
            # abort 必须等 commit 完成（同一把锁），不能切进半个提交
            aborted = rc.abort()
            release.set()
            committing.join(timeout=5)
            self.assertFalse(aborted, "commit 已开始则 abort 不应谎报拦下")
            self.assertTrue(path.exists())

    def test_save_functions_stage_until_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow_path = Path(tmp) / "flow_snapshot.json"
            inter_path = Path(tmp) / "intersection_state.json"
            with mock.patch.object(screen, "FLOW_SNAPSHOT_PATH", flow_path), \
                 mock.patch.object(screen, "INTERSECTION_STATE_FILE", inter_path):
                rc = state_commit.RoundCommit("t")
                screen.save_flow_history([], {"600001": [{"ts": time.time()}]}, rc)
                screen.save_intersection_state({"600001": {"phase": "OBSERVING"}}, "2026-10-02", rc)
                self.assertFalse(flow_path.exists(), "未提交的轮次不得写资金基准")
                self.assertFalse(inter_path.exists(), "未提交的轮次不得写交集状态")
                self.assertTrue(rc.commit())
                self.assertTrue(flow_path.exists())
                self.assertEqual(json.loads(inter_path.read_text(encoding="utf-8"))["date"], "2026-10-02")


def _bare_scheduler(latest=None) -> dash.ScreeningScheduler:
    s = dash.ScreeningScheduler.__new__(dash.ScreeningScheduler)
    s.latest_result = latest
    s.last_run_time = None
    s.last_run_duration = None
    s.preserve_snapshot = False
    s.preserved_from = None
    s.last_degraded_attempt = None
    s.is_running = False
    s.is_prewarming = False
    s.prewarm_progress = {"done": 0, "total": 0, "failed": 0}
    s.latest_md_path = None
    s.proxy_unavailable = False
    s._screening_lock = threading.Lock()
    s._prewarm_lock = threading.Lock()
    s._stop_event = threading.Event()
    s.settings = {
        "skip_capital_ranking": False, "network_mode": "auto",
        "auto_refresh": True, "auto_shutdown": True, "interval": 90, "top": 15,
    }
    s.config, s.config_error = {"revision": 3, "dashboard": {"negative_super_view": "strict", "top": 15}}, None
    return s


VALID_PREV = {"meta": {"timestamp": "2026-10-02 10:00:00", "market_fetch_complete": True,
                       "market_data_degraded": False}, "strict_ultra": []}


class DashboardTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.staged = self.root / "flow_snapshot.json"

    def _run(self, fake_engine, timeout=0.25):
        sched = _bare_scheduler(latest=dict(VALID_PREV))
        with mock.patch.object(dash.network_path, "has_working_path", return_value=True), \
             mock.patch.object(dash, "_inject_proxy_to_session", return_value=None), \
             mock.patch.object(dash, "is_trading_hours", return_value=False), \
             mock.patch.object(dash, "SCREENING_TIMEOUT", timeout), \
             mock.patch.object(sched, "_save_markdown", return_value=None), \
             mock.patch.object(sched, "_save_last_valid", return_value=None), \
             mock.patch.object(realtime_engine, "run_screening", side_effect=fake_engine):
            ok = sched.run_screening()
        return sched, ok

    def test_timed_out_round_cannot_commit_state(self):
        release = threading.Event()

        def fake_engine(**kwargs):
            commit = kwargs["state_commit"]
            commit.stage(self.staged, lambda: state_commit.atomic_write_text(self.staged, "LATE"))
            release.wait(timeout=5)      # 阻塞到超时发生之后
            commit.commit()              # 旧轮事后提交：必须是空操作
            return {"meta": {"timestamp": "2026-10-02 10:30:00",
                             "market_fetch_complete": True, "market_data_degraded": False}}

        sched, ok = self._run(fake_engine)
        self.assertTrue(ok)
        self.assertFalse(self.staged.exists(), "超时轮不得提交运行状态")
        self.assertEqual(sched.latest_result["meta"]["timestamp"], "2026-10-02 10:00:00")
        self.assertTrue(sched.preserve_snapshot)
        # 放行旧线程：事后提交仍不得落盘
        release.set()
        time.sleep(0.3)
        self.assertFalse(self.staged.exists(), "旧轮事后提交必须被拦下")

    def test_successful_round_commits_state(self):
        def fake_engine(**kwargs):
            commit = kwargs["state_commit"]
            commit.stage(self.staged, lambda: state_commit.atomic_write_text(self.staged, "OK"))
            return {"meta": {"timestamp": "2026-10-02 10:30:00",
                             "market_fetch_complete": True, "market_data_degraded": False},
                    "strict_ultra": []}

        _sched, ok = self._run(fake_engine)
        self.assertTrue(ok)
        self.assertTrue(self.staged.exists(), "成功轮必须提交状态")
        self.assertEqual(self.staged.read_text(encoding="utf-8"), "OK")

    def test_degraded_round_does_not_clear_previous_state(self):
        def fake_engine(**kwargs):
            commit = kwargs["state_commit"]
            commit.stage(self.staged, lambda: state_commit.atomic_write_text(self.staged, "DEGRADED"))
            return {"meta": {"timestamp": "2026-10-02 10:30:00",
                             "market_fetch_complete": True, "market_data_degraded": True},
                    "strict_ultra": []}

        _sched, ok = self._run(fake_engine)
        self.assertTrue(ok)
        self.assertFalse(self.staged.exists(), "降级快照不得提交状态（不得清空上一份有效状态）")

    def test_sina_probe_allows_engine_but_degraded_result_preserves_snapshot(self):
        previous = dict(VALID_PREV)
        sched = _bare_scheduler(latest=previous)

        def fake_engine(**kwargs):
            kwargs["state_commit"].stage(
                self.staged,
                lambda: state_commit.atomic_write_text(self.staged, "DEGRADED"),
            )
            return {
                "meta": {
                    "timestamp": "2026-10-05 10:30:00",
                    "market_fetch_complete": False,
                    "market_data_degraded": True,
                },
                "strict_ultra": [],
            }

        with mock.patch.object(dash.network_path, "has_working_path", return_value=False), \
             mock.patch.object(dash, "_sina_reachable", return_value=True), \
             mock.patch.object(dash, "_inject_proxy_to_session"), \
             mock.patch.object(dash, "is_trading_hours", return_value=False), \
             mock.patch.object(dash, "SCREENING_TIMEOUT", 0.5), \
             mock.patch.object(sched, "_save_markdown"), \
             mock.patch.object(sched, "_save_last_valid"), \
             mock.patch.object(realtime_engine, "run_screening", side_effect=fake_engine) as engine:
            ok = sched.run_screening()

        self.assertTrue(ok)
        engine.assert_called_once()
        self.assertIs(sched.latest_result, previous)
        self.assertTrue(sched.preserve_snapshot)
        self.assertFalse(sched.proxy_unavailable, "Sina probe succeeded; do not report all providers unavailable")
        self.assertFalse(self.staged.exists(), "fallback/degraded data cannot advance committed state")

    def test_dual_provider_failure_preserves_snapshot_and_skips_engine(self):
        previous = dict(VALID_PREV)
        sched = _bare_scheduler(latest=previous)
        with mock.patch.object(dash.network_path, "has_working_path", return_value=False), \
             mock.patch.object(dash, "_sina_reachable", return_value=False), \
             mock.patch.object(realtime_engine, "run_screening") as engine:
            ok = sched.run_screening()

        self.assertTrue(ok)
        engine.assert_not_called()
        self.assertIs(sched.latest_result, previous)
        self.assertTrue(sched.preserve_snapshot)
        self.assertTrue(sched.proxy_unavailable)

    def test_sina_probe_success_does_not_allow_late_commit_after_timeout(self):
        sched = _bare_scheduler(latest=dict(VALID_PREV))
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def fake_engine(**kwargs):
            commit = kwargs["state_commit"]
            commit.stage(self.staged, lambda: state_commit.atomic_write_text(self.staged, "LATE"))
            entered.set()
            release.wait(timeout=2)
            commit.commit()
            finished.set()
            return {"meta": {"timestamp": "2026-10-05 10:30:00",
                             "market_fetch_complete": True, "market_data_degraded": False}}

        with mock.patch.object(dash.network_path, "has_working_path", return_value=False), \
             mock.patch.object(dash, "_sina_reachable", return_value=True), \
             mock.patch.object(dash, "_inject_proxy_to_session"), \
             mock.patch.object(dash, "SCREENING_TIMEOUT", 0.05), \
             mock.patch.object(realtime_engine, "run_screening", side_effect=fake_engine):
            ok = sched.run_screening()

        self.assertTrue(entered.wait(timeout=0.2))
        self.assertTrue(ok)
        self.assertTrue(sched.preserve_snapshot)
        self.assertFalse(sched.proxy_unavailable)
        self.assertFalse(self.staged.exists())
        release.set()
        self.assertTrue(finished.wait(timeout=1))
        self.assertFalse(self.staged.exists(), "超时后的工作线程不得提交状态")

    def test_engine_round_never_cross_writes_new_round(self):
        """旧轮超时后，新一轮的提交不受旧轮影响，且旧轮无法再写。"""
        sched = _bare_scheduler(latest=dict(VALID_PREV))
        first_release = threading.Event()
        rounds = []

        def fake_engine(**kwargs):
            commit = kwargs["state_commit"]
            idx = len(rounds)
            rounds.append(commit)
            target = self.root / f"round{idx}.json"
            commit.stage(target, lambda: state_commit.atomic_write_text(target, f"R{idx}"))
            if idx == 0:
                first_release.wait(timeout=5)
            commit.commit()
            return {"meta": {"timestamp": f"2026-10-02 10:3{idx}:00",
                             "market_fetch_complete": True, "market_data_degraded": False},
                    "strict_ultra": []}

        with mock.patch.object(dash.network_path, "has_working_path", return_value=True), \
             mock.patch.object(dash, "_inject_proxy_to_session", return_value=None), \
             mock.patch.object(dash, "is_trading_hours", return_value=False), \
             mock.patch.object(dash, "SCREENING_TIMEOUT", 0.25), \
             mock.patch.object(sched, "_save_markdown", return_value=None), \
             mock.patch.object(sched, "_save_last_valid", return_value=None), \
             mock.patch.object(realtime_engine, "run_screening", side_effect=fake_engine):
            sched.run_screening()      # 第 1 轮：超时
            sched.run_screening()      # 第 2 轮：正常提交
            first_release.set()
            time.sleep(0.3)

        round0 = self.root / "round0.json"
        round1 = self.root / "round1.json"
        self.assertFalse(round0.exists(), "被中止的旧轮不得在事后写入")
        self.assertTrue(round1.exists(), "新一轮状态必须正常提交")
        self.assertEqual(round1.read_text(encoding="utf-8"), "R1")


if __name__ == "__main__":
    unittest.main()
