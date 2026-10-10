# -*- coding: utf-8 -*-
"""合成行情夹具：让 CLI 与看板入口在无网络、无真实状态的前提下跑完整流水线。

只用于测试：不读私有报告/持仓/影子库，不写真实运行状态。测试进程必须把
``A_SHARE_STATE_DIR``/``A_SHARE_REPORT_DIR`` 指向临时目录（整套 discover 已如此运行），
夹具本身也会把全部状态读写 patch 掉，双重保险。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock
from zoneinfo import ZoneInfo

import a_share_daily_screen as screen
import realtime_engine as engine

RISK_CACHE_TTL_SAFE = 60.0  # 缓存条目写得足够新，公告核验走缓存、不发请求

# 固定行情时间戳（2026-10-02 10:30 上海）：让 now/ts 在多次运行间完全一致，
# 状态机的 signal_age、时间戳字段才可逐值比较。
FIXED_TS = int(datetime(2026, 10, 2, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def make_enriched(code: str, name: str, **overrides: Any) -> screen.Enriched:
    """一个同时满足严格超短/趋势确认/准交集质量条件的合成标的。"""
    base: Dict[str, Any] = dict(
        code=code, name=name, price=12.0, change=3.6, turnover=4.0,
        amount=800_000_000.0, volume_ratio=1.8, high=12.3, low=11.7, open=11.8,
        prev_close=11.58, total_mv=12_000_000_000.0, float_mv=9_000_000_000.0,
        industry="半导体", timestamp=FIXED_TS, volume=60_000_000.0, kdate="2026-10-02",
        k_source="tencent_qfq", adj_close=12.0,
        ma5=11.6, ma10=11.4, ma20=11.0, prev_ma5=11.55, prev_ma10=11.35, prev_ma20=10.9,
        five_ret=0.06, dist60=0.05, ma20_dist=0.09, high_pull=0.4, cur_to_high=0.01,
        vol_vs_avg5=1.2, vwap=11.95, vwap_state="均价线上方",
        prior_high=12.2, prior_low=11.5,
        main_net=60_000_000.0, main_pct=6.0, super_net=35_000_000.0, super_pct=3.5,
        big_net=25_000_000.0, big_pct=2.5, mid_net=0, mid_pct=0, small_net=0, small_pct=0,
        flow_5m_inc=8_000_000.0, flow_15m_inc=12_000_000.0,
        price_above_vwap=True, flow_status="数据不足", flow_veto="",
        buy_ratio=1.6, risk_status="unknown",
    )
    base.update(overrides)
    return screen.Enriched(**base)


def market_row(e: screen.Enriched) -> Dict[str, Any]:
    """由 Enriched 造一行原始行情（filter_prefetch / sector_stats 需要）。"""
    return {
        "f12": e.code, "f14": e.name, "f2": e.price, "f3": e.change,
        "f4": e.change, "f5": e.volume, "f6": e.amount, "f7": 5.0,
        "f8": e.turnover, "f10": e.volume_ratio, "f15": e.high, "f16": e.low,
        "f17": e.open, "f18": e.prev_close, "f20": e.total_mv, "f21": e.float_mv,
        "f100": e.industry, "f124": e.timestamp, "f62": e.main_net,
    }


DEFAULT_BREADTH = {
    "total_rows": 5100, "provider_total": 5100, "adv": 3000, "dec": 2000, "flat": 100,
    "valid_change": 5100, "invalid_change": 0, "main_limit_up": 40, "main_limit_down": 5,
    "degraded": False, "quality_reason": "", "resonance_usable": True,
}


def risk_cache(risk_by_code: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """构造公告风险缓存；不在表里的代码因取数失败而判为 unknown。"""
    now = time.time()
    out: Dict[str, Dict[str, Any]] = {}
    for code, status in risk_by_code.items():
        out[code] = {
            "status": status,
            "keywords": ["测试"] if status != "clean" else [],
            "titles": ["合成公告"],
            "checked_at": now - 1.0,
        }
    return out


class SyntheticEnvironment:
    """patched 环境；``run_cli`` / ``run_engine`` 用它跑同一份输入。"""

    def __init__(
        self,
        enriched: List[screen.Enriched],
        *,
        risk_by_code: Optional[Dict[str, str]] = None,
        flow_history: Optional[Dict[str, Any]] = None,
        breadth: Optional[Dict[str, Any]] = None,
        intersection_state: Optional[Dict[str, Any]] = None,
        breakout_state: Optional[Dict[str, Any]] = None,
        market_status: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.enriched = list(enriched)
        self.market = [market_row(e) for e in enriched]
        self.risk_cache = risk_cache(risk_by_code or {})
        self.flow_history = flow_history or {}
        self.breadth = dict(breadth or DEFAULT_BREADTH)
        self.intersection_state = intersection_state or {"date": "", "items": {}}
        self.breakout_state = breakout_state or {"date": "", "items": {}}
        self.market_status = market_status or {"source": "eastmoney_push2", "complete": True}
        self.saved: Dict[str, Any] = {}

    # ── 构造 mock 栈 ──────────────────────────────────────────────
    def _patches(self) -> List[mock._patch]:
        saved = self.saved

        def rec(name):
            def _record(*args, **kwargs):
                saved[name] = {"args": args, "kwargs": kwargs}
            return _record

        patches = [
            mock.patch.object(screen, "fetch_market", return_value=(self.market, len(self.market))),
            mock.patch.object(screen, "get_market_fetch_status", return_value=dict(self.market_status)),
            mock.patch.object(screen, "fetch_indices", return_value=[]),
            mock.patch.object(screen, "fetch_sector_indices", return_value=[]),
            mock.patch.object(screen, "filter_prefetch", side_effect=lambda rows, modes, boards=None: list(self.market)),
            mock.patch.object(screen, "enrich_all", return_value=(list(self.enriched), [])),
            mock.patch.object(screen, "market_summary", return_value=dict(self.breadth)),
            mock.patch.object(screen, "load_flow_history", return_value=dict(self.flow_history)),
            mock.patch.object(screen, "save_flow_history", side_effect=rec("flow")),
            mock.patch.object(screen, "apply_flow_increments", side_effect=lambda *a, **k: None),
            mock.patch.object(screen, "fill_flow_increments_from_fflow", return_value=0),
            mock.patch.object(screen, "reset_flow_minute_round", side_effect=lambda *a, **k: None),
            mock.patch.object(screen, "load_intersection_state", return_value=dict(self.intersection_state)),
            mock.patch.object(screen, "save_intersection_state", side_effect=rec("intersection")),
            mock.patch.object(screen, "load_watchlist_breakout_state", return_value=dict(self.breakout_state)),
            mock.patch.object(screen, "save_watchlist_breakout_state", side_effect=rec("breakout")),
            mock.patch.object(screen, "load_holding_codes", return_value=set()),
            mock.patch.object(screen, "load_intersection_calibration", return_value=None),
            mock.patch.object(screen, "_load_announcement_risk_cache", return_value=dict(self.risk_cache)),
            mock.patch.object(screen, "_save_announcement_risk_cache", side_effect=lambda *a, **k: None),
            mock.patch.object(
                screen, "fetch_announcements",
                side_effect=RuntimeError("synthetic: announcement source offline"),
            ),
            # 看板专有依赖：分钟线与 5 分钟量能都不联网
            mock.patch.object(engine, "build_minute_map", return_value={}),
            mock.patch.object(engine, "enrich_min5", side_effect=lambda *a, **k: None),
            mock.patch.object(engine, "_save_kline_cache", side_effect=lambda *a, **k: None),
        ]
        return patches

    @contextlib.contextmanager
    def active(self):
        entered = []
        try:
            for p in self._patches():
                p.start()
                entered.append(p)
            yield self
        finally:
            for p in reversed(entered):
                p.stop()

    # ── 两个入口 ──────────────────────────────────────────────────
    def run_cli(self, argv: List[str]) -> Dict[str, Any]:
        """跑真实 main()，解析 JSON stdout。"""
        with self.active(), mock.patch.object(sys, "argv", ["a_share_daily_screen.py"] + argv):
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = screen.main()
        if code != 0:
            raise AssertionError(f"main() exit {code}: {err.getvalue()[-800:]}")
        return json.loads(buf.getvalue())

    def run_engine(self, **kwargs: Any) -> Dict[str, Any]:
        """跑看板入口 realtime_engine.run_screening。"""
        with self.active():
            return engine.run_screening(**kwargs)


def codes(rows: Any) -> List[str]:
    return [str(r.get("code")) for r in (rows or [])]


def phase_map(result: Dict[str, Any]) -> Dict[str, str]:
    return {
        str(r.get("code")): r.get("phase_code") or r.get("intersection_phase")
        for r in (result.get("intersection_states") or [])
    }
