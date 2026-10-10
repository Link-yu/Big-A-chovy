"""共用层（导航 + 运行状态条）与术语统一的结构性回归测试（不联网、不起服务）。

背景：两个页面（实时看板 / 筛选工作台）此前各自维护导航与运行状态，
看板还把「数据时间/数据源/下次刷新/行情不完整/K线缓存」分散在五处展示。
本文件锁住「两页共用同一实现、同一措辞、同一槽位」这件事，避免回退。
"""

import re
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REALTIME_HTML = (SCRIPT_DIR / "realtime_static" / "index.html").read_text(encoding="utf-8")
WORKBENCH_HTML = (SCRIPT_DIR / "workbench_static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_PY = (SCRIPT_DIR / "realtime_dashboard.py").read_text(encoding="utf-8")
WORKBENCH_PY = (SCRIPT_DIR / "web_workbench.py").read_text(encoding="utf-8")
ENGINE_PY = (SCRIPT_DIR / "realtime_engine.py").read_text(encoding="utf-8")
REALTIME_JS = (SCRIPT_DIR / "realtime_static" / "app.js").read_text(encoding="utf-8")
WORKBENCH_JS = (SCRIPT_DIR / "workbench_static" / "app.js").read_text(encoding="utf-8")
SHARED_JS = (SCRIPT_DIR / "shared_static" / "common.js").read_text(encoding="utf-8")


class SharedLayerWiringTests(unittest.TestCase):
    def test_shared_static_files_exist(self):
        self.assertTrue((SCRIPT_DIR / "shared_static" / "common.css").is_file())
        self.assertTrue((SCRIPT_DIR / "shared_static" / "common.js").is_file())

    def test_dashboard_handler_serves_shared_assets(self):
        self.assertIn('path == "/common.css"', DASHBOARD_PY)
        self.assertIn('path == "/common.js"', DASHBOARD_PY)
        self.assertIn("_serve_shared_static", DASHBOARD_PY)

    def test_status_exposes_strip_fields(self):
        self.assertIn('"data_timestamp"', DASHBOARD_PY)
        self.assertIn('"data_source"', DASHBOARD_PY)

    def test_both_pages_mount_the_shared_layer(self):
        for name, html in (("realtime", REALTIME_HTML), ("workbench", WORKBENCH_HTML)):
            with self.subTest(page=name):
                self.assertIn('href="/common.css"', html)
                self.assertIn('src="/common.js"', html)
                self.assertIn('class="view-switch"', html)
                self.assertIn('id="shared-status"', html)
                self.assertIn('id="action-notice"', html)

    def test_view_switch_shows_current_page_on_each_side(self):
        self.assertIn('aria-current="page">实时看板', REALTIME_HTML)
        self.assertIn('aria-current="page">筛选工作台', WORKBENCH_HTML)

    def test_no_external_tab_jump_between_the_two_views(self):
        """两个视图是同页切换，不该新开标签页。"""
        self.assertNotIn('target="_blank" rel="noopener">实时看板', WORKBENCH_HTML)

    def test_dashboard_does_not_duplicate_strip_facts(self):
        """数据时间/数据源已并入状态条；看板不应再各留一份（同一事实只出现一次）。"""
        for stale in ("mp-timestamp", "mp-source", "next-refresh", "fetch-warn", "cache-info"):
            with self.subTest(element=stale):
                self.assertNotIn(stale, REALTIME_HTML)
                self.assertNotIn(stale, REALTIME_JS)

    def test_both_pages_render_the_shared_strip(self):
        self.assertIn("SharedUI.render", REALTIME_JS)
        self.assertIn("SharedUI.render", WORKBENCH_JS)


class TerminologyTests(unittest.TestCase):
    def test_announcement_gate_cannot_be_disabled_from_any_entrypoint(self):
        """「公告检查」是一票否决门禁：两个页面都不再提供关闭开关，服务端强制开启。"""
        self.assertIn("公告检查", WORKBENCH_HTML)
        self.assertIn("强制开启", WORKBENCH_HTML)
        self.assertNotIn('id="check-announcements"', WORKBENCH_HTML)
        self.assertNotIn('id="skip-ann"', REALTIME_HTML)
        # 状态条防呆读的是"快照实际执行口径"，不是当前设置
        self.assertIn("announcement_check_skipped", SHARED_JS)
        self.assertIn("本快照公告检查已跳过", SHARED_JS)
        self.assertNotIn("settings.skip_announcements", SHARED_JS)
        # 服务端强制：B/S 两条筛选链路都写死跳过=否
        self.assertIn("skip_announcements=False", DASHBOARD_PY)
        self.assertIn("skip_announcements=False", WORKBENCH_PY)
        # 端到端链路：引擎把实际执行口径写进结果 meta，/api/status 再暴露给状态条
        self.assertIn('"announcement_check_skipped": bool(skip_announcements)', ENGINE_PY)
        self.assertIn('"announcement_check_skipped"', DASHBOARD_PY)

    def test_capital_ranking_uses_one_name(self):
        self.assertIn("资金排名", REALTIME_HTML)
        self.assertIn("资金排名", WORKBENCH_HTML)
        self.assertNotIn("资金排序", REALTIME_HTML)

    def test_workbench_run_options_are_labelled_as_this_run_only(self):
        """本次任务 vs 全局口径：作用域必须写明，避免改错地方。"""
        self.assertIn("本次任务参数", WORKBENCH_HTML)
        self.assertIn("全局口径", WORKBENCH_HTML)
        # 看板顶部只留状态入口（#config），不再就地改规则
        self.assertIn("#config", REALTIME_HTML)
        self.assertNotIn('id="skip-capital"', REALTIME_HTML)

    def test_polarity_mapping_is_inverted_in_the_request_body(self):
        """界面是正极性，请求体仍要发 skip_*；映射必须显式取反。"""
        self.assertIn("skip_capital_ranking: !$(\"#rank-capital\").checked", WORKBENCH_JS)
        # 公告检查不随请求体下发：它由服务端强制开启
        self.assertNotIn("skip_announcements", WORKBENCH_JS)


class BusyReasonTests(unittest.TestCase):
    def test_refresh_refusal_is_surfaced(self):
        """点刷新被占用时要有可见原因，不能转几秒就恢复（先前是无反馈）。"""
        self.assertIn('started.status !== "started"', REALTIME_JS)
        self.assertIn("SharedUI.notice", REALTIME_JS)

    def test_workbench_refusal_is_surfaced(self):
        self.assertIn("SharedUI.notice", WORKBENCH_JS)


if __name__ == "__main__":
    unittest.main()
