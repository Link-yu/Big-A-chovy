import unittest
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
for p in (str(PROJECT_ROOT), str(TOOLS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from detect_divergence_leader import (
    evaluate_history,
    evaluate_day_badges,
    day_files,
    D1_LOOKBACK,
)


def make_snap(i, price=40.0, pull=0.4, vwap_up=True, mainp=2.0, xl=+300.0,
              dom="✗", amt_wan=300000.0, reso=True, n_sec=2, ann="clean",
              mainp_override=None, flow_status=""):
    return {
        "time": f"{9 + i // 12:02d}:{(30 + i * 5) % 60:02d}",
        "price": price,
        "pull": pull,
        "vwap_up": vwap_up,
        "mainp": mainp_override if mainp_override is not None else mainp,
        "xl": xl,
        "dom": dom,
        "amt": amt_wan,
        "reso": reso,
        "n_sec": n_sec,
        "ann": ann,
        "flow_status": flow_status,
        "report_file": f"A股筛选结果_20260821_{i:04d}.md",
    }


def rising_snaps(mainp_seq, **kwargs):
    """按主力净占比升序构造一串合格快照，其余条件全部满足。"""
    return [make_snap(i, mainp=mp, **kwargs) for i, mp in enumerate(mainp_seq)]


class DivergenceEvaluateHistoryTests(unittest.TestCase):
    CODE, NAME, PLATE, DATE = "000426", "兴业银锡", "贵金属", "20260821"

    def test_xingye_style_scenario_b_triggers(self):
        """正例：主力占比创新高破5%+贴顶+共振，主导标签✗且比值不足 -> 场景B触发。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0, 6.8]
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq), self.DATE)
        self.assertIsNotNone(tg)
        self.assertEqual(tg["code"], self.CODE)
        self.assertIn("B比值不足", tg["scenario"])
        self.assertGreater(tg["mainp"], 5.0)

    def test_jushi_style_blocked_below_5pct(self):
        """反证锚：中国巨石式散户堆量（主力占比始终<5%）必须零触发。"""
        seq = [0.9, 1.6, 2.3, 3.0, 3.7, 4.4]
        tg = evaluate_history("600176", "中国巨石", "玻璃玻纤",
                              rising_snaps(seq), "20260807")
        self.assertIsNone(tg)

    def test_insufficient_history_blocked_then_allowed(self):
        """P1-4回归：快照数不足 LOOKBACK+1 时不得触发；达到后允许。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2]
        self.assertEqual(len(seq), D1_LOOKBACK)
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq), self.DATE)
        self.assertIsNone(tg)
        tg6 = evaluate_history(self.CODE, self.NAME, self.PLATE,
                               rising_snaps(seq + [6.0]), self.DATE)
        self.assertIsNotNone(tg6)

    def test_avoid_any_snapshot_is_stock_level_veto(self):
        """公告avoid当日任一快照出现即整股隔离，即使后续快照转clean。"""
        snaps = rising_snaps([2.0, 2.8, 3.6, 4.4, 5.2, 6.0])
        snaps[1]["ann"] = "avoid(资金占用)"
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE, snaps, self.DATE)
        self.assertIsNone(tg)

    def test_scenario_a_negative_superorder(self):
        """场景A：结构三条件全真但超大单为负 -> 否决旁路记录（分歧承接）。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0]
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq, xl=-8000.0), self.DATE)
        self.assertIsNotNone(tg)
        self.assertIn("A否决旁路", tg["scenario"])

    def test_formal_dominance_labels_never_enter_bypass_b(self):
        """语义修正回归：✓/✓(绝对)/✓(合力) 均为正式主导，只有显式 ✗ 进B场景。"""
        for label in ("✓", "✓(绝对)", "✓(合力)", ""):
            tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                                  rising_snaps([2.0, 2.8, 3.6, 4.4, 5.2, 6.0],
                                               dom=label),
                                  self.DATE)
            self.assertIsNone(tg, f"标签 {label!r} 不应进入旁路B")

    def test_first_trigger_only(self):
        """同股同日只记首个触发快照：返回的 trigger_time 是序列中最早合格点。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0, 6.5]
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq), self.DATE)
        above5 = next(i for i, mp in enumerate(seq) if mp > 5.0)
        expected_idx = max(above5, D1_LOOKBACK)
        self.assertEqual(tg["trigger_time"], make_snap(expected_idx)["time"])

    def test_resonance_required(self):
        """无板块共振或板内候选不足时不得触发。"""
        base = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0]
        self.assertIsNone(evaluate_history(
            self.CODE, self.NAME, self.PLATE,
            rising_snaps(base, reso=False), self.DATE))
        self.assertIsNone(evaluate_history(
            self.CODE, self.NAME, self.PLATE,
            rising_snaps(base, n_sec=1), self.DATE))

    def test_unknown_any_snapshot_is_stock_level_veto(self):
        """公告 unknown 与 avoid 一样：当日任一快照出现即整股隔离。"""
        snaps = rising_snaps([2.0, 2.8, 3.6, 4.4, 5.2, 6.0])
        snaps[2]["ann"] = "unknown(公告检查不可用)"
        self.assertIsNone(evaluate_history(
            self.CODE, self.NAME, self.PLATE, snaps, self.DATE))

    def test_distribution_veto_any_snapshot_isolates(self):
        """高位派发（低吸表资金状态“疑似派发”）同样不可豁免。"""
        snaps = rising_snaps([2.0, 2.8, 3.6, 4.4, 5.2, 6.0])
        snaps[1]["flow_status"] = "疑似派发；❌超大单为负·一票否决"
        self.assertIsNone(evaluate_history(
            self.CODE, self.NAME, self.PLATE, snaps, self.DATE))


class DivergenceDayFilesAndBadgesTests(unittest.TestCase):
    """只读判定：报告发现（根目录+日期目录）、去重与徽标状态。"""

    DATE = "20260930"
    HEADER = (
        "| 类 | 代码 | 名称 | 现价 | 涨幅 | 区间分位 | 换手率 | 成交额 | 量比 | 板块 | "
        "板块内候选 | 共振 | 高位回落 | 均价线 | 主力净占比 | 超大单 | 超单主导 | "
        "5分钟增量 | 资金状态 | 风险 | 公告风险 |"
    )
    SEP = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | " \
          "--- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.day_dir = self.root / self.DATE
        self.day_dir.mkdir(parents=True)
        patcher = mock.patch("detect_divergence_leader.REPORTS_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _row(self, code, name, mainp, ann="clean", flow_status="有效流入",
             dom="✗", xl="+300万", n_sec="2"):
        return (
            f"| B | {code} | {name} | 40.00 | 3.00% | 80% | 3.00% | 3.00亿 | 1.5 | "
            f"贵金属 | {n_sec} | 是 | 0.40pct | 均价线上方 | {mainp:.2f}% | {xl} | "
            f"{dom} | +10万 | {flow_status} | 无 | {ann} |"
        )

    def _write(self, stamp, row):
        content = (
            f"数据时间：2026-09-30 {stamp[:2]}:{stamp[2:]}:00，状态：盘中。\n\n"
            "## 低吸超短线 A/B/C\n\n" + self.HEADER + "\n" + self.SEP + "\n" + row + "\n"
        )
        (self.day_dir / f"A股筛选结果_{self.DATE}_{stamp}.md").write_text(
            content, encoding="utf-8")

    def test_day_files_merges_root_and_date_dir_and_dedups(self):
        """根目录（未归档）与日期目录都要发现，同名去重，按时间升序。"""
        self._write("1100", self._row("000426", "兴业银锡", 2.0))
        flat = self.root / f"A股筛选结果_{self.DATE}_1127.md"
        flat.write_text("占位", encoding="utf-8")
        dup = self.day_dir / f"A股筛选结果_{self.DATE}_1127.md"
        dup.write_text("占位", encoding="utf-8")

        files = day_files(self.DATE)
        names = [os.path.basename(f) for f in files]
        self.assertEqual(names, [
            f"A股筛选结果_{self.DATE}_1100.md",
            f"A股筛选结果_{self.DATE}_1127.md",
        ])

    def test_badges_triggered_not_triggered_and_undetermined(self):
        """三态：足够历史且触发=triggered；足够历史未触发=not_triggered；不足=undetermined。"""
        rising = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0]
        for i, mp in enumerate(rising):
            self._write(f"09{30 + i * 5:02d}", self._row("000426", "兴业银锡", mp))
        flat_mp = [6.0] * 6
        for i, mp in enumerate(flat_mp):
            self._write(f"10{00 + i * 5:02d}", self._row("600176", "中国巨石", mp))
        self._write("1050", self._row("002594", "比亚迪", 6.0))  # 仅 1 个快照

        badges = evaluate_day_badges(self.DATE)
        self.assertEqual(badges["000426"]["status"], "triggered")
        self.assertEqual(badges["000426"]["trigger_time"], "09:55")
        self.assertEqual(badges["600176"]["status"], "not_triggered")
        self.assertEqual(badges["002594"]["status"], "undetermined")
        self.assertNotIn("300750", badges)  # 不在低吸表 -> 调用方按未完成判定处理

    def test_badges_veto_blocks_trigger(self):
        """任一快照 unknown 或派发，即使结构合格也不得触发。"""
        rising = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0]
        for i, mp in enumerate(rising):
            ann = "unknown(公告检查不可用)" if i == 2 else "clean"
            self._write(f"09{30 + i * 5:02d}", self._row("000426", "兴业银锡", mp, ann=ann))
        badges = evaluate_day_badges(self.DATE)
        self.assertEqual(badges["000426"]["status"], "not_triggered")


if __name__ == "__main__":
    unittest.main()
