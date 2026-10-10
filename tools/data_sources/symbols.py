"""Strict A-share security-code normalization.

Pure six-digit stock codes are accepted with the normal A-share stock-market
inference.  Explicit prefixes/suffixes are checked for contradiction instead
of silently routing to a different security.  Index/ETF callers should pass
``kind`` explicitly when a six-digit string is inherently ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


class SymbolError(ValueError):
    pass


@dataclass(frozen=True)
class SecuritySymbol:
    code: str
    market: str
    kind: str = "stock"

    @property
    def market_code(self) -> str:
        return {"sh": "1", "sz": "0", "bj": "2"}[self.market]

    @property
    def tencent(self) -> str:
        return f"{self.market}{self.code}"

    @property
    def secid(self) -> str:
        return f"{self.market_code}.{self.code}"

    @property
    def display(self) -> str:
        return f"{self.market.upper()}{self.code}"


_EXPLICIT_RE = re.compile(r"^(?:(sh|sz|bj)[._-]?([0-9]{6})|([0-9]{6})[._](SH|SZ|BJ))$", re.I)


def market_for_stock_code(code: str) -> str:
    if not re.fullmatch(r"[0-9]{6}", code):
        raise SymbolError("股票代码必须是 6 位数字")
    if code.startswith(("4", "8", "92")):
        return "bj"
    if code.startswith(("6", "9", "5")):
        return "sh"
    if code.startswith(("0", "2", "3", "1")):
        return "sz"
    raise SymbolError(f"无法判定股票市场: {code}")


def normalize_security(value: str, *, kind: str = "stock") -> SecuritySymbol:
    raw = str(value or "").strip()
    if not raw:
        raise SymbolError("证券代码不能为空")
    kind = str(kind).lower().strip()
    if kind not in {"stock", "etf", "index", "security"}:
        raise SymbolError("kind 只能是 stock/etf/index/security")

    m = _EXPLICIT_RE.fullmatch(raw)
    explicit_market: str | None = None
    code: str
    if m:
        if m.group(1):
            explicit_market = m.group(1).lower()
            code = m.group(2)
        else:
            code = m.group(3)
            explicit_market = m.group(4).lower()
    elif re.fullmatch(r"[0-9]{6}", raw):
        code = raw
    else:
        raise SymbolError(
            f"不支持的证券代码格式: {raw!r}；请使用 6 位代码、SH600519 或 600519.SH"
        )

    inferred = market_for_stock_code(code)
    if explicit_market and explicit_market != inferred:
        raise SymbolError(
            f"证券代码市场前缀矛盾: {raw!r} 推断为 {inferred.upper()}，却显式指定 {explicit_market.upper()}"
        )
    # Index codes are intentionally not guessed from a bare 000001/399001.
    # An explicit market prefix is still required for index callers so a stock
    # lookup cannot silently return a different security.
    if kind == "index" and not explicit_market:
        raise SymbolError("指数查询必须显式指定市场前缀，例如 SH000001")
    return SecuritySymbol(code=code, market=explicit_market or inferred, kind=kind)


def validate_ymd(value: str) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"[0-9]{8}", raw):
        normalized = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        normalized = raw
    else:
        raise ValueError(f"日期必须是 YYYYMMDD 或 YYYY-MM-DD: {value!r}")
    from datetime import date

    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"无效日期: {value!r}") from exc
    return normalized
