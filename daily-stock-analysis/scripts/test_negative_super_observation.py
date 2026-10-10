import unittest
from datetime import datetime

import dashboard_settings
from realtime_engine import build_negative_super_payload
from a_share_daily_screen import (
    Enriched,
    build_negative_super_observations,
    count_negative_super_observations,
    is_negative_super,
    render_markdown,
    shadow_badge_text,
)


def stock(code, *, super_net, main_net=20_000_000, big_net=10_000_000,
          main_pct=6.0, flow_status="有效流入", flow_veto="", risk_status="clean",
          vwap_state="均价线上方", industry="测试板块"):
    return Enriched(
        code=code, name=code, price=10.0, change=2.0, turnover=4.0,
        amount=500_000_000, volume_ratio=2.0, high=10.2, low=9.8,
        open=9.9, prev_close=9.7, total_mv=10_000_000_000,
        float_mv=8_000_000_000, industry=industry, timestamp=0, volume=1,
        kdate="2026-09-30", k_source="test", adj_close=10.0, ma5=9.8,
        ma10=9.6, ma20=9.4, prev_ma5=9.7, prev_ma10=9.5,
        prev_ma20=9.3, five_ret=0.05, dist60=0.1, ma20_dist=0.06,
        high_pull=0.8, cur_to_high=0.02, vol_vs_avg5=1.2, vwap=9.9,
        vwap_state=vwap_state, prior_high=9.9, prior_low=9.3,
        main_net=main_net, main_pct=main_pct, super_net=super_net,
        super_pct=2.0, big_net=big_net, big_pct=1.0, mid_net=-2_000_000,
        mid_pct=-0.4, small_net=-3_000_000, small_pct=-0.6,
        flow_5m_inc=1_000_000, flow_15m_inc=float("nan"),
        price_above_vwap=vwap_state == "均价线上方", flow_status=flow_status,
        flow_veto=flow_veto, risk_status=risk_status,
    )


TS = datetime(2026, 9, 30, 11, 0, 0)


class NegativeSuperObservationTests(unittest.TestCase):
    STATS = {"测试板块": {"strong": 3, "n": 3, "adv": 3, "sum": 3.0}}

    def test_only_negative_finite_super_rows_are_collected(self):
        neg = stock("000001", super_net=-5_000_000, flow_veto="超大单为负·一票否决")
        pos = stock("000002", super_net=+5_000_000)
        missing = stock("000003", super_net=float("nan"))
        rows = build_negative_super_observations([neg, pos, missing], self.STATS, {}, TS)
        self.assertEqual([r["code"] for r in rows], ["000001"])

    def test_missing_data_is_not_treated_as_negative_sample(self):
        """缺失/非数值资金数据不得当成负值样本。"""
        for bad in (float("nan"), None, float("inf"), float("-inf")):
            with self.subTest(value=bad):
                item = stock("000009", super_net=0)
                item.super_net = bad
                rows = build_negative_super_observations([item], self.STATS, {}, TS)
                self.assertEqual(rows, [])

    def test_blockers_report_both_gates_and_never_fake_absolute(self):
        item = stock("000001", super_net=-5_000_000, flow_veto="超大单为负·一票否决")
        row = build_negative_super_observations([item], self.STATS, {}, TS)[0]
        joined = "；".join(row["blockers"])
        self.assertIn("超大单为负", joined)
        self.assertIn("absolute", joined)
        # 生产主导标签不得被伪造成 absolute
        self.assertNotEqual(row["dominance_type"], "absolute")
        self.assertIsNone(row["shadow_badge"])

    def test_rows_sorted_by_super_net_ascending(self):
        a = stock("000001", super_net=-1_000_000, flow_veto="超大单为负·一票否决")
        b = stock("000002", super_net=-9_000_000, flow_veto="超大单为负·一票否决")
        rows = build_negative_super_observations([a, b], self.STATS, {}, TS)
        self.assertEqual([r["code"] for r in rows], ["000002", "000001"])
        self.assertEqual(rows[0]["data_time"], "2026-09-30 11:00:00")


class NegativeSuperCountTests(unittest.TestCase):
    STATS = {"测试板块": {"strong": 3, "n": 3, "adv": 3, "sum": 3.0}}

    def test_is_negative_super_rejects_missing_and_non_finite(self):
        for bad in (float("nan"), None, float("inf"), float("-inf"), 0.0, 1.0):
            with self.subTest(value=bad):
                item = stock("000009", super_net=0)
                item.super_net = bad
                self.assertFalse(is_negative_super(item))
        self.assertTrue(is_negative_super(stock("000001", super_net=-1.0)))

    def test_count_matches_built_rows(self):
        items = [
            stock("000001", super_net=-1_000_000, flow_veto="超大单为负·一票否决"),
            stock("000002", super_net=+1_000_000),
            stock("000003", super_net=float("nan")),
        ]
        self.assertEqual(count_negative_super_observations(items), 1)
        self.assertEqual(len(build_negative_super_observations(items, self.STATS, {}, TS)), 1)


class NegativeSuperPayloadGateTests(unittest.TestCase):
    """严格模式只报数量；开启观察才构造行，避免为隐藏列表追加公告查询。"""

    STATS = {"测试板块": {"strong": 3, "n": 3, "adv": 3, "sum": 3.0}}
    ITEMS = [
        stock("000001", super_net=-1_000_000, flow_veto="超大单为负·一票否决"),
        stock("000002", super_net=-2_000_000, flow_veto="超大单为负·一票否决"),
    ]

    def test_strict_mode_counts_without_building_rows(self):
        count, rows = build_negative_super_payload(
            self.ITEMS, self.STATS, {}, TS, "ok", dashboard_settings.VIEW_STRICT)
        self.assertEqual(count, 2)
        self.assertEqual(rows, [])

    def test_observe_mode_builds_rows(self):
        count, rows = build_negative_super_payload(
            self.ITEMS, self.STATS, {}, TS, "ok", dashboard_settings.VIEW_OBSERVE)
        self.assertEqual(count, 2)
        self.assertEqual(len(rows), 2)

    def test_degraded_or_incomplete_reports_nothing(self):
        for status in ("degraded", "incomplete"):
            with self.subTest(status=status):
                count, rows = build_negative_super_payload(
                    self.ITEMS, self.STATS, {}, TS, status, dashboard_settings.VIEW_OBSERVE)
                self.assertEqual((count, rows), (0, []))


class ShadowBadgeTextTests(unittest.TestCase):
    def test_badge_texts(self):
        self.assertEqual(shadow_badge_text(None), "未完成判定")
        self.assertEqual(
            shadow_badge_text({"status": "triggered", "trigger_time": "09:55"}),
            "今日已触发影子条件 · 触发时间 09:55",
        )
        self.assertEqual(shadow_badge_text({"status": "not_triggered"}), "未触发（判定完成）")
        self.assertEqual(shadow_badge_text({"status": "undetermined"}), "未完成判定")


class RenderGateTests(unittest.TestCase):
    def _result(self, view, status="ok", rows=None):
        return {
            "meta": {
                "timestamp": "2026-09-30 11:00:00",
                "status": "盘中",
                "elapsed_seconds": 1,
                "source": "测试源",
                "negative_super_view": view,
            },
            "breadth": {},
            "indices": [],
            "warnings": [],
            "negative_super_status": status,
            "negative_super_observations": rows or [],
        }

    def test_section_hidden_in_strict_view(self):
        md = render_markdown(self._result("strict"))
        self.assertNotIn("负超单观察", md)

    def test_section_rendered_in_observe_view(self):
        rows = build_negative_super_observations(
            [stock("000001", super_net=-5_000_000, flow_veto="超大单为负·一票否决")],
            NegativeSuperObservationTests.STATS, {}, TS)
        md = render_markdown(self._result("observe", rows=rows))
        self.assertIn("负超单观察", md)
        self.assertIn("000001", md)
        self.assertIn("未完成判定", md)

    def test_degraded_status_not_read_as_empty(self):
        md = render_markdown(self._result("observe", status="degraded", rows=[]))
        self.assertIn("不代表“今天没有负超单标的”", md)
        # 降级时不得渲染成裸“无”
        self.assertNotIn("\n无\n", md)


if __name__ == "__main__":
    unittest.main()
