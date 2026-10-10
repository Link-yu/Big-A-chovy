# -*- coding: utf-8 -*-
"""CLI 与看板共用核心流水线的行为回归（R2 / R5 / R6）。

覆盖：
- ``top`` 只影响展示行数；完整候选集合、公告核验范围、双池交集、准交集与
  交集状态推进对 top 不变。
- 资金优选在公告核验之后、用同一份 flow_history 与同一候选集合排序；
  CLI 与看板入口对关键字段一致。

全部使用合成行情，不联网、不读写真实运行状态。
"""

import unittest

import testing_fixtures as fx


def _universe():
    """5 只正式候选 + 1 只距趋势确认仅差一项的准交集候选。"""
    enr = [fx.make_enriched(f"60010{i}", f"正式{i}") for i in range(5)]
    enr.append(fx.make_enriched("600200", "准交集", dist60=0.15))
    return enr


class TopInvarianceTests(unittest.TestCase):
    """R2：调整页面显示条数不得改变状态机输入。"""

    def _results(self):
        enr = _universe()
        risk = {e.code: "clean" for e in enr}
        out = {}
        for top in (3, 15, 50):
            env = fx.SyntheticEnvironment(enr, risk_by_code=risk)
            out[top] = env.run_cli(["--mode", "all", "--format", "json", "--top", str(top)])
        return out

    def test_computed_fields_are_top_invariant(self):
        results = self._results()
        base = results[50]
        for top in (3, 15):
            with self.subTest(top=top):
                cur = results[top]
                for field in (
                    "dual_pool", "dual_pool_raw", "pre_intersection",
                    "intersection_states", "trend_diagnostics",
                ):
                    self.assertEqual(cur[field], base[field], f"{field} 不应随 top 变化")
                self.assertEqual(
                    cur["announcement_total_count"], base["announcement_total_count"],
                    "公告核验范围必须按完整候选集合确定",
                )
                self.assertEqual(cur["board_scope_candidates"], base["board_scope_candidates"])
                self.assertEqual(fx.phase_map(cur), fx.phase_map(base))

    def test_only_display_rows_are_trimmed(self):
        results = self._results()
        base = results[50]
        self.assertGreater(len(fx.codes(base["strict_ultra"])), 3, "夹具必须多于 3 只才能验证裁剪")
        for top in (3, 15):
            cur = results[top]
            for field in (
                "strict_ultra", "trend_observation", "strict_trend",
                "low_ultra", "low_trend", "capital_rank",
            ):
                cur_codes = fx.codes(cur[field])
                self.assertEqual(cur_codes, fx.codes(base[field])[: len(cur_codes)], field)
                self.assertLessEqual(len(cur_codes), max(top, 15) if "low" in field else top)
        self.assertEqual(len(fx.codes(results[3]["strict_ultra"])), 3)
        self.assertEqual(len(fx.codes(results[15]["strict_ultra"])), len(fx.codes(base["strict_ultra"])))

    def test_pre_intersection_uses_full_ultra_pool(self):
        results = self._results()
        for top in (3, 15, 50):
            with self.subTest(top=top):
                self.assertIn("600200", fx.codes(results[top]["pre_intersection"]))
                self.assertIn("600200", fx.codes(results[top]["trend_diagnostics"]))


class RiskPolicyPipelineTests(unittest.TestCase):
    """R3/R5：公告门禁在池子、资金排名与状态机之间语义一致。"""

    def _run(self):
        enr = [
            fx.make_enriched("600101", "干净"),
            fx.make_enriched("600102", "观察风险"),
            fx.make_enriched("600103", "硬风险"),
            fx.make_enriched("600104", "数据不足"),
        ]
        risk = {
            "600101": "clean",
            "600102": "watch_risk",
            "600103": "avoid",
            "600104": "unknown",
        }
        env = fx.SyntheticEnvironment(enr, risk_by_code=risk)
        return env.run_cli(["--mode", "all", "--format", "json", "--top", "50"])

    def test_avoid_and_unknown_are_kept_out_of_formal_pools(self):
        result = self._run()
        for field in ("strict_trend", "dual_pool", "capital_rank"):
            with self.subTest(field=field):
                self.assertNotIn("600103", fx.codes(result[field]))
                self.assertNotIn("600104", fx.codes(result[field]))
        # watch_risk 仅减分，不因“非 clean”被移出正式资格池
        for field in ("strict_trend", "dual_pool", "capital_rank"):
            with self.subTest(field=field, risk="watch_risk"):
                self.assertIn("600102", fx.codes(result[field]))

    def test_raw_ultra_pool_keeps_everything_for_audit_with_labels(self):
        result = self._run()
        self.assertIn("600103", fx.codes(result["strict_ultra"]))
        self.assertIn("600104", fx.codes(result["strict_ultra"]))
        by_code = {r["code"]: r for r in result["strict_ultra"]}
        self.assertEqual(by_code["600103"]["risk_status"], "avoid")
        self.assertEqual(by_code["600104"]["risk_status"], "unknown")
        self.assertEqual(by_code["600102"]["risk_status"], "watch_risk")

    def test_watch_risk_progresses_like_clean_and_hard_veto_never_becomes_buyable(self):
        result = self._run()
        rows = {r["code"]: r for r in (result.get("intersection_states") or [])}
        # clean 与 watch_risk 相位一致：软风险不阻断状态推进
        self.assertIn("600101", rows)
        self.assertIn("600102", rows)
        self.assertEqual(rows["600101"]["phase_code"], rows["600102"]["phase_code"])
        self.assertEqual(rows["600101"]["new_open_eligible"], rows["600102"]["new_open_eligible"])
        # avoid/unknown 无论落在哪个状态都必须不可新开仓
        for code in ("600103", "600104"):
            if code in rows:
                with self.subTest(code=code):
                    self.assertFalse(rows[code]["new_open_eligible"])
                    self.assertFalse(rows[code]["actionable"])
                    self.assertIn("公告风险否决", rows[code]["entry_block_reason"] or rows[code]["risk_note"])


ROW_FIELDS = (
    "strict_ultra", "trend_observation", "strict_trend", "dual_pool",
    "dual_pool_raw", "pre_intersection", "intersection_states",
    "capital_rank", "low_ultra", "low_trend", "watchlist",
)
SCALAR_FIELDS = ("board_scope_status", "board_scope_candidates", "announcement_risk_map")


def project(row, keys):
    return {k: row.get(k) for k in keys}


class CliEngineParityTests(unittest.TestCase):
    """R6：同一份输入走 CLI 与看板入口，核心字段必须一致。

    看板允许在核心行上追加展示字段（进出场建议、分钟量能、交叉验证等）；因此比较
    方式是「看板的行以 CLI 的字段集合投影后必须逐值相等」，即 CLI 的字段是共同合同，
    看板只能在其上追加，不能改写。
    """

    def _pair(self):
        enr = _universe()
        risk = {e.code: "clean" for e in enr}
        env_cli = fx.SyntheticEnvironment(enr, risk_by_code=risk)
        env_engine = fx.SyntheticEnvironment(enr, risk_by_code=risk)
        cli = env_cli.run_cli(["--mode", "all", "--format", "json", "--top", "15"])
        engine = env_engine.run_engine(
            modes={"strict", "low", "watchlist"}, workers=4, top=15,
            settings_snapshot={"revision": 7, "negative_super_view": "strict", "enabled_boards": ["main"]},
        )
        return cli, engine

    def test_core_rows_match(self):
        cli, engine = self._pair()
        for field in ROW_FIELDS:
            with self.subTest(field=field):
                cli_rows = cli.get(field) or []
                eng_rows = engine.get(field) or []
                self.assertEqual(len(cli_rows), len(eng_rows), f"{field} 行数不一致")
                for idx, (c, e) in enumerate(zip(cli_rows, eng_rows)):
                    self.assertEqual(
                        project(e, c.keys()), c,
                        f"{field}[{idx}] 核心字段被看板入口改写",
                    )
        for field in SCALAR_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(cli.get(field), engine.get(field))
        self.assertEqual(cli["meta"]["timestamp"], engine["meta"]["timestamp"])
        self.assertEqual(cli["meta"]["enabled_boards"], engine["meta"]["enabled_boards"])

    def test_core_row_keys_are_stable_across_entrypoints(self):
        cli, engine = self._pair()
        cli_keys = {k for field in ROW_FIELDS for row in (cli.get(field) or []) for k in row}
        eng_keys = {k for field in ROW_FIELDS for row in (engine.get(field) or []) for k in row}
        self.assertTrue(cli_keys <= eng_keys, f"看板行缺少 CLI 字段：{sorted(cli_keys - eng_keys)}")

    def test_engine_adds_only_documented_extras(self):
        cli, engine = self._pair()
        extra = set(engine) - set(cli)
        self.assertTrue(
            extra <= {
                "market_thermometer", "negative_super_observations", "negative_super_status",
                "negative_super_count", "sticky_tracking", "min5_meta", "minute_fetch_log",
            },
            f"看板额外字段必须是明确定义的展示附加项，出现未记录字段：{sorted(extra)}",
        )
        self.assertEqual(engine["meta"]["config_revision"], 7)
        self.assertFalse(engine["meta"]["announcement_check_skipped"])
        self.assertNotIn("config_revision", cli["meta"])


if __name__ == "__main__":
    unittest.main()
