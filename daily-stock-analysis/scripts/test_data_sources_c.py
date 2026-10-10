"""Offline tests for allowlisted per-security context evidence."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.data_sources.cache import JsonCache
from tools.data_sources.context import (
    ContextSource,
    normalize_dragon_tiger,
    parse_anomaly_payload,
    parse_commodity_payload,
    parse_news_payload,
    parse_sse_interaction_html,
)
from tools.data_sources.http import HTTPClient, HTTPResponse


class ContextParserTests(unittest.TestCase):
    def test_anomaly_keeps_unknown_rule_code(self) -> None:
        result = parse_anomaly_payload({"result": 0, "date": "2026-10-03", "data": [{"c": "600519", "n": "样本", "e": 99, "m": 1, "a": 2.0}]}, code="600519")
        self.assertEqual(result["items"][0]["rule"], "未知规则码 99")

    def test_jsonp_news_and_commodity_preserve_source_fields(self) -> None:
        news = parse_news_payload('jQuery_news({"result":{"cmsArticleWebOld":[{"title":"<b>标题</b>","date":"2026-10-03 10:00","mediaName":"来源","url":"https://example.test/n"}]}})')
        self.assertEqual(news[0]["title"], "标题")
        commodity = parse_commodity_payload('var hq_str_hf_CU="铜,100,1.2,99,101,2026-10-03 10:00:00";', contract="hf_CU")
        self.assertEqual(commodity["contract"], "hf_CU")
        self.assertEqual(commodity["timezone"], "Asia/Shanghai")

    def test_sse_interaction_requires_company_identity_and_preserves_answer(self) -> None:
        text = '''<div class="m_feed_item" id="item-7"><div class="m_feed_detail m_qa_detail"><div class="m_feed_txt"><a>:样本(600519)</a>公司如何？</div><div class="m_feed_from"><span>2026年10月03日 10:00</span></div></div><div class="m_feed_detail m_qa"><div class="m_feed_txt">公司回复内容</div><div class="m_feed_from"><span>2026年10月03日 12:00</span></div></div></div>'''
        rows = parse_sse_interaction_html(text, code="600519")
        self.assertEqual(rows[0]["answer"], "公司回复内容")
        with self.assertRaises(ValueError):
            parse_sse_interaction_html(text.replace("600519", "600000"), code="600519")

    def test_dragon_tiger_keeps_buy_sell_overlap_without_double_counting(self) -> None:
        data = normalize_dragon_tiger(
            [{"SECURITY_CODE": "600519", "TRADE_DATE": "20261003", "EXPLANATION": "涨幅"}],
            [{"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0", "BUY": 100}],
            [{"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0", "SELL": 80}],
            code="600519",
        )
        self.assertEqual(data["seats"]["overlap"], ["机构专用"])
        self.assertEqual(len(data["institution"]["buy"]), 1)
        self.assertEqual(len(data["institution"]["sell"]), 1)


class ContextSourceTests(unittest.TestCase):
    def test_topics_use_allowlisted_routes_and_distinct_statuses(self) -> None:
        def transport(method, url, params, data, **kwargs):
            if url.endswith("stock_monitor.json"):
                return HTTPResponse(200, url, json.dumps([{"STKCODE": "600519", "STKNAME": "样本", "MARKET": "X", "VALIDATESTARTDATE": "2026-10-01", "VALIDATEENDDATE": "2026-10-31"}]).encode(), {"Content-Type": "application/json"})
            if "price-anomaly" in url:
                return HTTPResponse(200, url, json.dumps({"result": 0, "data": []}).encode(), {"Content-Type": "application/json"})
            if "slist/get" in url:
                return HTTPResponse(200, url, json.dumps({"data": {"diff": [{"f12": "600519", "f14": "白酒"}]}}).encode(), {"Content-Type": "application/json"})
            if "search/jsonp" in url:
                body = 'jQuery_news({"result":{"cmsArticleWebOld":[]}})'
                return HTTPResponse(200, url, body.encode(), {"Content-Type": "text/plain"})
            if "report/list2" in url:
                return HTTPResponse(200, url, json.dumps({"data": []}).encode(), {"Content-Type": "application/json"})
            if "queryKeyboardInfo" in url:
                return HTTPResponse(200, url, json.dumps({"data": [{"secid": "org-519"}]}).encode(), {"Content-Type": "application/json"})
            if "company/question" in url:
                return HTTPResponse(200, url, json.dumps({"rows": [{"stockCode": params.get("stockcode", "600519"), "companyShortName": "样本", "mainContent": "问题", "attachedContent": "回答", "pubDate": 1790973600000}]}).encode(), {"Content-Type": "application/json"})
            if "datacenter" in url:
                report = params.get("reportName")
                rows = [{"SECURITY_CODE": "600519", "TRADE_DATE": "20261003", "EXPLANATION": "涨幅"}] if report == "RPT_DAILYBILLBOARD_DETAILSNEW" else []
                return HTTPResponse(200, url, json.dumps({"result": {"data": rows}}).encode(), {"Content-Type": "application/json"})
            if "hq.sinajs.cn" in url:
                return HTTPResponse(200, url, b'var hq_str_hf_CU="CU,100,1,99,101,2026-10-03 10:00:00";', {"Content-Type": "text/plain; charset=GBK"})
            raise AssertionError(f"unexpected route: {url}")

        with tempfile.TemporaryDirectory() as directory:
            source = ContextSource(client=HTTPClient(transport=transport), cache=JsonCache("context_c", path=Path(directory) / "cache.json"))
            self.assertEqual(source.fetch("600519", topic="monitor", as_of="2026-10-03").status, "ok")
            self.assertEqual(source.fetch("600519", topic="anomaly", as_of="2026-10-03").status, "empty")
            self.assertEqual(source.fetch("600519", topic="themes", as_of="2026-10-03").data["data"][0]["concept"], "白酒")
            self.assertEqual(source.fetch("600519", topic="news", as_of="2026-10-03").status, "empty")
            self.assertEqual(source.fetch("600519", topic="research", as_of="2026-10-03").status, "empty")
            self.assertEqual(source.fetch("000001", topic="interaction", as_of="2026-10-03").status, "ok")
            self.assertEqual(source.fetch("600519", topic="dragon_tiger", as_of="2026-10-03").status, "ok")
            self.assertEqual(source.fetch("600519", topic="commodity", contract="copper", as_of="2026-10-03").status, "ok")
            self.assertEqual(source.fetch("600519", topic="not_allowed", as_of="2026-10-03").status, "unsupported")


if __name__ == "__main__":
    unittest.main()
