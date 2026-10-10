"""Allowlisted, on-demand context evidence for a single security."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import html
import json
import re
from typing import Any

from .cache import JsonCache, coalesced_fetch, result_cache_value, result_from_cache
from .contracts import Result, ResultStatus, result_empty, result_error, result_ok
from .events import DATACENTER_URL
from .http import HTTPClient, HTTPClientError
from .symbols import SymbolError, normalize_security, validate_ymd


CONTEXT_SOURCE = "context_evidence"
BEIJING = timezone(timedelta(hours=8))
MONITOR_URL = "https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json"
ANOMALY_URL = "https://dycalchis.eastmoney.com/price-anomaly/list"
THEME_URL = "https://push2.eastmoney.com/api/qt/stock/slist/get"
NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
RESEARCH_URL = "https://reportapi.eastmoney.com/report/list2"
CNINFO_IRM_KEYWORD_URL = "https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo"
CNINFO_IRM_QUESTION_URL = "https://irm.cninfo.com.cn/newircs/company/question"
SSE_E_BASE = "https://sns.sseinfo.com"
SINA_HQ_URL = "https://hq.sinajs.cn/list="

CONTEXT_TOPICS = (
    "monitor",
    "anomaly",
    "themes",
    "news",
    "research",
    "interaction",
    "dragon_tiger",
    "commodity",
)

ANOMALY_RULES = {
    1: "主板连续10个交易日内4次出现同向异常波动",
    2: "创业板连续10个交易日内3次出现同向异常波动",
    3: "科创板连续10个交易日内3次出现同向异常波动",
    4: "连续十个交易日内收盘涨跌幅偏离值累计达到正阈值",
    5: "连续十个交易日内收盘涨跌幅偏离值累计达到负阈值",
    6: "连续三十个交易日内收盘涨跌幅偏离值累计达到正阈值",
    7: "连续三十个交易日内收盘涨跌幅偏离值累计达到负阈值",
    8: "北交所连续10个交易日内3次出现同向异常波动",
}
DEFAULT_COMMODITY_MAP = {
    "copper": {"contract": "hf_CU", "name": "COMEX铜", "kind": "realtime_quote"},
    "gold": {"contract": "hf_XAU", "name": "COMEX黄金", "kind": "realtime_quote"},
    "crude_oil": {"contract": "hf_CL", "name": "NYMEX原油", "kind": "realtime_quote"},
}


def _first(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _strip_html(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def _date(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    raw = str(value).strip()
    if raw.isdigit() and len(raw) >= 10:
        try:
            return datetime.fromtimestamp(int(raw[:13]) / 1000, tz=BEIJING).isoformat(timespec="minutes")
        except (ValueError, OverflowError, OSError):
            return None
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw[:19]


def _validate_business_envelope(payload: Mapping[str, Any]) -> None:
    """Reject provider business failures before looking at empty containers."""
    envelopes: list[Mapping[str, Any]] = [payload]
    for key in ("result", "data"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            envelopes.append(nested)
    for envelope in envelopes:
        success = envelope.get("success")
        code = envelope.get("code")
        message = str(envelope.get("message") or payload.get("message") or "")
        if success is False:
            if str(code) == "9201" and ("空" in message or any(isinstance(envelope.get(key), list) for key in ("data", "rows", "list", "items"))):
                continue
            raise ValueError(f"上下文接口业务失败: {message or code}")
        if "code" in envelope and code not in (None, "", 0, "0", 200, "200"):
            raise ValueError(f"上下文接口业务失败: {code}")


def _rows_from_json(payload: Any, keys: tuple[str, ...] = ("data", "rows", "list", "items")) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        raise ValueError("上下文响应不是对象/列表")
    _validate_business_envelope(payload)
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = _rows_from_json(value, keys)
            if nested or any(k in value for k in keys):
                return nested
    # Some endpoints use result.data.
    if isinstance(payload.get("result"), Mapping):
        return _rows_from_json(payload["result"], keys)
    raise ValueError("上下文响应缺少可识别列表")


def parse_monitor_payload(payload: Any, *, code: str | None = None, as_of: str | None = None) -> list[dict[str, Any]]:
    rows = _rows_from_json(payload) if not isinstance(payload, list) else payload
    output = []
    for item in rows:
        item_code = str(_first(item, ("STKCODE", "code", "SECURITY_CODE")) or "")
        start, end = _date(_first(item, ("VALIDATESTARTDATE", "start"))), _date(_first(item, ("VALIDATEENDDATE", "end")))
        if code and item_code and item_code != code:
            continue
        active = not as_of or (not start or start[:10] <= as_of) and (not end or as_of <= end[:10])
        output.append({"code": item_code, "name": str(_first(item, ("STKNAME", "name")) or ""), "market": str(_first(item, ("MARKET", "market")) or "?"), "start": start, "end": end, "active": active, "link": str(_first(item, ("LINK_URL", "url")) or ""), "source": "eastmoney_stock_monitor", "raw": dict(item)})
    return output


def parse_anomaly_payload(payload: Mapping[str, Any], *, code: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("异动接口响应不是对象")
    _validate_business_envelope(payload)
    if payload.get("result") not in (0, "0", None):
        raise ValueError(f"异动接口拒绝: {payload.get('msg') if isinstance(payload, Mapping) else 'unknown'}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("异动响应缺少 data 列表")
    output = []
    for item in rows:
        if not isinstance(item, Mapping):
            raise ValueError("异动记录不是对象")
        item_code = str(item.get("c") or item.get("code") or "")
        if code and item_code and item_code != code:
            continue
        raw_rule = item.get("e")
        try:
            rule_code = int(raw_rule) * 10 if item.get("s") == 6 and int(raw_rule) in (4, 5, 6, 7) else int(raw_rule)
        except (TypeError, ValueError):
            rule_code = raw_rule
        output.append({"code": item_code, "name": item.get("n"), "market": item.get("m"), "change_pct": item.get("a"), "deviation": item.get("x"), "days": item.get("d"), "board": item.get("s"), "rule_code": rule_code, "rule": ANOMALY_RULES.get(rule_code, f"未知规则码 {rule_code}"), "is_today": item.get("o") != 2, "source": "eastmoney_price_anomaly", "raw": dict(item)})
    return {"date": str(payload.get("date") or ""), "pages": payload.get("pages"), "items": output}


def parse_theme_payload(payload: Mapping[str, Any], *, code: str) -> list[dict[str, Any]]:
    rows = _rows_from_json(payload, keys=("diff", "data", "list"))
    output = []
    for item in rows:
        # In the theme endpoint f12 is commonly a board identity such as
        # BK1001, not the requested security.  Keep the two identities
        # separate; only explicit security fields are eligible for filtering.
        item_code = str(_first(item, ("SECURITY_CODE", "stockCode", "secuCode")) or "")
        if item_code and item_code.zfill(6) != code:
            continue
        board_code = _first(item, ("boardCode", "bk", "f12"))
        output.append({"code": code, "identity_type": "board", "board_code": board_code, "concept": _first(item, ("conceptName", "f14", "name", "concept")), "concept_id": _first(item, ("conceptId", "bk", "f12")), "hit": _first(item, ("hitCount", "hit")), "source": "eastmoney_stock_themes", "raw": dict(item)})
    return output


def _parse_jsonp(text: str) -> Any:
    left, right = text.find("("), text.rfind(")")
    if left < 0 or right <= left:
        raise ValueError("JSONP 外壳缺失")
    return json.loads(text[left + 1:right])


def parse_news_payload(payload: Any, *, source_url: str = NEWS_URL) -> list[dict[str, Any]]:
    parsed = _parse_jsonp(payload) if isinstance(payload, str) else payload
    rows = _rows_from_json(parsed, keys=("cmsArticleWebOld", "data", "list"))
    return [{"title": _strip_html(_first(item, ("title", "articleTitle"))), "content": _strip_html(_first(item, ("content", "summary")))[:300], "published_at": _date(_first(item, ("date", "publishTime", "time"))), "source_name": _first(item, ("mediaName", "source")), "url": _first(item, ("url", "articleUrl")), "source": "eastmoney_stock_news", "source_url": source_url, "raw": dict(item)} for item in rows]


def parse_research_payload(payload: Mapping[str, Any], *, source_url: str = RESEARCH_URL) -> list[dict[str, Any]]:
    rows = _rows_from_json(payload, keys=("data", "list", "rows"))
    return [{"title": _first(item, ("title", "researchTitle", "报告标题")), "published_at": _date(_first(item, ("publishDate", "noticeDate", "公告日期"))), "institution": _first(item, ("orgSName", "orgName", "机构")), "rating": _first(item, ("emRatingName", "rating", "评级")), "eps_forecast": _first(item, ("predictEps", "EPS")), "url": _first(item, ("infoCode", "url")), "source": "eastmoney_research", "source_url": source_url, "raw": dict(item)} for item in rows]


def parse_cninfo_interaction_payload(payload: Mapping[str, Any], *, code: str) -> list[dict[str, Any]]:
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if rows is None and isinstance(payload, Mapping):
        rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("互动易响应缺少 rows/data")
    output = []
    for item in rows:
        if not isinstance(item, Mapping):
            raise ValueError("互动易问答记录不是对象")
        item_code = str(_first(item, ("stockCode", "code")) or code)
        if item_code != code:
            raise ValueError(f"互动易返回其他证券: {item_code}")
        output.append({"code": item_code, "company": _first(item, ("companyShortName", "company")), "question": _strip_html(_first(item, ("mainContent", "question"))), "answer": _strip_html(_first(item, ("attachedContent", "answer"))) or None, "answerer": _first(item, ("attachedAuthor", "answerer")), "published_at": _date(_first(item, ("pubDate", "ask_time"))), "url": _first(item, ("url", "detailUrl")), "source": "cninfo_irm", "raw": dict(item)})
    return output


def parse_sse_interaction_html(text: str, *, code: str) -> list[dict[str, Any]]:
    if not isinstance(text, str):
        raise ValueError("上证e互动响应不是文本")
    if not re.search(r"暂无|暂时没有", text) and "m_feed_item" not in text:
        raise ValueError("上证e互动既无问答也无明确空提示")
    output = []
    for chunk in re.split(r'<div class="m_feed_item[^\"]*" id="item-', text)[1:]:
        numbered = re.match(r"(\d+)", chunk)
        question = re.search(r'<div class="m_feed_txt"[^>]*>\s*<a[^>]*>:(.*?)\((\d{6})\)</a>(.*?)</div>', chunk, re.S)
        ask_time = re.search(r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', chunk)
        if not numbered or not question or not ask_time:
            raise ValueError("上证e互动问答结构改变")
        if question.group(2) != code:
            raise ValueError(f"上证e互动返回其他证券: {question.group(2)}")
        answer_part = chunk.partition('class="m_feed_detail m_qa"')[2]
        answer = answer_time = None
        if answer_part:
            body = re.search(r'<div class="m_feed_txt"[^>]*>(.*?)</div>', answer_part, re.S)
            when = re.search(r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', answer_part)
            if not body or not when:
                raise ValueError("上证e互动回复块结构改变")
            answer, answer_time = _strip_html(body.group(1)), _date(when.group(1))
        output.append({"id": numbered.group(1), "code": code, "company": _strip_html(question.group(1)), "question": _strip_html(question.group(3)), "question_time": _date(ask_time.group(1)), "answer": answer, "answer_time": answer_time, "source": "sse_e_interaction", "source_url": SSE_E_BASE})
    return output


def normalize_dragon_tiger(records: Iterable[Mapping[str, Any]], buys: Iterable[Mapping[str, Any]], sells: Iterable[Mapping[str, Any]], *, code: str, as_of: str | None = None, source_url: str = DATACENTER_URL) -> dict[str, Any]:
    def row_date(item: Mapping[str, Any]) -> str | None:
        return _date(_first(item, ("TRADE_DATE", "BILLBOARD_DATE", "date", "DATE")))

    normalized_records = []
    record_keys: set[tuple[Any, ...]] = set()
    record_dates: set[str] = set()
    for item in records:
        item_code = str(_first(item, ("SECURITY_CODE", "code")) or code)
        if item_code.zfill(6) != code:
            raise ValueError(f"龙虎榜返回其他证券: {item_code}")
        trade_date = row_date(item)
        if as_of and trade_date and trade_date[:10] > as_of:
            continue
        if as_of and not trade_date:
            continue
        key = (trade_date, _first(item, ("EXPLANATION", "reason")), _first(item, ("BILLBOARD_NET_AMT", "net_buy")))
        if key in record_keys:
            continue
        record_keys.add(key)
        if trade_date:
            record_dates.add(trade_date[:10])
        normalized_records.append({"code": code, "date": trade_date, "reason": _first(item, ("EXPLANATION", "reason")), "net_buy": _first(item, ("BILLBOARD_NET_AMT", "net_buy")), "turnover": _first(item, ("TURNOVERRATE", "turnover")), "source": "eastmoney_dragon_tiger", "raw": dict(item)})

    def normalize_seats(rows: Iterable[Mapping[str, Any]], side: str) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for item in rows:
            name = str(_first(item, ("OPERATEDEPT_NAME", "seat", "name")) or "").strip()
            if not name:
                continue
            seat_date = row_date(item)
            if as_of and (not seat_date or (record_dates and seat_date[:10] not in record_dates)):
                continue
            unique_key = f"{seat_date or ''}|{name}|{side}"
            unique[unique_key] = {"name": name, "date": seat_date, "side": side, "buy_amount": _first(item, ("BUY", "buy_amt")), "sell_amount": _first(item, ("SELL", "sell_amt")), "net_amount": _first(item, ("NET", "net")), "seat_code": _first(item, ("OPERATEDEPT_CODE", "seat_code")), "raw": dict(item)}
        return list(unique.values())
    buy_rows, sell_rows = normalize_seats(buys, "buy"), normalize_seats(sells, "sell")
    buy_names, sell_names = {row["name"] for row in buy_rows}, {row["name"] for row in sell_rows}
    return {"code": code, "records": normalized_records, "record_dates": sorted(record_dates, reverse=True), "seats": {"buy": buy_rows[:5], "sell": sell_rows[:5], "overlap": sorted(buy_names & sell_names)}, "institution": {"buy": [row for row in buy_rows if str(row.get("seat_code")) == "0"], "sell": [row for row in sell_rows if str(row.get("seat_code")) == "0"]}, "source": "eastmoney_dragon_tiger", "source_url": source_url, "as_of": as_of, "note": "按交易日期截止 as_of；席位只与同一上榜交易日关联，买卖重叠席位不重复累计，不把席位名称映射成未经核实的游资身份"}


def parse_commodity_payload(text: str, *, contract: str, source_url: str = SINA_HQ_URL) -> dict[str, Any]:
    match = re.search(r'="(.*)"', str(text), re.S)
    if not match:
        raise ValueError("新浪商品行情响应缺少 hq_str 数据")
    parts = match.group(1).split(",")
    if len(parts) < 6:
        raise ValueError("新浪商品行情字段不足")
    return {"contract": contract, "name": parts[0], "price": parts[1] or None, "change_pct": parts[2] or None, "bid": parts[3] or None, "ask": parts[4] or None, "time": parts[-1] or None, "timezone": "Asia/Shanghai", "kind": "realtime_quote", "source": "sina_commodity", "source_url": source_url, "raw_fields": parts}


class ContextSource:
    """One allowlisted entry point for all on-demand context topics."""

    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None, cache_ttl: int = 300, commodity_map: Mapping[str, Mapping[str, Any]] | None = None):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("context_evidence")
        self.cache_ttl = max(30, int(cache_ttl))
        self.commodity_map = {**DEFAULT_COMMODITY_MAP, **{str(k): dict(v) for k, v in (commodity_map or {}).items()}}
        self._sse_uid: dict[str, str] = {}

    def _result(self, topic: str, code: str, data: Any, *, status: ResultStatus | str = ResultStatus.OK, source_url: str = "", data_date: str | None = None, warnings: list[str] | None = None) -> Result:
        as_of = datetime.now(BEIJING).isoformat(timespec="seconds")
        return Result(status=status, data={"topic": topic, "code": code, "data": data, "as_of": as_of, "coverage": "on_demand", "note": "上下文为证据线索，不单独证明主线共振、盈利影响或买入许可"}, source=CONTEXT_SOURCE, source_url=source_url, data_date=data_date, as_of=as_of, freshness="fresh", warnings=warnings or [], request_count=self.client.request_count)

    def _sse_company_uid(self, code: str) -> str:
        if code in self._sse_uid:
            return self._sse_uid[code]
        # The official company index is paged by code. Bound the lookup so a
        # malformed page cannot turn a detail click into an unbounded crawl.
        for page in range(1, 76):
            response = self.client.post(
                SSE_E_BASE + "/allcompany.do",
                data={"code": "0", "order": "2", "areaId": "0", "page": str(page)},
                headers={"Referer": SSE_E_BASE + "/"},
                retries=1,
            )
            payload = response.json()
            content = payload.get("content") if isinstance(payload, Mapping) else None
            if not isinstance(content, str):
                raise ValueError("上证e互动公司列表缺少 content")
            pairs = re.findall(r"uid=['\"]?(\d+)['\"]?[^>]*>\s*<img[^>]*company/(\d{6})\.png", content)
            if not pairs and ("没有任何上市公司的信息" in content or page > 1):
                if "没有任何上市公司的信息" in content:
                    break
                raise ValueError("上证e互动公司列表结构改变")
            for uid, item_code in pairs:
                self._sse_uid[item_code] = uid
            if code in self._sse_uid:
                return self._sse_uid[code]
        raise ValueError(f"上证e互动没有 {code}（公司不在上交所或已退市）")

    def _fetch_topic(self, symbol: Any, topic: str, *, as_of: str | None, limit: int, contract: str | None) -> Result:
        code = symbol.code
        if topic == "monitor":
            response = self.client.get(MONITOR_URL, headers={"Referer": "https://vipmoney.eastmoney.com/"}, retries=1)
            return self._result(topic, code, parse_monitor_payload(response.json(), code=code, as_of=as_of), source_url=response.url, data_date=as_of)
        if topic == "anomaly":
            response = self.client.get(ANOMALY_URL, params={"team": "h5", "product": "EastMoney", "client": "WAP", "version": "9001", "name": "WAP", "user": "123", "pageSize": str(limit), "pageNo": "1"}, headers={"Referer": "https://vipmoney.eastmoney.com/"}, retries=1)
            parsed = parse_anomaly_payload(response.json(), code=code)
            return self._result(topic, code, parsed, source_url=response.url, data_date=as_of, status=ResultStatus.OK if parsed.get("items") else ResultStatus.EMPTY)
        if topic == "themes":
            response = self.client.get(THEME_URL, params={"secid": symbol.secid, "fields": "f12,f14,f3,f104", "ut": "7eea3edcaed734bea9c7b7a7f85d5b38"}, headers={"Referer": "https://quote.eastmoney.com/"}, retries=1)
            rows = parse_theme_payload(response.json(), code=code)
            return self._result(topic, code, rows, source_url=response.url, data_date=as_of, status=ResultStatus.OK if rows else ResultStatus.EMPTY)
        if topic == "news":
            inner = json.dumps({"uid": "", "keyword": code, "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web", "clientVersion": "curr", "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": limit, "preTag": "", "postTag": ""}}}, separators=(",", ":"))
            response = self.client.get(NEWS_URL, params={"cb": "jQuery_news", "param": inner}, headers={"Referer": "https://so.eastmoney.com/"}, retries=1)
            rows = parse_news_payload(response.text, source_url=response.url)
            return self._result(topic, code, rows, source_url=response.url, data_date=as_of, status=ResultStatus.OK if rows else ResultStatus.EMPTY)
        if topic == "research":
            response = self.client.get(RESEARCH_URL, params={"pageNo": "1", "pageSize": str(limit), "code": code}, headers={"Referer": "https://data.eastmoney.com/report/"}, retries=1)
            rows = parse_research_payload(response.json(), source_url=response.url)
            return self._result(topic, code, rows, source_url=response.url, data_date=as_of, status=ResultStatus.OK if rows else ResultStatus.EMPTY)
        if topic == "interaction":
            return self._interaction(symbol, as_of=as_of, limit=limit)
        if topic == "dragon_tiger":
            return self._dragon_tiger(symbol, as_of=as_of, limit=limit)
        if topic == "commodity":
            return self._commodity(contract=contract, code=code, as_of=as_of)
        raise ValueError(f"未允许的上下文 topic: {topic}")

    def _interaction(self, symbol: Any, *, as_of: str | None, limit: int) -> Result:
        if symbol.market == "bj":
            return self._result("interaction", symbol.code, [], status=ResultStatus.UNSUPPORTED, source_url=CNINFO_IRM_QUESTION_URL, warnings=["深市互动易与沪市上证e互动均不覆盖北交所"])
        if symbol.market == "sh":
            uid = self._sse_company_uid(symbol.code)
            response = self.client.post(SSE_E_BASE + "/ajax/userfeeds.do", data={"typeCode": "company", "type": "11", "pageSize": str(limit), "uid": uid, "page": "1"}, headers={"Referer": SSE_E_BASE + "/"}, retries=1)
            rows = parse_sse_interaction_html(response.text, code=symbol.code)
            return self._result("interaction", symbol.code, rows, source_url=response.url, data_date=as_of, status=ResultStatus.OK if rows else ResultStatus.EMPTY)
        keyboard = self.client.post(CNINFO_IRM_KEYWORD_URL, data={"keyWord": symbol.code}, headers={"Referer": "https://irm.cninfo.com.cn/"}, retries=1)
        candidates = (keyboard.json() or {}).get("data") if isinstance(keyboard.json(), Mapping) else None
        if not isinstance(candidates, list) or not candidates:
            return self._result("interaction", symbol.code, [], status=ResultStatus.EMPTY, source_url=keyboard.url, data_date=as_of)
        org_id = _first(candidates[0], ("secid", "orgId")) if isinstance(candidates[0], Mapping) else None
        if not org_id:
            raise ValueError("互动易公司映射缺少 orgId")
        response = self.client.post(CNINFO_IRM_QUESTION_URL, params={"_t": "1", "stockcode": symbol.code, "orgId": org_id, "pageSize": str(limit), "pageNum": "1", "keyWord": "", "startDay": "", "endDay": ""}, headers={"Referer": "https://irm.cninfo.com.cn/"}, retries=1)
        rows = parse_cninfo_interaction_payload(response.json(), code=symbol.code)
        return self._result("interaction", symbol.code, rows, source_url=response.url, data_date=as_of, status=ResultStatus.OK if rows else ResultStatus.EMPTY)

    def _dragon_tiger(self, symbol: Any, *, as_of: str | None, limit: int) -> Result:
        if not as_of:
            as_of = datetime.now(BEIJING).date().isoformat()
        records: list[Mapping[str, Any]] = []
        buy: list[Mapping[str, Any]] = []
        sell: list[Mapping[str, Any]] = []
        urls = []
        configs = [("RPT_DAILYBILLBOARD_DETAILSNEW", "records"), ("RPT_BILLBOARD_DAILYDETAILSBUY", "buy"), ("RPT_BILLBOARD_DAILYDETAILSSELL", "sell")]
        for report, kind in configs:
            response = self.client.get(DATACENTER_URL, params={"reportName": report, "columns": "ALL", "source": "WEB", "client": "WEB", "filter": f'(SECURITY_CODE="{symbol.code}")(TRADE_DATE<=\'{as_of}\')', "pageNumber": "1", "pageSize": str(min(limit, 100)), "sortColumns": "TRADE_DATE", "sortTypes": "-1"}, headers={"Referer": "https://data.eastmoney.com/"}, retries=1)
            urls.append(response.url)
            rows = _rows_from_json(response.json(), keys=("data",))
            if kind == "records":
                records = rows
            elif kind == "buy":
                buy = rows
            else:
                sell = rows
        data = normalize_dragon_tiger(records, buy, sell, code=symbol.code, as_of=as_of, source_url=urls[0] if urls else DATACENTER_URL)
        actual_date = max(data.get("record_dates") or [], default=None)
        warnings = [] if actual_date else ["龙虎榜没有可确认的历史上榜交易日；不能把请求日期当作数据日期"]
        return self._result("dragon_tiger", symbol.code, data, source_url=urls[0] if urls else DATACENTER_URL, data_date=actual_date, status=ResultStatus.OK if data.get("records") else ResultStatus.EMPTY, warnings=warnings)

    def _commodity(self, *, contract: str | None, code: str, as_of: str | None) -> Result:
        chosen = self.commodity_map.get(contract or "copper") if (contract or "copper") in self.commodity_map else next((value for value in self.commodity_map.values() if value.get("contract") == contract), None)
        if not chosen:
            return self._result("commodity", code, [], status=ResultStatus.UNSUPPORTED, source_url=SINA_HQ_URL, warnings=["商品合约不在本地 allowlist"])
        ticker = str(chosen["contract"])
        response = self.client.get(SINA_HQ_URL + ticker, headers={"Referer": "https://finance.sina.com.cn/"}, retries=1)
        row = parse_commodity_payload(response.text, contract=ticker, source_url=response.url)
        row["display_name"] = chosen.get("name")
        return self._result("commodity", code, row, source_url=response.url, data_date=as_of)

    @coalesced_fetch("context")
    def fetch(self, code: str, *, topic: str, as_of: str | None = None, force: bool = False, limit: int = 20, contract: str | None = None) -> Result:
        if topic not in CONTEXT_TOPICS:
            return result_error(ResultStatus.UNSUPPORTED, source=CONTEXT_SOURCE, source_url="", code="unsupported_topic", message=f"topic 必须是: {', '.join(CONTEXT_TOPICS)}")
        try:
            symbol = normalize_security(code)
            if as_of is None:
                as_of = datetime.now(BEIJING).date().isoformat()
            as_of = validate_ymd(as_of)
        except (SymbolError, ValueError) as exc:
            return result_error(ResultStatus.UNSUPPORTED, source=CONTEXT_SOURCE, source_url="", code="invalid_request", message=str(exc))
        key = self.cache.key({"code": symbol.code, "market": symbol.market, "topic": topic, "as_of": as_of, "limit": limit, "contract": contract})
        if not force:
            cached = self.cache.get(key, ttl=self.cache_ttl)
            restored = result_from_cache(cached.value, default_source=CONTEXT_SOURCE, default_source_url="") if cached else None
            if restored is not None:
                return restored
        try:
            result = self._fetch_topic(symbol, topic, as_of=as_of, limit=limit, contract=contract)
        except HTTPClientError as exc:
            result = result_error(ResultStatus.UNAVAILABLE, source=CONTEXT_SOURCE, source_url="", code=exc.code, message=str(exc), retryable=exc.retryable, data_date=as_of, as_of=as_of)
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            result = result_error(ResultStatus.UNAVAILABLE, source=CONTEXT_SOURCE, source_url="", code="malformed_or_unavailable", message=str(exc), retryable=True, data_date=as_of, as_of=as_of)
        if result.status in {ResultStatus.OK.value, ResultStatus.EMPTY.value, ResultStatus.PARTIAL.value, ResultStatus.STALE.value}:
            self.cache.set(key, result_cache_value(result), ttl=self.cache_ttl, data_date=as_of)
        elif result.status == ResultStatus.UNAVAILABLE.value:
            self.cache.set(key, result_cache_value(result), ttl=min(self.cache_ttl, 30), data_date=as_of)
        return result
