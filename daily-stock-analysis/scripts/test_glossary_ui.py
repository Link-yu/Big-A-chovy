"""术语说明（词典 + 侧栏 + ⓘ）结构回归测试（不联网、不起服务）。

锁定三件事：
  1) 词典是唯一来源，按「栏目 + 术语」建键，每条含三段（是什么/现在应看什么/权限边界）；
  2) 看板入口（顶部按钮、表格 ⓘ）与侧栏读同一份数据；未收录标签不给近似解释；
  3) 侧栏与权限提示位于 10 秒重绘的表格容器之外，且“准交集”旧口径已统一修正。
"""

import re
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATIC = SCRIPT_DIR / "realtime_static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")
SCREEN_PY = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
ENGINE_PY = (SCRIPT_DIR / "realtime_engine.py").read_text(encoding="utf-8")

ENTRY_RE = re.compile(r'group: "([^"]+)", scope: "([^"]+)", term: "([^"]+)"')

# 首批文案：栏目 + 术语（与方案表格一一对应）
EXPECTED_TERMS = {
    ("明日观察", "趋势低吸"), ("明日观察", "突破前观察"),
    ("明日观察", "触发价"), ("明日观察", "低吸区"),
    ("交集状态", "准交集"), ("交集状态", "首次交集"), ("交集状态", "等待回踩"),
    ("交集状态", "回踩确认"), ("交集状态", "可新开仓"), ("交集状态", "迟到交集"),
    ("突破状态", "WATCHING"), ("突破状态", "TRIGGERED"), ("突破状态", "CONFIRMED"),
    ("资金状态", "有效流入"), ("资金状态", "疑似流入"), ("资金状态", "价量背离"),
    ("资金状态", "疑似派发"), ("资金状态", "数据不足"),
    ("公告", "watch_risk"), ("公告", "avoid / unknown"),
}


class GlossaryDataTests(unittest.TestCase):
    def setUp(self):
        self.entries = ENTRY_RE.findall(APP_JS)

    def test_first_batch_terms_are_all_present(self):
        self.assertEqual({(s, t) for _g, s, t in self.entries}, EXPECTED_TERMS)

    def test_every_entry_has_three_segments_and_a_reference(self):
        count = len(self.entries)
        self.assertGreater(count, 0)
        for field in ("what", "look", "limit", "ref"):
            with self.subTest(field=field):
                self.assertEqual(len(re.findall(rf'^\s+{field}: "', APP_JS, re.M)), count)

    def test_index_is_keyed_by_scope_so_same_name_never_crosses(self):
        """同名术语靠栏目区分：索引必须按 scope 分层，不能拍平成一张表。"""
        self.assertIn("GLOSSARY_INDEX[entry.scope] = GLOSSARY_INDEX[entry.scope] || {}", APP_JS)
        self.assertIn("const byScope = GLOSSARY_INDEX[scope];", APP_JS)

    def test_unknown_labels_get_no_approximate_explanation(self):
        self.assertIn('if (!entry) return "";', APP_JS)

    def test_sidebar_reads_the_same_dictionary(self):
        self.assertIn("GLOSSARY_GROUPS.map(", APP_JS)
        self.assertIn("GLOSSARY_ENTRIES.filter((e) => e.group === g)", APP_JS)


class GlossaryEntryPointTests(unittest.TestCase):
    def test_column_scope_map_covers_the_required_columns(self):
        for key in ("structure", "intersection_phase", "flow_status", "announcement_risk"):
            with self.subTest(column=key):
                self.assertRegex(APP_JS, rf'\b{key}: "')

    def test_breakout_states_are_sidebar_only_not_fabricated(self):
        """看板链路（realtime_engine）不产出 breakout_phase：硬加一列会把缺失值画成
        WATCHING，等于伪造状态，所以它只作为侧栏词条存在。"""
        self.assertNotIn('{ k: "breakout_phase"', APP_JS)
        self.assertNotIn("function breakoutBadge", APP_JS)
        self.assertNotIn("breakout_phase:", APP_JS)
        self.assertIn('scope: "突破状态"', APP_JS)

    def test_info_button_uses_delegation_not_per_cell_binding(self):
        """表格每 10 秒重绘：必须用事件委托，逐元素绑定会失效。"""
        self.assertIn("data-glossary-id=", APP_JS)
        self.assertIn('closest("[data-glossary-id]")', APP_JS)
        self.assertNotIn('querySelectorAll(".info-btn")', APP_JS)

    def test_app_js_defines_its_own_esc_helper(self):
        """realtime app.js 不自带 esc（只在共用层导出）；解释层用到它，缺失会直接抛错。"""
        self.assertIn("function esc(value)", APP_JS)

    def test_key_column_headers_carry_info_buttons(self):
        self.assertIn('gh: ["明日观察", "触发价"]', APP_JS)
        self.assertIn('gh: ["明日观察", "低吸区"]', APP_JS)


class GlossaryShellTests(unittest.TestCase):
    def test_sidebar_and_hint_live_outside_the_redrawn_table_container(self):
        """两者都必须与 #table-container 平级（不能被 innerHTML 重绘带走）。"""
        idx = INDEX_HTML
        hint_at = idx.index('id="permission-hint"')
        table_at = idx.index('id="table-container"')
        self.assertLess(hint_at, table_at)
        # 提示条在表格容器开始前就已闭合 => 是兄弟节点，不是被重绘的子节点
        self.assertIn("</div>", idx[hint_at:table_at])
        # 侧栏在 </main> 之后 => 完全脱离重绘区
        self.assertLess(idx.index("</main>"), idx.index('id="glossary"'))

    def test_sidebar_has_top_entry_fixed_note_and_accessible_controls(self):
        self.assertIn('id="glossary-btn"', INDEX_HTML)
        self.assertIn('id="glossary-close"', INDEX_HTML)
        self.assertIn('aria-haspopup="dialog"', INDEX_HTML)
        self.assertIn(
            "筛选标签用于发现和解释证据；真实仓建议仍须按《选股框架.md》完成全部核验。",
            INDEX_HTML,
        )

    def test_keyboard_and_mobile_support(self):
        self.assertIn('e.key === "Escape"', APP_JS)
        self.assertIn(".glossary-panel { width: 100%;", STYLE_CSS)
        self.assertIn(".glossary.hidden { display: none; }", STYLE_CSS)

    def test_permission_hint_covers_qualified_and_experimental_states(self):
        self.assertIn("EXPERIMENTAL_BREAKOUT_PHASES", APP_JS)
        self.assertIn("不等于全部真实仓门禁已通过", APP_JS)
        self.assertIn("观察池突破仍属实验状态", APP_JS)


class BreakoutScopeNoteTests(unittest.TestCase):
    """突破状态只由 CLI 路径计算；看板链路须如实标注适用范围，不得伪造状态。"""

    def test_scope_note_constant_exists_with_exact_wording(self):
        self.assertIn(
            'const BREAKOUT_SCOPE_NOTE = "实时看板当前未计算这些状态；仅用于理解已运行状态机的报告。";',
            APP_JS,
        )

    def test_every_breakout_entry_carries_the_scope_note(self):
        breakout_terms = re.findall(
            r'group: "突破状态".*?note: BREAKOUT_SCOPE_NOTE,', APP_JS, re.S
        )
        self.assertEqual(len(breakout_terms), 3)

    def test_scope_note_is_actually_rendered_in_the_sidebar(self):
        self.assertIn("e.note ?", APP_JS)
        self.assertIn('<p class="g-note">', APP_JS)
        self.assertIn(".g-note {", STYLE_CSS)


class ReportMissingValueTests(unittest.TestCase):
    """未评估的看板结果不得伪造 WATCHING / 0次 / ✗，标题也不得无条件写状态机。"""

    def test_breakout_phase_defaults_to_not_evaluated(self):
        self.assertIn('r.get("breakout_phase") or "未评估"', SCREEN_PY)
        self.assertNotIn('r.get("breakout_phase", "WATCHING")', SCREEN_PY)

    def test_uncomputed_count_and_label_render_as_dash(self):
        self.assertIn('if r.get("confirm_count") is not None else "—"', SCREEN_PY)
        self.assertIn('r.get("dominance_label") or "—"', SCREEN_PY)

    def test_title_mentions_the_state_machine_only_when_evaluated(self):
        self.assertIn("breakout_evaluated = any(r.get(\"breakout_phase\")", SCREEN_PY)
        self.assertIn(
            '"## 明日观察池（含突破升级状态机）" if breakout_evaluated else "## 明日观察池"',
            SCREEN_PY,
        )

    def test_board_has_no_breakout_column_that_would_fabricate_state(self):
        self.assertNotIn('{ k: "breakout_phase"', APP_JS)


class PreIntersectionWordingTests(unittest.TestCase):
    """旧口径“四道门槛（含板块共振）”必须已统一修正为“共振仅参考”。"""

    def test_no_stale_four_gate_wording_remains(self):
        for name, src in (("app.js", APP_JS), ("a_share_daily_screen.py", SCREEN_PY),
                          ("realtime_engine.py", ENGINE_PY)):
            with self.subTest(file=name):
                self.assertNotIn("四道门槛", src)

    def test_new_wording_is_present_in_board_and_report(self):
        for name, src in (("app.js", APP_JS), ("a_share_daily_screen.py", SCREEN_PY)):
            with self.subTest(file=name):
                self.assertIn("用于参考，不单独否决", src)


if __name__ == "__main__":
    unittest.main()
