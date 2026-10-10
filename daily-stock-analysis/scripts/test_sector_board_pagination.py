"""板块列表分页的完整性测试（不联网）。

回归点（同类工具调研发现）：
- 原实现固定最多 5 页、不读 total，板块数超过 500 会被**静默截断**；
- 某页在所有主机都失败时会直接 break，悄悄返回部分板块。

2026-09-28 追加（盘中 total=496 / 实收 491 且失败页为 0）：
- 翻页排序键必须固定，不能是实时涨跌幅 f3（同页 6 秒内只重合 92/100）；
- 去重键是板块代码 f12，不是板块名。
"""
import unittest
from unittest.mock import patch

import a_share_daily_screen as screen


def page_rows(count, start=0, name_of=None):
    """造一页原始行。f12 是去重键，必须唯一；name_of 可用来制造同名不同代码。"""
    rows = []
    for i in range(start, start + count):
        code = f"BK{i:04d}"
        rows.append({
            "f12": code,
            "f14": (name_of(i) if name_of else f"板块{i:03d}"),
            "f2": 1000 + i, "f3": 1.0, "f8": 1.0, "f104": i, "f105": 0,
        })
    return rows


def fake_pages(total, page_size=screen.SECTOR_BOARD_PAGE_SIZE, fail=(), name_of=None):
    """按 total 生成每页的 (rows, total)；fail 里的页号抛异常（模拟全部主机失败）。"""
    import math

    def fetch(page):
        if page in fail:
            raise RuntimeError("all hosts failed")
        start = (page - 1) * page_size
        count = max(0, min(page_size, total - start))
        return page_rows(count, start, name_of), total

    return fetch


class SectorBoardPaginationTests(unittest.TestCase):
    def setUp(self):
        screen.MARKET_WARNINGS.clear()
        screen.SECTOR_BOARDS_STATUS.update({
            "complete": None, "provider_total": None, "expected_pages": 0,
            "received_pages": 0, "failed_pages": [], "retrieved_rows": 0,
            "raw_rows": 0, "duplicate_rows": 0, "blank_code_rows": 0, "missing_rows": 0,
            "sort_field": screen.SECTOR_BOARD_SORT_FIELD,
        })

    def test_paging_uses_a_stable_sort_field(self):
        """分页排序键必须盘中不变：用 f3（实时涨跌幅）会导致页间漂移、板块漏取。"""
        params = screen._sector_board_params(2)
        self.assertEqual(params["fid"], "f12")
        self.assertNotEqual(params["fid"], "f3")
        self.assertEqual(params["pn"], 2)

    def test_fetches_all_pages_according_to_total(self):
        with patch.object(screen, "_fetch_sector_page", side_effect=fake_pages(496)):
            boards = screen.fetch_sector_indices()
        self.assertEqual(len(boards), 496)
        status = screen.SECTOR_BOARDS_STATUS
        self.assertTrue(status["complete"])
        self.assertEqual(status["expected_pages"], 5)
        self.assertEqual(status["received_pages"], 5)
        self.assertEqual(status["provider_total"], 496)
        self.assertEqual(status["missing_rows"], 0)
        self.assertEqual(screen.MARKET_WARNINGS, [])

    def test_more_than_five_pages_is_not_truncated(self):
        """板块数超过 500 时必须继续翻页——这是原实现静默截断的场景。"""
        with patch.object(screen, "_fetch_sector_page", side_effect=fake_pages(650)):
            boards = screen.fetch_sector_indices()
        self.assertEqual(len(boards), 650)
        self.assertEqual(screen.SECTOR_BOARDS_STATUS["expected_pages"], 7)
        self.assertTrue(screen.SECTOR_BOARDS_STATUS["complete"])

    def test_failed_page_is_retried_then_flagged(self):
        """失败页重试一次，仍失败 → 标记不完整并写警告，绝不静默返回部分板块。"""
        fetch = fake_pages(496, fail=(3,))
        with patch.object(screen, "_fetch_sector_page", side_effect=fetch):
            boards = screen.fetch_sector_indices()
        status = screen.SECTOR_BOARDS_STATUS
        self.assertFalse(status["complete"])
        self.assertIn(3, status["failed_pages"])
        self.assertTrue(any("板块列表不完整" in w for w in screen.MARKET_WARNINGS))
        self.assertLess(len(boards), 496)   # 确实少了，但**报出来了**

    def test_row_shortfall_marks_incomplete(self):
        """服务端 total 与实际行数不符 → 视为不完整。"""
        def fetch(page):     # total 说 300，但只给 150 行
            if page == 1:
                return page_rows(100), 300
            if page == 2:
                return page_rows(50, 100), 300
            return [], 300

        with patch.object(screen, "_fetch_sector_page", side_effect=fetch):
            boards = screen.fetch_sector_indices()
        self.assertEqual(len(boards), 150)
        self.assertFalse(screen.SECTOR_BOARDS_STATUS["complete"])
        self.assertTrue(any("板块列表不完整" in w for w in screen.MARKET_WARNINGS))

    def test_first_page_failure_is_reported(self):
        with patch.object(screen, "_fetch_sector_page", side_effect=RuntimeError("boom")):
            boards = screen.fetch_sector_indices()
        self.assertEqual(boards, [])
        self.assertFalse(screen.SECTOR_BOARDS_STATUS["complete"])
        self.assertTrue(any("板块指数查询失败" in w for w in screen.MARKET_WARNINGS))

    def test_duplicate_boards_are_deduped_by_code(self):
        """页间重复（漂移的后果）按代码去掉，计入 duplicate_rows，且不能算完整。"""
        def fetch(page):
            if page in (1, 2):
                return page_rows(100, 0), 200   # 两页返回同一批板块代码
            return [], 200

        with patch.object(screen, "_fetch_sector_page", side_effect=fetch):
            boards = screen.fetch_sector_indices()
        self.assertEqual(len(boards), 100)
        status = screen.SECTOR_BOARDS_STATUS
        self.assertEqual(status["raw_rows"], 200)
        self.assertEqual(status["duplicate_rows"], 100)
        self.assertEqual(status["missing_rows"], 100)
        self.assertFalse(status["complete"])

    def test_same_name_different_code_boards_are_both_kept(self):
        """同名不同代码（行业板块与概念板块重名）不能互相顶掉——按代码去重。"""
        def fetch(page):
            if page == 1:
                return [{"f12": "BK0001", "f14": "综合", "f3": 1.0},
                        {"f12": "BK0002", "f14": "综合", "f3": -2.0}], 2
            return [], 2

        with patch.object(screen, "_fetch_sector_page", side_effect=fetch):
            boards = screen.fetch_sector_indices()
        self.assertEqual(len(boards), 2)
        self.assertEqual({b["code"] for b in boards}, {"BK0001", "BK0002"})
        self.assertEqual({b["change"] for b in boards}, {1.0, -2.0})

    def test_rows_shifted_between_pages_are_reported_with_reason(self):
        """页间漂移的现场：某板块在两页各出现一次，另一个整轮缺席。

        这是 2026-09-28 盘中「total=496 / 实收 491 / 失败页 无」的形态——
        必须报不完整，且要在警告里说清是重复行造成的。
        """
        def fetch(page):
            if page == 1:
                return page_rows(100, 0), 300
            if page == 2:
                # 第 2 页从 90 开始：90-99 与第 1 页重复，200-299 无人提供
                return page_rows(80, 90) + page_rows(20, 200), 300
            return [], 300

        with patch.object(screen, "_fetch_sector_page", side_effect=fetch):
            boards = screen.fetch_sector_indices()
        status = screen.SECTOR_BOARDS_STATUS
        self.assertEqual(len(boards), 190)          # 200 行原始 → 190 个唯一板块
        self.assertEqual(status["raw_rows"], 200)
        self.assertEqual(status["duplicate_rows"], 10)
        self.assertFalse(status["complete"])
        warning = next(w for w in screen.MARKET_WARNINGS if "板块列表不完整" in w)
        self.assertIn("重复行", warning)
        self.assertIn("原始 200 行", warning)

    def test_rows_without_code_are_counted_separately(self):
        def fetch(page):
            if page == 1:
                return [{"f12": "", "f14": "无代码板块", "f3": 0.5},
                        {"f12": "BK0009", "f14": "正常板块", "f3": 0.1}], 1
            return [], 1

        with patch.object(screen, "_fetch_sector_page", side_effect=fetch):
            boards = screen.fetch_sector_indices()
        self.assertEqual(len(boards), 1)
        self.assertEqual(screen.SECTOR_BOARDS_STATUS["blank_code_rows"], 1)


if __name__ == "__main__":
    unittest.main()
