"""Offline regressions for Tencent's optional upper/lower limit quote fields."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import query_quote  # noqa: E402


def quote_response(symbol: str, upper: str = "11.00", lower: str = "9.00", field_count: int = 52) -> str:
    """Build the Tencent tilde-delimited wire shape (0-based fields 47/48)."""
    fields = ["0"] * 52
    fields[1:6] = ["测试股票", symbol[2:], "10.20", "10.00", "10.10"]
    fields[30] = "20261005100000"
    fields[47:49] = [upper, lower]
    return f'v_{symbol}="' + "~".join(fields[:field_count]) + '";'


class QuotePriceLimitTests(unittest.TestCase):
    def fetch(self, content: str, codes: list[str]):
        with patch.object(
            query_quote.urllib.request,
            "urlopen",
            return_value=io.BytesIO(content.encode("gbk")),
        ) as opener:
            quotes = query_quote.fetch_realtime_quotes(codes)
        return quotes, opener

    def test_sh_and_sz_batch_keep_upper_and_lower_limits_in_correct_order(self) -> None:
        content = quote_response("sh600000") + "\n" + quote_response("sz000001", "22.00", "18.00")
        quotes, opener = self.fetch(content, ["600000", "000001"])

        self.assertEqual(set(quotes), {"600000", "000001"})
        self.assertEqual((quotes["600000"]["zt"], quotes["600000"]["dt"]), (11.0, 9.0))
        self.assertEqual((quotes["000001"]["zt"], quotes["000001"]["dt"]), (22.0, 18.0))
        request = opener.call_args.args[0]
        self.assertIn("q=sh600000,sz000001", request.full_url)

    def test_limit_fields_are_missing_independently_without_swapping(self) -> None:
        cases = (
            ("", "9.00", (0.0, 9.0)),
            ("11.00", "", (11.0, 0.0)),
            ("", "", (0.0, 0.0)),
            ("invalid", "9.00", (0.0, 9.0)),
        )
        for upper, lower, expected in cases:
            with self.subTest(upper=upper, lower=lower):
                quotes, _ = self.fetch(quote_response("sh600000", upper, lower), ["sh600000"])
                self.assertEqual((quotes["600000"]["zt"], quotes["600000"]["dt"]), expected)

    def test_short_response_preserves_available_upper_field_only(self) -> None:
        # A 48-field response has field 47 but omits field 48.
        quotes, _ = self.fetch(quote_response("sh600000", "11.00", "9.00", field_count=48), ["600000"])
        self.assertEqual((quotes["600000"]["zt"], quotes["600000"]["dt"]), (11.0, 0.0))

        quotes, _ = self.fetch(quote_response("sh600000", field_count=40), ["600000"])
        self.assertEqual((quotes["600000"]["zt"], quotes["600000"]["dt"]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
