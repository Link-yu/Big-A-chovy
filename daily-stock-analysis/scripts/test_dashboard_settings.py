import json
import tempfile
import threading
import unittest
from pathlib import Path

import dashboard_settings as ds


class DashboardSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dashboard_settings.json"

    # ---- 默认与持久化 ----
    def test_missing_file_returns_strict_defaults(self):
        config, error = ds.load(self.path)
        self.assertIsNone(error)
        self.assertEqual(config["dashboard"]["negative_super_view"], ds.VIEW_STRICT)
        self.assertEqual(config["revision"], 1)

    def test_apply_persists_and_bumps_revision(self):
        result = ds.apply(
            {"revision": 1, "dashboard": {
                "negative_super_view": ds.VIEW_OBSERVE, "top": 20,
                "interval": 120, "network_mode": "direct",
            }},
            self.path,
        )
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["config"]["revision"], 2)
        self.assertTrue(self.path.exists())

        reloaded, error = ds.load(self.path)
        self.assertIsNone(error)
        self.assertEqual(reloaded["revision"], 2)
        self.assertEqual(reloaded["dashboard"]["negative_super_view"], ds.VIEW_OBSERVE)
        self.assertEqual(reloaded["dashboard"]["top"], 20)
        self.assertEqual(ds.run_params(reloaded)["network_mode"], "direct")

    def test_atomic_write_leaves_no_temp_file(self):
        ds.apply({"revision": 1, "dashboard": dict(ds.DEFAULT_DASHBOARD)}, self.path)
        leftovers = [p.name for p in self.path.parent.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    # ---- 版本冲突 ----
    def test_revision_conflict_rejected(self):
        ds.apply({"revision": 1, "dashboard": dict(ds.DEFAULT_DASHBOARD)}, self.path)
        result = ds.apply(
            {"revision": 1, "dashboard": {**ds.DEFAULT_DASHBOARD, "top": 30}}, self.path)
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        # 冲突不得改写文件
        reloaded, _ = ds.load(self.path)
        self.assertEqual(reloaded["revision"], 2)
        self.assertEqual(reloaded["dashboard"]["top"], 15)

    # ---- 校验 ----
    def test_invalid_values_rejected(self):
        bad_cases = [
            {"negative_super_view": "loose"},
            {"top": 2},
            {"top": 51},
            {"interval": 5},
            {"network_mode": "offline"},
            {"bogus": 1},
        ]
        for patch in bad_cases:
            with self.subTest(patch=patch):
                result = ds.apply(
                    {"revision": 1, "dashboard": {**ds.DEFAULT_DASHBOARD, **patch}}, self.path)
                self.assertFalse(result["ok"], patch)
                self.assertFalse(self.path.exists())

    def test_type_coercion_not_applied(self):
        """字符串数字必须拒绝，不做静默转换。"""
        result = ds.apply(
            {"revision": 1, "dashboard": {**ds.DEFAULT_DASHBOARD, "top": "20"}}, self.path)
        self.assertFalse(result["ok"])

    def test_revision_is_required(self):
        """必须带正整数 revision，否则无法做并发版本校验。"""
        for bad in (None, "1", 0, -1, True, 1.5):
            with self.subTest(revision=bad):
                payload = {"dashboard": dict(ds.DEFAULT_DASHBOARD)}
                if bad is not None:
                    payload["revision"] = bad
                result = ds.apply(payload, self.path)
                self.assertFalse(result["ok"], bad)
                self.assertFalse(result.get("conflict"), bad)
                self.assertFalse(self.path.exists(), bad)

    def test_concurrent_submissions_only_one_wins(self):
        """并发提交同一版本：锁内「比较→写入」保证只有一个成功，其余全部版本冲突。"""
        size = 8
        results: list = []
        barrier = threading.Barrier(size)

        def attempt(i: int) -> None:
            barrier.wait()
            results.append(ds.apply(
                {"revision": 1, "dashboard": {**ds.DEFAULT_DASHBOARD, "top": 10 + i}},
                self.path,
            ))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(size)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r["ok"]]
        conflicts = [r for r in results if r.get("conflict")]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), size - 1)
        reloaded, error = ds.load(self.path)
        self.assertIsNone(error)
        self.assertEqual(reloaded["revision"], 2)

    # ---- 损坏回退 ----
    def test_corrupt_file_falls_back_with_error(self):
        self.path.write_text("{not json", encoding="utf-8")
        config, error = ds.load(self.path)
        self.assertIsNotNone(error)
        self.assertEqual(config["dashboard"]["negative_super_view"], ds.VIEW_STRICT)

    def test_invalid_stored_dashboard_falls_back(self):
        self.path.write_text(json.dumps({
            "schema_version": 1, "revision": 3,
            "dashboard": {**ds.DEFAULT_DASHBOARD, "top": 999},
        }), encoding="utf-8")
        config, error = ds.load(self.path)
        self.assertIsNotNone(error)
        self.assertEqual(config["revision"], 1)

    def test_unsupported_schema_version_falls_back(self):
        self.path.write_text(json.dumps({
            "schema_version": 99, "revision": 3, "dashboard": dict(ds.DEFAULT_DASHBOARD),
        }), encoding="utf-8")
        config, error = ds.load(self.path)
        self.assertIsNotNone(error)

    # ---- 便捷读取 ----
    def test_view_of_and_run_params_tolerate_junk(self):
        self.assertEqual(ds.view_of(None), ds.VIEW_STRICT)
        self.assertEqual(ds.view_of({"dashboard": {"negative_super_view": "x"}}), ds.VIEW_STRICT)
        params = ds.run_params({"dashboard": {"top": "x", "interval": None, "network_mode": "y"}})
        self.assertEqual(params["top"], 15)
        self.assertEqual(params["interval"], 90)
        self.assertEqual(params["network_mode"], "auto")


if __name__ == "__main__":
    unittest.main()
