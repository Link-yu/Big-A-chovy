import unittest

from a_share_daily_screen import SCREENING_CONFIG, sector_stats


def _row(code, change, amount, industry="银行"):
    return {
        "f12": code,
        "f14": f"测试{code[-2:]}",
        "f3": change,
        "f6": amount,
        "f100": industry,
    }


class SectorStatsSentinelTests(unittest.TestCase):
    def setUp(self):
        cfg = SCREENING_CONFIG["resonance"]
        self.change_min = float(cfg["strong_change_min_inclusive"])
        self.amount_min = float(cfg["strong_amount_min_inclusive"])

    def test_f6_sentinel_dash_does_not_crash(self):
        # Eastmoney returns "-" for suspended stocks' amount (f6); the row must
        # be skipped for strong counting instead of raising TypeError.
        strong_change = self.change_min + 0.1
        rows = [
            _row("600000", strong_change, "-"),
            _row("600001", strong_change, "--"),
            _row("600002", strong_change, None),
        ]
        stats = sector_stats(rows)
        self.assertEqual(stats["银行"]["n"], 3)
        self.assertEqual(stats["银行"]["strong"], 0)

    def test_f6_numeric_amount_still_counts_strong(self):
        strong = [
            _row("600000", self.change_min + 0.1, self.amount_min + 1.0),
            _row("600001", self.change_min, self.amount_min),
        ]
        weak = [_row("600002", self.change_min + 0.1, self.amount_min - 1.0)]
        stats = sector_stats(strong + weak)
        self.assertEqual(stats["银行"]["strong"], 2)
        self.assertEqual(stats["银行"]["n"], 3)


if __name__ == "__main__":
    unittest.main()
