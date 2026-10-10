#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline-friendly historical A-share research sources.

The daily-package parser is deliberately independent from the rest of the
screening engine.  It reads the official TongDaXin post-close ZIP format and
returns explicit share/yuan units.  Optional BaoStock and Shenwan adapters are
kept behind their respective dependencies; when a dependency is absent the
result is ``unsupported`` rather than a fabricated empty table.

This module is for historical research and export only.  It does not alter
screening state, trading permissions, or the decision record.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import struct
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import zipfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.data_sources.cache import JsonCache
from tools.data_sources.contracts import Result, ResultStatus, result_empty, result_error, result_ok
from tools.data_sources.http import HTTPClient, HTTPClientError
from tools.data_sources.symbols import SymbolError, normalize_security, validate_ymd


TDX_PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
TDX_BJ_FIRST_DAY = "20220506"
TDX_MIN_PRICED = {"sh": 10000, "sz": 3000, "bj": 50}
TDX_MARKETS = ("sh", "sz", "bj")
TDX_COD_RECORD_SIZE = 150
TDX_MD1_BLOCK_SIZE = 512

BAOSTOCK_HISTORY_SOURCE = "baostock_valuation_history"
BAOSTOCK_HISTORY_URL = "baostock://query_history_k_data_plus"
SW_INDUSTRY_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"


def _finite_number(value: Any, *, field: str, allow_none: bool = True) -> float | None:
    if value in (None, "", "-", "--", "null", "None"):
        if allow_none:
            return None
        raise ValueError(f"{field} 缺失")
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是数字: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} 不是有限数值")
    return number


def _safe_zip_names(archive: zipfile.ZipFile) -> set[str]:
    """Reject path traversal before reading any archive member."""
    names: set[str] = set()
    for info in archive.infolist():
        name = info.filename
        if info.is_dir():
            raise ValueError(f"通达信 ZIP 包含目录项: {name!r}")
        if (
            not name
            or "\\" in name
            or name.startswith("/")
            or name.startswith("\\")
            or re.search(r"(^|/)\.\.?(/|$)", name)
        ):
            raise ValueError(f"通达信 ZIP 路径不安全: {name!r}")
        if name in names:
            raise ValueError(f"通达信 ZIP 包含重复文件名: {name!r}")
        names.add(name)
    return names


def _tdx_market_files(archive: zipfile.ZipFile, names: set[str], market: str, ymd: str) -> tuple[bytes, bytes] | None:
    cod_name = f"{market}{ymd[2:]}.cod"
    md1_name = f"{market}{ymd[2:]}.md1"
    if market == "bj" and ymd < TDX_BJ_FIRST_DAY and cod_name not in names and md1_name not in names:
        return None
    if cod_name not in names or md1_name not in names:
        raise ValueError(f"通达信盘后包缺少 {cod_name}/{md1_name}")
    return archive.read(cod_name), archive.read(md1_name)


def parse_tdx_daily_package(
    content: bytes,
    ymd: str,
    *,
    min_priced: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Parse one official TongDaXin daily ZIP.

    ``ymd`` accepts ``YYYYMMDD`` or ``YYYY-MM-DD``.  By default the parser
    applies observed per-market row floors so a truncated archive cannot be
    mistaken for a complete market.  Tests and callers parsing a deliberately
    small fixture may pass lower floors explicitly.
    """
    normalized = validate_ymd(ymd).replace("-", "")
    floors = dict(TDX_MIN_PRICED if min_priced is None else min_priced)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"通达信盘后包无法解压: {type(exc).__name__}") from exc

    rows: list[dict[str, Any]] = []
    with archive:
        names = _safe_zip_names(archive)
        for market in TDX_MARKETS:
            files = _tdx_market_files(archive, names, market, normalized)
            if files is None:
                continue
            cod, md1 = files
            if len(cod) % TDX_COD_RECORD_SIZE or len(md1) % TDX_MD1_BLOCK_SIZE:
                raise ValueError(f"{market} 代码表或行情块长度不是整块")
            record_count = len(cod) // TDX_COD_RECORD_SIZE
            block_count = len(md1) // TDX_MD1_BLOCK_SIZE
            if record_count != block_count:
                raise ValueError(f"{market} 代码表 {record_count} 条、行情块 {block_count} 块，对不上")

            before = len(rows)
            codes: set[str] = set()
            seqs: set[int] = set()
            for offset in range(0, len(cod), TDX_COD_RECORD_SIZE):
                record = cod[offset : offset + TDX_COD_RECORD_SIZE]
                code = record[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
                seq = struct.unpack("<H", record[32:34])[0]
                if not re.fullmatch(r"[0-9]{6}", code):
                    raise ValueError(f"{market} 代码表出现非 6 位数字代码: {code!r}")
                if code in codes or seq in seqs:
                    raise ValueError(f"{market} 代码表有重复代码/行情块序号: {code!r}, seq={seq}")
                if seq >= block_count:
                    raise ValueError(f"{market}{code} 行情块序号越界: {seq}")
                codes.add(code)
                seqs.add(seq)
                block = md1[seq * TDX_MD1_BLOCK_SIZE : (seq + 1) * TDX_MD1_BLOCK_SIZE]
                if len(block) != TDX_MD1_BLOCK_SIZE:
                    raise ValueError(f"{market}{code} 行情块长度异常")

                prev_close = struct.unpack("<d", block[4:12])[0]
                open_, high, low, close = struct.unpack("<4d", block[12:44])
                volume_shares = struct.unpack("<Q", block[56:64])[0]
                amount_yuan = struct.unpack("<d", block[72:80])[0]
                values = (prev_close, open_, high, low, close, amount_yuan)
                if not all(math.isfinite(value) for value in values):
                    raise ValueError(f"{market}{code} 行情块出现非有限价格/金额")
                if close <= 0:
                    # Index and user-defined board records use an all-zero
                    # price block and are intentionally not counted as stocks.
                    continue
                if any(value < 0 for value in (open_, high, low, close, prev_close, amount_yuan)):
                    raise ValueError(f"{market}{code} 行情块出现负价格/金额")
                if high < low or not (low <= close <= high):
                    raise ValueError(f"{market}{code} 高低收价格关系异常")
                raw_name = record[40:72].split(b"\x00", 1)[0]
                try:
                    name = raw_name.decode("gbk").strip()
                except UnicodeDecodeError as exc:
                    raise ValueError(f"{market}{code} 名称不是合法 GBK") from exc
                if not name:
                    raise ValueError(f"{market}{code} 有价却没有名称")
                rows.append(
                    {
                        "date": f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}",
                        "data_date": f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}",
                        "market": market,
                        "code": code,
                        "name": name,
                        "prev_close": round(prev_close, 4),
                        "open": round(open_, 4),
                        "high": round(high, 4),
                        "low": round(low, 4),
                        "close": round(close, 4),
                        "volume": int(volume_shares),
                        "volume_shares": int(volume_shares),
                        "volume_unit": "shares",
                        "amount": round(amount_yuan, 2),
                        "amount_yuan": round(amount_yuan, 2),
                        "amount_unit": "yuan",
                        "source": "tdx_daily_package",
                    }
                )
            floor = int(floors.get(market, 0))
            if len(rows) - before < floor:
                raise ValueError(f"{market} 市场只有 {len(rows) - before} 条有价记录（要求至少 {floor} 条）")
    return rows


class TDXHistorySource:
    """Fetch and cache a complete TongDaXin daily package."""

    def __init__(self, client: HTTPClient | None = None, *, cache: JsonCache | None = None):
        self.client = client or HTTPClient()
        self.cache = cache or JsonCache("tdx_history_daily")

    def fetch(
        self,
        trading_date: str,
        *,
        code: str | None = None,
        force: bool = False,
        min_priced: Mapping[str, int] | None = None,
    ) -> Result:
        try:
            normalized_date = validate_ymd(trading_date)
        except ValueError as exc:
            return result_error(
                ResultStatus.UNSUPPORTED,
                source="tdx_daily_package",
                source_url=TDX_PACKAGE_URL,
                code="invalid_date",
                message=str(exc),
            )
        ymd = normalized_date.replace("-", "")
        normalized_code: str | None = None
        if code:
            try:
                normalized_code = normalize_security(code).code
            except SymbolError as exc:
                return result_error(
                    ResultStatus.UNSUPPORTED,
                    source="tdx_daily_package",
                    source_url=TDX_PACKAGE_URL.format(ymd=ymd),
                    code="invalid_code",
                    message=str(exc),
                    data_date=normalized_date,
                )
        url = TDX_PACKAGE_URL.format(ymd=ymd)
        cache_key = self.cache.key({"date": normalized_date, "min_priced": dict(min_priced or TDX_MIN_PRICED)})
        if not force:
            entry = self.cache.get(cache_key)
            if entry and isinstance(entry.value, list):
                rows = entry.value
                if normalized_code:
                    rows = [row for row in rows if row.get("code") == normalized_code]
                return result_ok(
                    rows,
                    source="tdx_daily_package",
                    source_url=url,
                    data_date=normalized_date,
                    freshness="cache",
                    cache={"hit": True, "stored_at": entry.stored_at, "path": str(self.cache.path)},
                    request_count=0,
                )
        try:
            response = self.client.get(url, timeout=90.0, retries=1, headers={"Accept": "application/zip,application/octet-stream"})
        except HTTPClientError as exc:
            status = ResultStatus.UNAVAILABLE
            code_name = "missing_date" if exc.status == 404 else exc.code
            message = (
                f"{normalized_date} 没有可用通达信盘后包：非交易日、尚未发布或早于保留范围"
                if exc.status == 404
                else f"通达信盘后包请求失败: {exc}"
            )
            return result_error(
                status,
                source="tdx_daily_package",
                source_url=url,
                code=code_name,
                message=message,
                data_date=normalized_date,
                request_count=self.client.request_count,
            )
        except Exception as exc:
            return result_error(
                ResultStatus.UNAVAILABLE,
                source="tdx_daily_package",
                source_url=url,
                code="network_error",
                message=f"通达信盘后包请求失败: {type(exc).__name__}: {exc}",
                data_date=normalized_date,
                request_count=self.client.request_count,
            )
        try:
            if not response.body.startswith(b"PK"):
                raise ValueError("响应不是 ZIP 文件，可能是错误页")
            rows = parse_tdx_daily_package(response.body, normalized_date, min_priced=min_priced)
        except (ValueError, zipfile.BadZipFile, EOFError, struct.error) as exc:
            return result_error(
                ResultStatus.UNAVAILABLE,
                source="tdx_daily_package",
                source_url=url,
                code="invalid_package",
                message=str(exc),
                data_date=normalized_date,
            )
        self.cache.set(cache_key, rows, data_date=normalized_date)
        selected = [row for row in rows if not normalized_code or row.get("code") == normalized_code]
        status = ResultStatus.OK if selected else ResultStatus.EMPTY
        if status == ResultStatus.EMPTY and normalized_code:
            return result_empty(
                source="tdx_daily_package",
                source_url=url,
                data=[],
                data_date=normalized_date,
                cache={"hit": False, "path": str(self.cache.path)},
                request_count=self.client.request_count,
            )
        return result_ok(
            selected,
            source="tdx_daily_package",
            source_url=url,
            data_date=normalized_date,
            freshness="fresh",
            cache={"hit": False, "path": str(self.cache.path)},
            request_count=self.client.request_count,
        )


def _first_value(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in row:
            return row[name]
    lower = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def parse_valuation_rows(rows: Iterable[Mapping[str, Any]], *, code: str | None = None) -> list[dict[str, Any]]:
    """Normalize BaoStock-like valuation rows without pandas."""
    expected = normalize_security(code).code if code else None
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        raw_code = str(_first_value(raw, ("code", "证券代码")) or "")
        row_code = raw_code.split(".")[-1] if "." in raw_code else raw_code
        if row_code and not re.fullmatch(r"[0-9]{6}", row_code):
            raise ValueError(f"估值历史代码不是 6 位数字: {raw_code!r}")
        if expected and row_code and row_code != expected:
            raise ValueError(f"估值历史混入其它证券: {row_code!r} != {expected!r}")
        row_date = str(_first_value(raw, ("date", "data_date", "日期")) or "")[:10]
        try:
            row_date = validate_ymd(row_date)
        except ValueError as exc:
            raise ValueError(f"估值历史日期异常: {row_date!r}") from exc
        values = {
            "close": _finite_number(_first_value(raw, ("close", "收盘", "收盘价")), field="close"),
            "pe_ttm": _finite_number(_first_value(raw, ("peTTM", "pe_ttm", "PE_TTM")), field="peTTM"),
            "pb_mrq": _finite_number(_first_value(raw, ("pbMRQ", "pb_mrq", "PB_MRQ")), field="pbMRQ"),
            "ps_ttm": _finite_number(_first_value(raw, ("psTTM", "ps_ttm", "PS_TTM")), field="psTTM"),
            "pcf_ncf_ttm": _finite_number(_first_value(raw, ("pcfNcfTTM", "pcf_ncf_ttm", "PCF_NCF_TTM")), field="pcfNcfTTM"),
            "turnover_pct": _finite_number(_first_value(raw, ("turn", "turnover_pct", "换手率")), field="turn"),
        }
        normalized.append(
            {
                "date": row_date,
                "data_date": row_date,
                "code": row_code or expected,
                **values,
                "trade_status": _first_value(raw, ("tradestatus", "trade_status", "停牌状态")),
                "is_st": _first_value(raw, ("isST", "is_st", "ST")),
                "source": "baostock_valuation_history",
            }
        )
    return sorted(normalized, key=lambda row: row["date"])


def _baostock_code(code: str) -> str:
    symbol = normalize_security(code)
    if symbol.market not in {"sh", "sz"}:
        raise SymbolError("baostock 不支持北交所历史估值；请使用其它当日快照源")
    return f"{symbol.market}.{symbol.code}"


def query_valuation_history(code: str, start_date: str, end_date: str) -> Result:
    """Optional BaoStock history including valuation, turnover, ST and halt state."""
    try:
        start = validate_ymd(start_date)
        end = validate_ymd(end_date)
        if start > end:
            raise ValueError("start_date 不能晚于 end_date")
        bs_code = _baostock_code(code)
    except (ValueError, SymbolError) as exc:
        return result_error(
            ResultStatus.UNSUPPORTED,
            source=BAOSTOCK_HISTORY_SOURCE,
            source_url=BAOSTOCK_HISTORY_URL,
            code="invalid_request",
            message=str(exc),
        )
    try:
        import baostock as bs  # type: ignore
    except ImportError:
        return result_error(
            ResultStatus.UNSUPPORTED,
            source=BAOSTOCK_HISTORY_SOURCE,
            source_url=BAOSTOCK_HISTORY_URL,
            code="optional_dependency_missing",
            message="未安装 baostock；历史估值/ST/停牌 topic 不可用，日线 ZIP 和筹码估算不受影响",
        )
    try:
        login = bs.login()
        if getattr(login, "error_code", "1") != "0":
            raise RuntimeError(f"baostock 登录失败: {login.error_code} {login.error_msg}")
        try:
            fields = "date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,turn,tradestatus,isST"
            response = bs.query_history_k_data_plus(
                bs_code,
                fields,
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="3",
            )
            if getattr(response, "error_code", "1") != "0":
                raise RuntimeError(f"baostock 查询失败: {response.error_code} {response.error_msg}")
            raw_rows: list[dict[str, Any]] = []
            while response.next():
                raw_rows.append(dict(zip(response.fields, response.get_row_data())))
        finally:
            bs.logout()
        rows = parse_valuation_rows(raw_rows, code=code)
    except Exception as exc:
        try:
            bs.logout()
        except Exception:
            pass
        return result_error(
            ResultStatus.UNAVAILABLE,
            source=BAOSTOCK_HISTORY_SOURCE,
            source_url=BAOSTOCK_HISTORY_URL,
            code="source_error",
            message=f"baostock 历史估值不可用: {type(exc).__name__}: {exc}",
            data_date=end,
        )
    if not rows:
        return result_empty(
            source=BAOSTOCK_HISTORY_SOURCE,
            source_url=BAOSTOCK_HISTORY_URL,
            data=[],
            data_date=end,
            as_of=end,
            warnings=["查询完成但该区间没有可用估值记录；不能据此判断无交易或无ST"],
        )
    return result_ok(
        rows,
        source=BAOSTOCK_HISTORY_SOURCE,
        source_url=BAOSTOCK_HISTORY_URL,
        data_date=end,
        as_of=end,
        freshness="fresh",
    )


def parse_industry_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a Shenwan classification-history export.

    Shenwan publishes classification codes rather than a reliable matching
    Chinese name table.  Codes are therefore retained as the authoritative
    identity and names, when supplied by a caller, are explicitly optional.
    """
    output: list[dict[str, Any]] = []
    for raw in rows:
        code = str(_first_value(raw, ("code", "股票代码", "证券代码")) or "").strip().zfill(6)
        industry_code = str(_first_value(raw, ("industry_code", "行业代码")) or "").strip()
        start = str(_first_value(raw, ("start_date", "计入日期", "effective_date")) or "")[:10]
        update = str(_first_value(raw, ("update_date", "更新日期")) or "")[:10] or None
        if not re.fullmatch(r"[0-9]{6}", code) or not industry_code:
            raise ValueError(f"申万行业历史身份字段异常: code={code!r}, industry_code={industry_code!r}")
        start = validate_ymd(start)
        try:
            update = validate_ymd(update) if update else None
        except ValueError as exc:
            raise ValueError(f"申万行业更新日期异常: {update!r}") from exc
        industry_code = industry_code.zfill(6)
        output.append(
            {
                "code": code,
                "start_date": start,
                "update_date": update,
                "industry_code": industry_code,
                "l1_code": industry_code[:2] + "0000",
                "l2_code": industry_code[:4] + "00",
                "industry_name": _first_value(raw, ("industry_name", "行业名称")),
                "source": "shenwan_industry_history",
            }
        )
    return sorted(output, key=lambda row: (row["code"], row["start_date"], row["industry_code"]))


def industry_as_of(rows: Iterable[Mapping[str, Any]], code: str, as_of: str) -> dict[str, Any] | None:
    """Select the last classification effective on or before ``as_of``."""
    normalized_code = normalize_security(code).code
    target = validate_ymd(as_of)
    normalized = parse_industry_rows(rows)
    selected = [row for row in normalized if row["code"] == normalized_code and row["start_date"] <= target]
    if not selected:
        return None
    return {**selected[-1], "as_of": target, "since": selected[-1]["start_date"]}


def fetch_shenwan_industry_history(client: HTTPClient | None = None) -> Result:
    """Fetch the optional official Shenwan XLS classification history."""
    http = client or HTTPClient()
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        return result_error(
            ResultStatus.UNSUPPORTED,
            source="shenwan_industry_history",
            source_url=SW_INDUSTRY_URL,
            code="optional_dependency_missing",
            message="未安装 pandas/xlrd；保留代码级行业历史导入/筛选函数，未把当前行业回填历史",
        )
    try:
        response = http.get(SW_INDUSTRY_URL, timeout=60.0, retries=1, headers={"Accept": "application/vnd.ms-excel"})
        frame = pd.read_excel(io.BytesIO(response.body))
        raw_rows = frame.to_dict(orient="records")
        rows = parse_industry_rows(raw_rows)
    except Exception as exc:
        return result_error(
            ResultStatus.UNAVAILABLE,
            source="shenwan_industry_history",
            source_url=SW_INDUSTRY_URL,
            code="source_error",
            message=f"申万行业历史不可用: {type(exc).__name__}: {exc}",
        )
    if not rows:
        return result_empty(
            source="shenwan_industry_history",
            source_url=SW_INDUSTRY_URL,
            data=[],
            warnings=["申万返回空表，不能把空表解释为无行业归属"],
        )
    return result_ok(rows, source="shenwan_industry_history", source_url=SW_INDUSTRY_URL, freshness="fresh")


def _load_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payload = payload["data"]
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("JSON 必须是对象数组，或包含 data 对象数组")
        return payload
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError("研究输入只支持 .json 或 .csv")


def _write_output(path: str | None, payload: Any) -> None:
    if not path:
        return
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _main() -> int:
    parser = argparse.ArgumentParser(description="A股历史日线/估值/申万行业研究查询（不改筛选状态）")
    parser.add_argument("--date", help="通达信盘后包日期，YYYYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--code", help="六位股票代码，可选：从全市场日线结果筛选")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--output", help="将 JSON 写入明确的本地路径")
    parser.add_argument("--valuation", action="store_true", help="查询可选 baostock 历史估值/ST/停牌")
    parser.add_argument("--start-date", help="估值起始日")
    parser.add_argument("--end-date", help="估值结束日")
    parser.add_argument("--industry-json", help="读取申万行业历史 JSON/CSV 本地导出并按 --code/--as-of 查询")
    parser.add_argument("--as-of", help="行业查询时点，YYYY-MM-DD")
    args = parser.parse_args()

    if args.valuation:
        if not (args.code and args.start_date and args.end_date):
            parser.error("--valuation 需要同时提供 --code、--start-date、--end-date")
        result = query_valuation_history(args.code, args.start_date, args.end_date)
        payload = result.to_dict()
    elif args.industry_json:
        if not (args.code and args.as_of):
            parser.error("--industry-json 需要同时提供 --code 和 --as-of")
        rows = _load_rows(Path(args.industry_json))
        payload = {"status": "ok", "data": industry_as_of(rows, args.code, args.as_of), "source": "shenwan_industry_history_local"}
    else:
        if not args.date:
            parser.error("默认模式需要 --date；估值模式请加 --valuation")
        result = TDXHistorySource().fetch(args.date, code=args.code)
        payload = result.to_dict()
    _write_output(args.output, payload)
    if args.json or args.output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"status={payload.get('status')} source={payload.get('source')} data_date={payload.get('data_date')}")
        data = payload.get("data")
        print(f"rows={len(data) if isinstance(data, list) else 1 if data else 0}")
        if payload.get("error"):
            print(f"error={payload['error'].get('message', payload['error'])}")
    return 0 if payload.get("status") not in {"unavailable", "unsupported"} else 2


if __name__ == "__main__":
    raise SystemExit(_main())
