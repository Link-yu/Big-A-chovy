"""日 K 取数链与降级标注的回归测试（不联网）。

背景（2026-09-28 实测）：
- 腾讯整链连续失败约 47 分钟，期间整轮落到新浪：不复权、且末根停在 09-24（缺当日 bar），
  与前复权基准差 1.4%~2.7%、5 日涨幅差最多 2 倍，池子被压掉约一半（超短 4 → 7~8）。
- 东财 push2his 同日复测已恢复且为前复权（与腾讯同日收盘差 ≤0.2%），应作为并列第二档。
- 旧的降级警告每进程只出一次，15 轮降级里只有 1 份报告带警告，频率被低估。

本文件锁住：档位顺序、根数门槛、口径基准与逐轮标注。
"""

import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import a_share_daily_screen as screen


TODAY = "2026-09-28"
PREV = "2026-09-24"          # 中秋休市前的最后一个交易日
QFQ_CLOSE = 15.27            # 前复权基准收盘（腾讯 09-28 实测）
RAW_CLOSE = 14.86            # 新浪不复权同日实测收盘


def tencent_payload(symbol="sh600886", bars=90, last_date=TODAY, last_close=QFQ_CLOSE):
    rows = []
    base = datetime(2026, 5, 20)
    for i in range(bars - 1):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        rows.append([d, "10.0", "10.2", "10.5", "9.9", "1000"])
    rows.append([last_date, "15.0", str(last_close), "15.5", "14.9", "2000"])
    return {"data": {symbol: {"qfqday": rows}}}


def em_payload(bars=90, last_date=TODAY, last_close=QFQ_CLOSE):
    rows = []
    for i in range(bars - 1):
        rows.append(f"2026-05-{20 + (i % 10):02d},10.0,10.2,10.5,9.9,1000,100,1,1,0.1,1")
    rows.append(f"{last_date},15.0,{last_close},15.5,14.9,2000,100,1,1,0.1,1")
    return {"data": {"klines": rows}}


def sina_payload(bars=90, last_date=PREV, last_close=RAW_CLOSE):
    out = []
    for i in range(bars - 1):
        out.append({"day": f"2026-05-{20 + (i % 10):02d}", "open": "10.0", "high": "10.5",
                    "low": "9.9", "close": "10.2", "volume": "1000"})
    out.append({"day": last_date, "open": "15.0", "high": "15.5", "low": "14.9",
                "close": str(last_close), "volume": "2000"})
    return out


class KlineSourceChainTests(unittest.TestCase):
    def setUp(self):
        screen.MARKET_WARNINGS.clear()
        screen._tencent_kline_fail_streak = 0
        screen._tencent_kline_blocked_until = 0.0
        screen._kline_reference.clear()
        screen._kline_fallback_codes.clear()
        screen._kline_mismatch_codes.clear()
        # 生产侧有个全局副作用：realtime_engine 被导入时会把 screen.fetch_kline 换成
        # 带缓存的包装函数。整套 discover 跑时（别的用例会导入引擎）缓存一命中，本文件的
        # 用例就走不到取数链，六个用例会一起误判成“落到东财/新浪”——单跑因为不导入引擎，
        # 反而看不出问题。这里锁死无缓存的原始链：本文件测的是档位，不是缓存。
        engine = sys.modules.get("realtime_engine")
        uncached = getattr(engine, "_original_fetch_kline", screen.fetch_kline)
        self._uncached_patch = patch.object(screen, "fetch_kline", uncached)
        self._uncached_patch.start()
        self.addCleanup(self._uncached_patch.stop)

    def _fetch_with(self, tencent=None, em=None, sina=None, captured=None):
        """按主机归属分发应答；None 表示该主机不可用（抛异常）。"""
        def fake_json(url, params=None, **kwargs):
            if captured is not None:
                captured.append((url, params or {}))
            if "gtimg" in url or "qq.com" in url:
                if tencent is None:
                    raise RuntimeError("tencent down")
                return tencent
            if "push2his" in url:
                if em is None:
                    raise RuntimeError("eastmoney down")
                return em
            if "sina" in url:
                if sina is None:
                    raise RuntimeError("sina down")
                return sina
            raise RuntimeError(f"unexpected url {url}")
        return patch.object(screen, "fetch_json", side_effect=fake_json), \
            patch.object(screen, "_rank_urls", side_effect=lambda urls: list(urls))

    def test_tencent_is_primary(self):
        p1, p2 = self._fetch_with(tencent=tencent_payload())
        with p1, p2:
            bars, source = screen.fetch_kline("600886")
        self.assertEqual(source, "tencent_qfq")
        self.assertEqual(len(bars), 90)
        self.assertEqual(screen._kline_fallback_codes, set())
        self.assertEqual(screen._kline_reference["600886"], (TODAY, QFQ_CLOSE))

    def test_eastmoney_covers_tencent_outage(self):
        """腾讯整链挂掉时，东财应顶上，而不是直接落到新浪（2026-09-28 的现场）。"""
        p1, p2 = self._fetch_with(tencent=None, em=em_payload(), sina=sina_payload())
        with p1, p2:
            bars, source = screen.fetch_kline("600886")
        self.assertEqual(source, "eastmoney_qfq")
        self.assertEqual(len(bars), 90)
        self.assertEqual(screen._kline_fallback_codes, set(), "走东财不算降级")
        screen.kline_source_summary([])
        self.assertFalse(any("日K降级" in w for w in screen.MARKET_WARNINGS))

    def test_eastmoney_request_uses_qfq_daily_params(self):
        captured = []
        p1, p2 = self._fetch_with(em=em_payload(), captured=captured)
        with p1, p2:
            screen.fetch_kline("600886")
            screen.fetch_kline("000592")
        em_calls = [params for url, params in captured if "push2his" in url]
        self.assertTrue(em_calls)
        self.assertTrue(all(p.get("fqt") == 1 for p in em_calls), "必须请求前复权")
        self.assertTrue(all(p.get("klt") == 101 for p in em_calls), "必须是日线")
        secids = [p.get("secid") for p in em_calls]
        self.assertIn("1.600886", secids)
        self.assertIn("0.000592", secids)

    def test_eastmoney_short_sample_falls_through_to_sina(self):
        p1, p2 = self._fetch_with(tencent=None, em=em_payload(bars=10), sina=sina_payload())
        with p1, p2:
            bars, source = screen.fetch_kline("600886")
        self.assertEqual(source, "sina_daily")
        self.assertEqual(len(bars), 90)

    def test_sina_fallback_is_flagged_every_round_not_once(self):
        """降级标注必须逐轮出现（旧实现每进程只报一次，15 轮里只 1 份报告带警告）。"""
        p1, p2 = self._fetch_with(tencent=None, em=None, sina=sina_payload())
        for _ in range(3):
            screen.MARKET_WARNINGS.clear()
            screen._kline_fallback_codes.clear()
            with p1, p2:
                _bars, source = screen.fetch_kline("600886")
            self.assertEqual(source, "sina_daily")
            screen.kline_source_summary([])
            self.assertTrue(
                any("日K降级" in w for w in screen.MARKET_WARNINGS),
                "每一轮都应有降级标注",
            )

    def test_sina_mismatch_against_qfq_reference_is_counted(self):
        screen._kline_reference["600886"] = (TODAY, QFQ_CLOSE)
        p1, p2 = self._fetch_with(tencent=None, em=None, sina=sina_payload())
        with p1, p2:
            screen.fetch_kline("600886")
        self.assertIn("600886", screen._kline_mismatch_codes)
        screen.kline_source_summary([])
        warning = next(w for w in screen.MARKET_WARNINGS if "日K降级" in w)
        self.assertIn("不一致", warning)
        self.assertIn("1 只", warning)

    def test_reference_match_is_not_counted_as_mismatch(self):
        screen._kline_reference["600886"] = (TODAY, RAW_CLOSE)
        p1, p2 = self._fetch_with(tencent=None, em=None,
                                  sina=sina_payload(last_date=TODAY, last_close=RAW_CLOSE))
        with p1, p2:
            screen.fetch_kline("600886")
        self.assertIn("600886", screen._kline_fallback_codes)
        self.assertEqual(screen._kline_mismatch_codes, set())

    def test_summary_clears_round_state(self):
        p1, p2 = self._fetch_with(tencent=None, em=None, sina=sina_payload())
        with p1, p2:
            screen.fetch_kline("600886")
        screen.kline_source_summary([])
        self.assertEqual(screen._kline_fallback_codes, set())
        self.assertEqual(screen._kline_mismatch_codes, set())
        screen.MARKET_WARNINGS.clear()
        screen.kline_source_summary([])
        self.assertEqual(screen.MARKET_WARNINGS, [])

    def test_all_sources_down_raises(self):
        p1, p2 = self._fetch_with(tencent=None, em=None, sina=None)
        with p1, p2:
            with self.assertRaises(RuntimeError):
                screen.fetch_kline("600886")

    def test_em_and_tencent_share_the_k_row_shape(self):
        """东财 klines 与腾讯 qfqday 前 6 列同序，故共用 parse_k_rows。"""
        rows = screen.parse_k_rows(["2026-09-28,15.0,15.27,15.5,14.9,2000,100,1,1,0.1,1"])
        self.assertEqual(rows[0]["date"], "2026-09-28")
        self.assertEqual(rows[0]["open"], 15.0)
        self.assertEqual(rows[0]["close"], 15.27)
        self.assertEqual(rows[0]["high"], 15.5)
        self.assertEqual(rows[0]["low"], 14.9)


if __name__ == "__main__":
    unittest.main()
