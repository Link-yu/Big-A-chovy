import unittest

from a_share_daily_screen import normalize_row


class FlowMissingValueTests(unittest.TestCase):
    def test_eastmoney_flow_sentinels_are_numeric_zero(self):
        row = {
            "f12": "600000",
            "f14": "测试股",
            "f62": "--",
            "f184": "-",
            "f66": None,
            "f69": "",
            "f72": "not-a-number",
            "f75": "--",
            "f78": "--",
            "f81": "-",
            "f84": "--",
            "f87": "--",
        }

        normalized = normalize_row(row)

        flow_fields = (
            "main_net", "main_pct", "super_net", "super_pct", "big_net", "big_pct",
            "mid_net", "mid_pct", "small_net", "small_pct",
        )
        for field in flow_fields:
            with self.subTest(field=field):
                self.assertIsInstance(normalized[field], (int, float))
                self.assertEqual(normalized[field], 0)

    def test_numeric_string_flow_is_preserved_as_number(self):
        normalized = normalize_row({"f62": "123.5", "f184": "4.2"})

        self.assertEqual(normalized["main_net"], 123.5)
        self.assertEqual(normalized["main_pct"], 4.2)


if __name__ == "__main__":
    unittest.main()
