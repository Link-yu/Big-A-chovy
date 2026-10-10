"""交易板范围（enabled_boards）的回归测试。

口径（2026-09-30 修正）：**三个交易板都参与完整筛选**，通过配置决定哪些板进入本轮新增候选。
- 默认只选主板 → 与历史基线一致；
- 勾选创业板/科创板 → 与主板走同一条链路（资金增量与状态 → 超短池/趋势池 → 双池交集 →
  资金优选/低吸/明日观察池 → 状态机），使用同一套门槛与排名，不单独放大两板阈值；
- 未勾选的交易板**不得出现在新增候选里**（旧的“正式池非主板零泄漏”已改为“未选交易板零混入”）；
- 已持仓股票不因取消某板而停止监控。
"""

import json
import tempfile
import unittest
from pathlib import Path

import a_share_daily_screen as screen
import dashboard_settings as ds


SCRIPT_DIR = Path(__file__).resolve().parent


def _row(code, name="测试股份", **over):
    base = {"f12": code, "f14": name, "f2": 10.0, "f3": 3.0, "f5": 1000, "f6": 3e8,
            "f8": 5.0, "f10": 2.0, "f15": 10.0, "f18": 9.0, "f21": 5e9, "f100": "测试行业"}
    base.update(over)
    return base


class BoardSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json"

    def test_default_is_main_only(self):
        self.assertEqual(ds.boards_of(ds.default_config()), ["main"])
        self.assertEqual(ds.boards_of(None), ["main"])
        self.assertEqual(ds.boards_of({"dashboard": {"enabled_boards": "garbage"}}), ["main"])

    def test_any_non_empty_combination_and_canonical_order(self):
        dashboard, errors = ds.validate_dashboard(dict(
            ds.DEFAULT_DASHBOARD, enabled_boards=["star", "main", "star"]))
        self.assertEqual(errors, [])
        self.assertEqual(dashboard["enabled_boards"], ["main", "star"])
        for combo in (["main"], ["chinext"], ["star"], ["chinext", "star"]):
            with self.subTest(combo=combo):
                _d, errs = ds.validate_dashboard(dict(ds.DEFAULT_DASHBOARD, enabled_boards=list(combo)))
                self.assertEqual(errs, [])

    def test_empty_and_unknown_are_rejected(self):
        for bad, needle in (([], "至少要选择一个交易板"), (["bse"], "未知取值"), ("main", "必须是数组")):
            with self.subTest(value=bad):
                _d, errs = ds.validate_dashboard(dict(ds.DEFAULT_DASHBOARD, enabled_boards=bad))
                self.assertTrue(any(needle in e for e in errs))

    def test_legacy_config_keeps_other_settings(self):
        self.path.write_text(json.dumps({
            "schema_version": ds.SCHEMA_VERSION, "revision": 7,
            "dashboard": {"negative_super_view": "observe", "top": 20,
                          "interval": 120, "network_mode": "direct"},
        }, ensure_ascii=False), encoding="utf-8")
        config, error = ds.load(self.path)
        self.assertIsNone(error)
        self.assertEqual(ds.boards_of(config), ["main"])
        self.assertEqual(config["dashboard"]["top"], 20)
        self.assertEqual(config["revision"], 7)

    def test_partial_submit_preserves_boards(self):
        first = ds.apply({"revision": 1, "dashboard": dict(
            ds.DEFAULT_DASHBOARD, enabled_boards=["main", "chinext"])}, self.path)
        self.assertTrue(first["ok"])
        second = ds.apply({"revision": first["config"]["revision"], "dashboard": {"top": 20}}, self.path)
        self.assertTrue(second["ok"])
        self.assertEqual(ds.boards_of(second["config"]), ["main", "chinext"])

    def test_request_override_semantics(self):
        self.assertEqual(ds.parse_request_boards(None), (None, None))
        boards, error = ds.parse_request_boards(["bse"])
        self.assertIsNone(boards)
        self.assertTrue(error)


class BoardClassificationTests(unittest.TestCase):
    def test_board_of(self):
        cases = {"600886": "main", "002413": "main", "300750": "chinext", "301029": "chinext",
                 "688981": "star", "689009": "star", "920002": "unknown", "430047": "unknown"}
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(screen.board_of(code), expected)

    def test_normalize_fails_safe_to_main(self):
        for bad in ([], ["bse"], 3, None):
            with self.subTest(value=bad):
                self.assertEqual(screen.normalize_boards(bad), ["main"])

    def test_valid_board_row_membership(self):
        self.assertTrue(screen.valid_board_row(_row("600886")))
        self.assertFalse(screen.valid_board_row(_row("300750")))
        self.assertTrue(screen.valid_board_row(_row("300750"), ["main", "chinext"]))
        self.assertFalse(screen.valid_board_row(_row("300750", "ST测试"), ["main", "chinext"]))


class PrefetchScopeTests(unittest.TestCase):
    """预筛按所选交易板放行：未勾选的板不得进入新增候选。"""

    def test_unselected_boards_never_enter_prefetch(self):
        rows = [_row("600886"), _row("300750"), _row("688981")]
        for boards, allowed in ((["main"], {"600886"}), (["chinext"], {"300750"}),
                                (["star"], {"688981"}), (["chinext", "star"], {"300750", "688981"})):
            with self.subTest(boards=boards):
                out = screen.filter_prefetch(rows, ["all"], boards=boards)
                codes = {r["f12"] for r in out}
                self.assertTrue(codes <= allowed, f"{boards} 混入了 {codes - allowed}")
                self.assertTrue(all(screen.board_of(c) in boards for c in codes))

    def test_chinext_only_can_pass_prefetch(self):
        """只选创业板时，符合现有门槛的创业板股票必须能通过预筛（不再被主板前缀挡住）。"""
        out = screen.filter_prefetch([_row("300750")], ["all"], boards=["chinext"])
        self.assertEqual({r["f12"] for r in out}, {"300750"})

    def test_prewarm_accepts_boards(self):
        import inspect
        import realtime_engine
        self.assertIn("boards", inspect.signature(realtime_engine.prewarm_kline_cache).parameters,
                      "预热必须接收与本轮相同的交易板范围")

    def test_engine_and_cli_share_the_scope(self):
        """范围口径只能有一份：两个入口都委托给同一条核心流水线。"""
        core = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
        engine = (SCRIPT_DIR / "realtime_engine.py").read_text(encoding="utf-8")
        self.assertIn("boards=boards_in_use", core, "核心预筛必须按本轮所选交易板放行")
        self.assertIn("run_screening_core", core)
        self.assertIn("screen.run_screening_core", engine, "看板入口必须复用核心，不得另起一套")
        self.assertNotIn("split_by_board", engine, "主板专属隔离必须解除")
        self.assertNotIn("split_by_board", core)


class ResonanceScopeTests(unittest.TestCase):
    """共振口径：行业背景用全市场，强势股计数只认所选交易板。"""

    def _stats(self, boards):
        rows = [_row("600886", f3=9.9, f6=9e8), _row("300750", f3=9.9, f6=9e8), _row("688981", f3=9.9, f6=9e8)]
        return screen.sector_stats(rows, None, boards=boards)["测试行业"]

    def test_strong_follows_selected_boards(self):
        self.assertEqual(self._stats(["main"])["strong"], 1)
        self.assertEqual(self._stats(["main", "chinext"])["strong"], 2)
        self.assertEqual(self._stats(["main", "chinext", "star"])["strong"], 3)
        self.assertEqual(self._stats(["chinext"])["strong"], 1)

    def test_industry_background_stays_full_market(self):
        """上涨比例与样本数仍是全市场背景，不随交易板范围变化。"""
        main_only = self._stats(["main"])
        all_boards = self._stats(["main", "chinext", "star"])
        self.assertEqual(main_only["n"], all_boards["n"])
        self.assertEqual(main_only["adv"], all_boards["adv"])

    def test_main_board_prefixes_still_used_for_limit_stats(self):
        """main_board_prefixes 仍服务于「主板涨停/跌停」，两个用途必须分开。"""
        src = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
        self.assertIn("main_board_prefixes", src, "主板涨跌停统计仍需该前缀")
        self.assertIn("strong_scope_boards", src, "强势股口径应独立可追溯")


class ReportContractTests(unittest.TestCase):
    """报告与状态条必须把范围结论显示出来（曾出现：函数写对了但渲染没读它）。"""

    def _result(self, boards, candidates, note=None):
        row = {"code": "300750", "name": "测试", "price": 10.0, "change": 3.0, "turnover": 5.0,
               "amount": 3e8, "volume_ratio": 2.0, "industry": "测试行业", "main_pct": 5.0,
               "flow_5m_inc": 1e6, "risk": "clean", "resonance": "是", "range_position": 50}
        result = {
            "meta": {"timestamp": "2026-10-01 10:00:00", "status": "盘中", "source": "test",
                     "enabled_boards": boards, "enabled_boards_label": "+".join(boards)},
            "breadth": {"adv": 1, "dec": 0, "flat": 0, "total_rows": 1, "valid_change": 1, "invalid_change": 0},
            "market_fetch_status": {"complete": True}, "indices": [], "errors": [], "warnings": [],
            "announcement_errors": [], "strict_enabled": True,
            "strict_ultra": [dict(row) for _ in range(candidates)],
            "trend_observation": [], "strict_trend": [], "trend_diagnostics": [],
            "dual_pool": [], "dual_pool_raw": [], "pre_intersection": [], "intersection_states": [],
            "intersection_config": {}, "intersection_config_meta": {},
            "capital_rank": [], "low_ultra": [], "low_trend": [], "watchlist": [],
            "sector_indices": [], "flow_detail": [], "low_open_wash": [],
            "board_scope_status": "ok",
            "board_scope_candidates": candidates,
            "board_scope_note": note if note is not None else screen.board_scope_note("ok", boards, candidates),
        }
        screen.stamp_board_fields(result)
        return result

    def test_scope_conclusion_is_rendered_in_report(self):
        """范围结论必须出现在报告正文，不能只存在于 JSON 字段里。"""
        md = screen.render_markdown(self._result(["chinext"], 0))
        self.assertIn("筛选范围：创业板", md)
        self.assertIn("没有符合现有门槛的候选", md)
        md_some = screen.render_markdown(self._result(["main", "star"], 7))
        self.assertIn("7 条候选", md_some)

    def test_degraded_report_never_claims_no_match(self):
        """数据不可用时必须说「结果不完整」，不得写成“没有符合条件的标的”。"""
        note = screen.board_scope_note("degraded", ["main", "chinext"], 0)
        md = screen.render_markdown(self._result(["main", "chinext"], 0, note=note))
        self.assertIn("本轮结果不完整", md)
        self.assertIn("不等于", md)

    def test_board_column_is_rendered_in_report(self):
        md = screen.render_markdown(self._result(["chinext"], 1))
        self.assertIn("| 交易板 |", md)
        self.assertIn("| 创业板 |", md)

    def test_stamping_happens_after_derived_pools(self):
        """标注必须发生在派生栏目（资金优选/交集状态机/观察池）生成之后。

        早于派生栏目调用会让这些表整列漏标「交易板」；原先的写法还把调用放在
        result 赋值之前，属于必然 NameError。核心与看板附加层各标注一次，各自
        都必须排在它负责生成/替换的派生栏目之后。
        """
        core = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
        engine = (SCRIPT_DIR / "realtime_engine.py").read_text(encoding="utf-8")
        for name, src, anchors in (
            ("core", core[: core.rindex("def main()")],
             ('result["capital_rank"] = capital_rank_full',
              'result["intersection_states"] = state_rows',
              'result["watchlist"] = watchlist_evaluated')),
            ("engine", engine, ('enrich_min5(result)',)),
        ):
            with self.subTest(entry=name):
                stamp_at = src.rindex("stamp_board_fields(result)")
                for anchor in anchors:
                    self.assertGreater(
                        stamp_at, src.index(anchor),
                        f"{name}: stamp_board_fields 必须排在 {anchor} 之后",
                    )

    def test_status_and_strip_distinguish_the_two_conclusions(self):
        dash = (SCRIPT_DIR / "realtime_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('"board_scope_status"', dash)
        self.assertIn('"board_scope_candidates"', dash)
        self.assertIn('"board_scope_note"', dash)
        common = (SCRIPT_DIR / "shared_static" / "common.js").read_text(encoding="utf-8")
        self.assertIn("范围内无符合条件的候选", common)
        self.assertIn("本轮范围内结果不可用", common)

    def test_legacy_flag_is_reachable_from_both_pages(self):
        """两页都要能拿到「旧口径快照」标记，且用同一个判定。

        先前只有 /api/config 暴露 snapshot_screen_method，而看板读的是 /api/status，
        于是工作台会提示、看板静默——同一个事实在两处结论不一致。
        """
        dash = (SCRIPT_DIR / "realtime_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("def _snapshot_screen_method", dash)
        self.assertGreaterEqual(dash.count("self._snapshot_screen_method()"), 2)
        self.assertIn('"snapshot_screen_method": self._snapshot_screen_method()', dash)
        app = (SCRIPT_DIR / "realtime_static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("status.snapshot_screen_method", app)

    def test_candidate_count_covers_every_pool(self):
        """候选数若只算严格三池，「仅低吸有产出」的轮次会被误报成范围内无候选。

        候选数在共享核心里按**完整**内部集合计算（`_all_rows`），不能被 top 截短。
        """
        src = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
        block = src[src.index("board_scope_candidates = len({"):]
        block = block[: block.index("})") + 2]
        for pool in ("strict_ultra_all_rows", "trend_observation_all_rows", "strict_trend_all_rows",
                     "low_ultra_all_rows", "low_trend_all_rows"):
            self.assertIn(pool, block, f"候选数应计入完整集合 {pool}")


class StatePruningTests(unittest.TestCase):
    """切换范围后旧候选不得沿用；已持仓保留监控。"""

    def test_prune_drops_deselected_boards(self):
        items = {"600886": {}, "300750": {}, "688981": {}}
        self.assertEqual(sorted(screen.prune_state_by_boards(items, ["main"])), ["600886"])
        self.assertEqual(sorted(screen.prune_state_by_boards(items, ["main", "chinext"])), ["300750", "600886"])

    def test_open_selection_prunes_everything(self):
        """空集在引擎内回退为「仅主板」，不得放宽成全部交易板。"""
        self.assertEqual(sorted(screen.prune_state_by_boards({"300750": {}}, [])), [])

    def test_holdings_are_kept_even_when_board_deselected(self):
        items = {"600886": {}, "688981": {}}
        self.assertEqual(sorted(screen.prune_state_by_boards(items, ["main"], ["688981"])),
                         ["600886", "688981"])

    def test_engine_and_cli_prune_state(self):
        """状态剔除只保留一份实现（核心），两个入口都必须走它。"""
        core = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
        engine = (SCRIPT_DIR / "realtime_engine.py").read_text(encoding="utf-8")
        self.assertIn("prune_state_by_boards", core)
        self.assertIn("load_holding_codes", core)
        self.assertIn("screen.run_screening_core", engine)


class ResultContractTests(unittest.TestCase):
    """结果口径：每行标交易板；范围结论区分「未符合条件」与「数据不可用」。"""

    def test_rows_are_stamped_with_board(self):
        result = {"strict_ultra": [{"code": "300750"}, {"code": "600886"}], "low_ultra": [{"code": "688981"}]}
        screen.stamp_board_fields(result)
        self.assertEqual(result["strict_ultra"][0]["board_label"], "创业板")
        self.assertEqual(result["strict_ultra"][1]["board_label"], "沪深主板")
        self.assertEqual(result["low_ultra"][0]["board_label"], "科创板")

    def test_scope_note_distinguishes_reasons(self):
        no_match = screen.board_scope_note("ok", ["chinext"], 0)
        self.assertIn("没有符合现有门槛的候选", no_match)
        unavailable = screen.board_scope_note("degraded", ["main", "chinext"], 0)
        self.assertIn("本轮结果不完整", unavailable)
        self.assertIn("不等于", unavailable)
        self.assertIn("7 条候选", screen.board_scope_note("ok", ["main"], 7))

    def test_extended_board_branch_is_gone_from_new_results(self):
        """新结果不再有独立观察分支；旧快照仅在带旧字段时按历史口径渲染。"""
        core = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
        self.assertNotIn("build_extended_board_observations", core)
        self.assertIn('if "extended_board_observations" in result', core, "旧快照需保留原口径渲染")
        self.assertIn('"board_scope_status"', core)
        engine = (SCRIPT_DIR / "realtime_engine.py").read_text(encoding="utf-8")
        self.assertNotIn('"extended_board_observations": extended_board_observations', engine)

    def test_snapshot_method_is_marked_for_pages(self):
        dash = (SCRIPT_DIR / "realtime_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("snapshot_screen_method", dash)
        self.assertIn("legacy_extended_observation", dash)

    def test_dashboard_shows_board_column_and_no_extended_tab(self):
        app = (SCRIPT_DIR / "realtime_static" / "app.js").read_text(encoding="utf-8")
        self.assertGreaterEqual(app.count("board_label"), 10, "正式栏目应展示交易板列")
        self.assertNotIn("tab-btn-ext-board", app)
        html = (SCRIPT_DIR / "realtime_static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("ext-board", html)

    def test_workbench_has_no_observe_only_wording(self):
        html = (SCRIPT_DIR / "workbench_static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("仅观察", html)
        self.assertIn("统一参与完整筛选", html)


if __name__ == "__main__":
    unittest.main()
