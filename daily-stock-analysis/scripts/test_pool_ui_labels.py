import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


class PoolUiLabelTests(unittest.TestCase):
    def test_gui_uses_unambiguous_dual_pool_label(self):
        source = (SCRIPT_DIR / "a_share_screen_gui.py").read_text(encoding="utf-8")

        self.assertIn("开始双池筛选", source)
        self.assertIn("趋势观察池", source)
        self.assertIn("趋势确认池", source)

    def test_legacy_streamlit_and_html_entrypoints_are_removed(self):
        self.assertFalse((SCRIPT_DIR / "streamlit_app.py").exists())
        self.assertFalse((SCRIPT_DIR.parent / "运行A股筛选Web.command").exists())
        source = (SCRIPT_DIR / "a_share_screen_gui.py").read_text(encoding="utf-8")
        self.assertNotIn("webbrowser", source)
        self.assertNotIn("open_html_preview", source)

    def test_realtime_dashboard_tab_aliases_cover_low_open_and_sector_group(self):
        app = (SCRIPT_DIR / "realtime_static" / "app.js").read_text(encoding="utf-8")
        html = (SCRIPT_DIR / "realtime_static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('"low-open": "low"', app)
        self.assertIn('sector: "sector"', app)
        self.assertIn('sectors: "sector"', app)
        self.assertIn('data-tab="sectors"', html)


class EastmoneyStockLinkTests(unittest.TestCase):
    """看板代码列 → 东财个股页：只对恰好六位的 60/68/00/30 建链接，其余原样显示。"""

    APP = (SCRIPT_DIR / "realtime_static" / "app.js").read_text(encoding="utf-8")
    CSS = (SCRIPT_DIR / "realtime_static" / "style.css").read_text(encoding="utf-8")

    def test_pure_url_function_validates_and_maps_by_market_prefix(self):
        app = self.APP
        self.assertIn("function eastmoneyStockUrl(code)", app)
        # 仅接受恰好六位数字，未知前缀不猜测
        self.assertIn('if (!/^\\d{6}$/.test(c)) return "";', app)
        self.assertIn("https://quote.eastmoney.com/sh${c}.html", app)
        self.assertIn("https://quote.eastmoney.com/kcb/${c}.html", app)
        self.assertIn("https://quote.eastmoney.com/sz${c}.html", app)

    def test_link_opens_in_new_tab_without_referrer_and_is_escaped(self):
        app = self.APP
        self.assertIn("function renderStockCode(code)", app)
        self.assertIn('target="_blank" rel="noopener noreferrer"', app)
        self.assertIn("const text = esc(code);", app)

    def test_code_columns_render_through_one_shared_helper(self):
        app = self.APP
        self.assertIn('else if (col.c === "code") rendered = renderStockCode(val);', app)
        # 资金追踪：持仓标记保留在链接之外
        self.assertIn(
            'k: "code", l: "代码", c: "code", r: (v, row) => renderStockCode(v) + (row._holding ?',
            app,
        )
        self.assertIn(".stock-link {", self.CSS)


if __name__ == "__main__":
    unittest.main()
