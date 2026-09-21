#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金约束模拟盘引擎（10 万本金 · AI 决策 · 净值验证）

与项目既有「模拟仓」的区别（见 tools/rule_config.py 的 sim 段注释）：
  - 既有模拟仓 = **信号样本采集器**，框架第 69/104 行明确「模拟资金与笔数不限」；
  - 本引擎     = **资金曲线验证器**，固定 10 万本金，考核净值与回撤。

职责边界：本工具只做模拟盘记账与规则判定，**不触碰真实仓**，也不产生真实下单。

设计原则：全部规则判定在本模块完成（不在 LLM 侧），所以定时任务只需
「调用脚本 -> 读结果 -> 落盘」，避免每轮把报告全文塞进上下文、也避免
由语言模型自由裁量导致结果不可复现。

命令：
    status                      账户状态（现金 / 持仓 / 净值 / 累计收益）
    scan                        扫最新报告，输出可开仓清单（档位 / 股数 / 止损止盈）
    open --code 600360          开仓（不指定参数时自动判档、取现价、算股数）
    check-exit                  检查今日应卖出的持仓（止损 / 止盈 / T+1 窗口截止）
    close --code 600360         平仓
    settle                      日终结算并写入当日净值
    nav                         净值曲线
    sync                        把账本同步写回决策记录快照

账本（权威）：tools/sim_data/sim_account.json
展示（人读）：决策记录/YYYYMMDD.md 的 `## 收盘持仓快照`，由 sync 自动生成
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
for _p in (PROJECT_ROOT, TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:  # Windows 控制台默认 cp936，会让中文输出乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

from tools.rule_config import RULE_CONFIG
from tools.report_parser import parse_screening_report
from tools.query_quote import fetch_realtime_quotes

SIM = RULE_CONFIG["sim"]
POS_CFG = SIM["position"]
ENTRY_CFG = SIM["entry"]
EXIT_CFG = SIM["exit"]

ACCOUNT_FILE = PROJECT_ROOT / SIM["account_file"]
DECISION_DIR = PROJECT_ROOT / "决策记录"
REPORTS_DIR = PROJECT_ROOT / "筛选结果"

LOT = 100  # A 股一手 100 股


# ══════════════════════════════════════════════════════════════
# 数值解析（报告里混用全角负号 −、单位 万/亿、百分号）
# ══════════════════════════════════════════════════════════════

_NULL_TOKENS = {"", "-", "--", "—", "None", "none", "null", "N/A", "n/a", "－"}


def _clean(raw: Any) -> Optional[str]:
    """统一空白与各类减号，返回可解析字符串；空值返回 None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in _NULL_TOKENS:
        return None
    s = s.replace("\u2212", "-").replace("\uff0d", "-").replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace(",", "").replace(" ", "")
    return s or None


def parse_pct(raw: Any) -> Optional[float]:
    """'3.12%' / '+1.2pct' / '-0.6' -> float(百分数数值)。"""
    s = _clean(raw)
    if s is None:
        return None
    s = s.replace("%", "").replace("pct", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_amount_wan(raw: Any) -> Optional[float]:
    """'1.43亿' / '+130万' / '−40万' -> 以「万」为单位的 float。"""
    s = _clean(raw)
    if s is None:
        return None
    s = s.replace("+", "")
    mult = 1.0
    if s.endswith("亿"):
        mult, s = 10000.0, s[:-1]
    elif s.endswith("万"):
        mult, s = 1.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def parse_int(raw: Any) -> Optional[int]:
    s = _clean(raw)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _has(text: Any, *needles: str) -> bool:
    s = str(text or "")
    return any(n in s for n in needles)


# ══════════════════════════════════════════════════════════════
# 账本读写
# ══════════════════════════════════════════════════════════════

def now_dt() -> datetime:
    return datetime.now()


def today_str() -> str:
    return now_dt().strftime("%Y%m%d")


def load_account() -> Dict[str, Any]:
    """加载账本；不存在则按配置初始化（本金 + 全现金）。"""
    if ACCOUNT_FILE.exists():
        try:
            data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("positions", [])
                data.setdefault("closed", [])
                data.setdefault("nav_history", [])
                data.setdefault("cash", float(SIM["initial_capital"]))
                return data
        except Exception as exc:
            raise SystemExit(f"账本损坏，拒绝覆盖以免丢数据：{ACCOUNT_FILE}\n{exc}")
    return {
        "version": "1.0",
        "created": now_dt().strftime("%Y-%m-%d"),
        "initial_capital": float(SIM["initial_capital"]),
        "cash": float(SIM["initial_capital"]),
        "positions": [],
        "closed": [],
        "nav_history": [],
        "meta": {"last_settle": None, "settle_count": 0},
    }


def save_account(acc: Dict[str, Any]) -> None:
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps(acc, ensure_ascii=False, indent=2), encoding="utf-8")


def held_shares(acc: Dict[str, Any], code: str) -> int:
    return sum(int(p.get("shares", 0)) for p in acc["positions"] if str(p.get("code")) == str(code))


def find_position(acc: Dict[str, Any], code: str) -> Optional[Dict[str, Any]]:
    for p in acc["positions"]:
        if str(p.get("code")) == str(code):
            return p
    return None


# ══════════════════════════════════════════════════════════════
# 执行窗口（框架：rule_config.execution.time_windows）
# ══════════════════════════════════════════════════════════════

def entry_window(dt: Optional[datetime] = None) -> Tuple[bool, float, str]:
    """返回 (是否允许开新仓, 仓位倍数, 窗口名)。

    框架窗口：09:30-09:40 观察禁买、09:40-09:50 等待确认、09:50-10:45 第一买点、
    10:45-11:30 早盘确认、11:30-13:00 午休、13:00-13:45 午后回流·仓位减半、
    13:45-14:20 午后尾段、14:20 后禁新仓。
    """
    dt = dt or now_dt()
    if dt.weekday() >= 5:
        return False, 0.0, "非交易日"
    t = dt.hour * 60 + dt.minute
    if t < 9 * 60 + 30:
        return False, 0.0, "盘前"
    if t < 9 * 60 + 50:
        return False, 0.0, "观察期/等待确认·禁买"
    if t < 10 * 60 + 45:
        return True, 1.0, "第一买点窗口"
    if t < 11 * 60 + 30:
        return True, 1.0, "早盘确认期"
    if t < 13 * 60:
        return False, 0.0, "午休"
    if t < 13 * 60 + 45:
        return True, 0.5, "午后回流·仓位减半"
    if t < 14 * 60 + 20:
        return True, 1.0, "午后尾段"
    if t < 14 * 60 + 40:
        return False, 0.0, "尾盘风控·禁新仓"
    return False, 0.0, "收盘/盘后·仅持仓管理"


def is_t1_sell_window(dt: Optional[datetime] = None) -> bool:
    """框架第113行：次日 09:30–09:45，09:45 为常规退出截止。"""
    dt = dt or now_dt()
    if dt.weekday() >= 5:
        return False
    t = dt.hour * 60 + dt.minute
    start = int(EXIT_CFG["t1_window_start"][:2]) * 60 + int(EXIT_CFG["t1_window_start"][3:])
    end = int(EXIT_CFG["t1_exit_deadline"][:2]) * 60 + int(EXIT_CFG["t1_exit_deadline"][3:])
    return start <= t <= end


def past_t1_deadline(dt: Optional[datetime] = None) -> bool:
    dt = dt or now_dt()
    t = dt.hour * 60 + dt.minute
    end = int(EXIT_CFG["t1_exit_deadline"][:2]) * 60 + int(EXIT_CFG["t1_exit_deadline"][3:])
    return t > end


# ══════════════════════════════════════════════════════════════
# 档位判定（正式门槛 / 已许可实验类别）
# ══════════════════════════════════════════════════════════════

def _veto_reason(row: Dict[str, Any]) -> Optional[str]:
    """框架红线一票否决：公告 avoid/unknown、超大单为负。"""
    ann = str(row.get("公告风险") or "")
    for bad in ENTRY_CFG["veto_announcement"]:
        if bad in ann.lower():
            return f"公告{bad}一票否决"
    if ENTRY_CFG["veto_negative_super_net"]:
        sup = parse_amount_wan(row.get("超大单"))
        if sup is not None and sup < 0:
            return "超大单为负一票否决（散户堆量）"
    return None


def classify(row: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """判定一条候选的档位。

    kind='short' 低吸超短线表（A类要求 5分增量>500万）；'trend' 低吸短线趋势表
    （无 5 分钟增量列时按 A 类 500 万口径，避免用低门槛放行）。
    返回 {tier, category, label, blockers, metrics, eligible}
    """
    code = str(row.get("代码") or "").strip()
    cls = str(row.get("类") or "").strip().upper()
    main_pct = parse_pct(row.get("主力净占比"))
    inc5 = parse_amount_wan(row.get("5分钟增量"))
    pull = parse_pct(row.get("高位回落"))
    dom = str(row.get("超单主导") or "")
    vwap = str(row.get("均价线") or "")
    fund = str(row.get("资金状态") or "")
    reson = str(row.get("共振") or "")
    sector_cnt = parse_int(row.get("板块内候选"))
    super_net = parse_amount_wan(row.get("超大单"))

    metrics = {
        "code": code,
        "name": str(row.get("名称") or "").strip(),
        "class": cls,
        "price_raw": _clean(row.get("现价")),
        "main_pct": main_pct,
        "inc5_wan": inc5,
        "pullback_pct": pull,
        "super_net_wan": super_net,
        "sector": str(row.get("板块") or "-").strip() or "-",
        "sector_candidates": sector_cnt,
        "dominance": dom,
        "fund_state": fund,
        "kind": kind,
    }

    veto = _veto_reason(row)
    if veto:
        return {"tier": None, "category": "veto", "label": veto, "blockers": [veto],
                "metrics": metrics, "eligible": False}

    # ---- 正式门槛（全部须满足） ----
    blockers: List[str] = []
    if cls not in ("A", "B"):
        blockers.append(f"类={cls or '?'}（非A/B）")
    if main_pct is None:
        blockers.append("主力净占比缺失")
    elif not main_pct > 5.0:
        blockers.append(f"主力{main_pct:.1f}%≤5%")
    need_5m = 500.0 if cls != "B" else 100.0  # 单位：万（A类>500万、B类>100万）
    if inc5 is None:
        blockers.append("5分钟增量缺失")
    elif not inc5 > need_5m:
        blockers.append(f"5分钟{inc5:+.0f}万≤{need_5m:.0f}万")
    if pull is None:
        blockers.append("回落缺失")
    elif not pull < 1.0:
        blockers.append(f"回落{pull:.2f}%≥1.0%")
    if not _has(vwap, "均价线上方"):
        blockers.append("未站上均价线")
    if "✓" not in dom:
        blockers.append("超单主导✗")
    if "有效流入" not in fund:
        blockers.append(f"资金状态={fund or '-'}")
    if reson.strip() != "是":
        blockers.append("无板块共振")

    formal_full = not blockers

    if formal_full and ENTRY_CFG["allow_formal"]:
        # 加强档：主力≥10% 且 5分钟增量≥1000万 且 板块内候选≥2
        if (main_pct or 0) >= 10.0 and (inc5 or 0) >= 1000.0 and (sector_cnt or 0) >= 2:
            return {"tier": "B", "category": "formal", "label": "正式全过·加强档",
                    "blockers": [], "metrics": metrics, "eligible": True}
        return {"tier": "A", "category": "formal", "label": "正式全过·标准档",
                "blockers": [], "metrics": metrics, "eligible": True}

    # ---- 已许可实验类别 ----
    if ENTRY_CFG["allow_experimental"]:
        cats = ENTRY_CFG["experimental_categories"]
        # 回落放宽：<2.0%且主力>10%，或 <3.0%且主力>15%
        if "pullback_relax" in cats and pull is not None and main_pct is not None:
            if (pull < 2.0 and main_pct > 10.0) or (pull < 3.0 and main_pct > 15.0):
                return {"tier": "S", "category": "pullback_relax",
                        "label": "实验·回落放宽档", "blockers": blockers,
                        "metrics": metrics, "eligible": True}
        # 合力主升：生产标签 ✓(合力)
        if "coalition" in cats and "合力" in dom:
            return {"tier": "S", "category": "coalition", "label": "实验·合力主升",
                    "blockers": blockers, "metrics": metrics, "eligible": True}

    return {"tier": None, "category": "none", "label": "未达门槛",
            "blockers": blockers, "metrics": metrics, "eligible": False}


# ══════════════════════════════════════════════════════════════
# 仓位计算
# ══════════════════════════════════════════════════════════════

def calc_lots(price: float, tier: str, acc: Dict[str, Any], window_mult: float = 1.0,
              code: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    """按档位算手数。规则：不足 min_lots 按 min_lots（「1手起步」）；
    跨周减半；单股累计 ≤ 1/4 本金；受可用现金约束。"""
    capital = float(acc["initial_capital"])
    cash = float(acc["cash"])
    if price <= 0:
        return 0, {"reason": "价格无效"}
    tier_cfg = POS_CFG["tiers"].get(tier) or {}
    detail: Dict[str, Any] = {"tier": tier, "price": price}

    if "lots_fixed" in tier_cfg:
        lots = int(tier_cfg["lots_fixed"])
        detail["base"] = f"固定{tier_cfg['lots_fixed']}手"
    else:
        target = capital * float(tier_cfg.get("target_ratio", 0.0))
        lots = int(target // (price * LOT))
        detail["base"] = f"目标{target:.0f}元 → {lots}手"

    if lots < POS_CFG["min_lots"]:
        lots = POS_CFG["min_lots"]
        detail["min_floor"] = True

    if window_mult != 1.0:
        lots = max(POS_CFG["min_lots"], int(lots * window_mult))
        detail["window_mult"] = window_mult

    # 单股上限（含已持有）
    cap_lots = int(capital * POS_CFG["single_stock_cap_ratio"] // (price * LOT))
    held = held_shares(acc, code) if code else 0
    room_lots = max(0, cap_lots - held // LOT)
    if lots > room_lots:
        detail["capped_by_single_stock"] = room_lots
        lots = room_lots

    # 现金约束
    cash_lots = int(cash // (price * LOT))
    if lots > cash_lots:
        detail["capped_by_cash"] = cash_lots
        lots = cash_lots

    lots = max(0, lots)
    detail["lots"] = lots
    detail["shares"] = lots * LOT
    detail["amount"] = round(lots * LOT * price, 2)
    return lots, detail


# ══════════════════════════════════════════════════════════════
# 报告扫描
# ══════════════════════════════════════════════════════════════

def latest_report(date_str: Optional[str] = None) -> Optional[Path]:
    d = date_str or today_str()
    cands = sorted(REPORTS_DIR.glob(f"A股筛选结果_{d}_*.md"))
    if not cands:  # 兼容平铺/子目录两种布局
        cands = sorted(REPORTS_DIR.glob(f"*/A股筛选结果_{d}_*.md"))
    return cands[-1] if cands else None


def report_data_time(rep: Dict[str, Any], path: Path) -> Optional[datetime]:
    """从报告头部取「数据时间」，用于新鲜度校验。"""
    head = (rep.get("meta") or {}).get("data_time")
    if head:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(str(head), fmt)
            except ValueError:
                pass
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:400]
    except Exception:
        return None
    m = re.search(r"数据时间[:：]\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", text)
    if not m:
        return None
    raw = m.group(1).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def build_candidates(path: Path) -> Dict[str, Any]:
    """解析一份报告，产出可开仓清单（已过窗口/新鲜度由调用方判断）。"""
    rep = parse_screening_report(str(path))
    tables = rep.get("tables") or {}
    rows: List[Tuple[Dict[str, Any], str]] = []
    for r in tables.get("low_absorb_short") or []:
        rows.append((r, "short"))
    for r in tables.get("low_absorb_trend") or []:
        rows.append((r, "trend"))

    out: List[Dict[str, Any]] = []
    seen = set()
    for row, kind in rows:
        code = str(row.get("代码") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        res = classify(row, kind)
        if not res["eligible"]:
            continue
        out.append({
            "code": code,
            "tier": res["tier"],
            "category": res["category"],
            "label": res["label"],
            "real_blockers": res["blockers"],
            **{k: v for k, v in res["metrics"].items() if k != "code"},
        })

    # 资金排序（框架第八条）：主力净额 / 5 分钟增量优先
    out.sort(key=lambda x: (-(x.get("main_pct") or 0), -(x.get("inc5_wan") or 0)))
    return {"report": path.name, "data_time": report_data_time(rep, path),
            "candidates": out, "raw": rep}


# ══════════════════════════════════════════════════════════════
# 决策记录同步
# ══════════════════════════════════════════════════════════════

# 标题一律用 MULTILINE 行首锚定位：决策记录的说明文字里会以行内代码形式引用
# `## 收盘持仓快照`，不带锚点的 str.replace / re.search 会命中那处引用并把说明
# 文字劈成两半（2026-09-21 实盘踩到，见当日决策记录修复）。
SNAPSHOT_RE = re.compile(
    r"^##[ \t]*收盘持仓快照[ \t]*\n+```ya?ml[ \t]*\n(.*?)\n```",
    re.DOTALL | re.MULTILINE,
)
TRADES_HEAD_RE = re.compile(r"^##[ \t]*模拟盘成交[ \t]*$", re.MULTILINE)
SNAPSHOT_HEAD_RE = re.compile(r"^##[ \t]*收盘持仓快照[ \t]*$", re.MULTILINE)


def _yaml_str(value: Any) -> str:
    s = str(value if value is not None else "")
    return "'" + s.replace("'", "''") + "'"   # code 必须带引号，否则 002011 会变成 2011


def render_snapshot_yaml(acc: Dict[str, Any]) -> str:
    lines = ["positions:"]
    sim = acc.get("positions") or []
    if sim:
        lines.append("  simulated:")
        for p in sim:
            lines.append(f"    - code: {_yaml_str(p.get('code'))}")
            lines.append(f"      name: {p.get('name', '')}")
            lines.append(f"      qty: {int(p.get('shares', 0))}")
            lines.append(f"      cost: {p.get('cost')}")
            lines.append(f"      stop: {p.get('stop')}")
            lines.append(f"      sector: {_yaml_str(p.get('sector', '-'))}")
            lines.append(f"      buy_date: {_yaml_str(p.get('buy_date'))}")
    else:
        lines.append("  simulated: []")
    lines.append("  real: []")
    lines.append("  real_mother: []")
    lines.append("cash:")
    lines.append("  real_available: null")
    lines.append(f"  sim_available: {round(float(acc.get('cash', 0.0)), 2)}")
    lines.append("watchlist: []")
    lines.append("t1_plan: []")
    return "\n".join(lines)


def sync_decision_record(acc: Dict[str, Any], date_str: Optional[str] = None,
                         note: Optional[str] = None) -> Path:
    """把账本写回当日决策记录：更新 ## 收盘持仓快照，并把成交追加到「模拟盘成交」。

    硬约束：`## 收盘持仓快照` 标题下必须**紧接**代码块，中间不能插文字，
    否则 tools/get_position.py 的正则匹配不到（返回 has_yaml=False）。
    """
    d = date_str or today_str()
    DECISION_DIR.mkdir(parents=True, exist_ok=True)
    path = DECISION_DIR / f"{d}.md"
    if not path.exists():
        path.write_text(
            f"# 决策记录 {d[:4]}-{d[4:6]}-{d[6:]}（自动创建）\n\n"
            "> 持仓与 T+1 状态的唯一事实来源。\n\n"
            "## 模拟盘成交\n\n## 收盘持仓快照\n\n```yaml\n\n```\n",
            encoding="utf-8",
        )

    text = path.read_text(encoding="utf-8")
    block = f"```yaml\n{render_snapshot_yaml(acc)}\n```"

    if SNAPSHOT_RE.search(text):
        text = SNAPSHOT_RE.sub(lambda _m: "## 收盘持仓快照\n\n" + block, text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n## 收盘持仓快照\n\n" + block + "\n"

    if note:
        stamp = now_dt().strftime("%H:%M")
        entry = f"- {stamp} {note}\n"
        m_trade = TRADES_HEAD_RE.search(text)
        if m_trade:
            # 已有「模拟盘成交」段：插到第一条成交之前（最新在上），
            # 段标题下的说明引用行保持不动。
            tail = text[m_trade.end():]
            m_first = re.search(r"^- ", tail, re.MULTILINE)
            if m_first:
                pos = m_trade.end() + m_first.start()
                text = text[:pos] + entry + text[pos:]
            else:
                text = text[:m_trade.end()] + "\n\n" + entry + tail.lstrip("\n")
        else:
            section = f"## 模拟盘成交\n\n{entry}\n"
            m_head = SNAPSHOT_HEAD_RE.search(text)
            if m_head:
                text = text[:m_head.start()] + section + text[m_head.start():]
            else:
                text = text.rstrip("\n") + "\n\n" + section

    path.write_text(text, encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════
# 市值与净值
# ══════════════════════════════════════════════════════════════

def get_prices(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    if not codes:
        return {}
    try:
        return fetch_realtime_quotes(codes) or {}
    except Exception as exc:
        print(f"⚠️ 行情获取失败（将退化为成本价估值）：{exc}", file=sys.stderr)
        return {}


def mark_to_market(acc: Dict[str, Any]) -> Dict[str, Any]:
    """按最新价估值。取不到价时退回成本价，并标记 stale。"""
    codes = [str(p.get("code")) for p in acc.get("positions") or []]
    quotes = get_prices(codes)
    rows, mv = [], 0.0
    stale = []
    for p in acc.get("positions") or []:
        code = str(p.get("code"))
        q = quotes.get(code) or {}
        price = q.get("price") or 0.0
        cost = float(p.get("cost") or 0.0)
        shares = int(p.get("shares") or 0)
        if not price:
            price, stale_flag = cost, True
        else:
            stale_flag = False
        if stale_flag:
            stale.append(code)
        value = price * shares
        mv += value
        rows.append({
            "code": code, "name": p.get("name"), "shares": shares, "cost": cost,
            "price": price, "value": round(value, 2),
            "pnl": round((price - cost) * shares, 2),
            "pnl_pct": round((price / cost - 1) * 100, 2) if cost else 0.0,
            "tier": p.get("tier"), "buy_date": p.get("buy_date"), "stale": stale_flag,
        })
    cash = float(acc.get("cash", 0.0))
    return {"rows": rows, "cash": cash, "market_value": round(mv, 2),
            "nav": round(cash + mv, 2), "stale_codes": stale}


def realized_pnl(acc: Dict[str, Any]) -> float:
    return round(sum(float(c.get("pnl") or 0.0) for c in acc.get("closed") or []), 2)


# ══════════════════════════════════════════════════════════════
# 命令实现
# ══════════════════════════════════════════════════════════════

def cmd_status(args: argparse.Namespace) -> int:
    acc = load_account()
    mtm = mark_to_market(acc)
    init = float(acc["initial_capital"])
    total = mtm["nav"] + realized_pnl(acc)
    if args.json:
        print(json.dumps({**mtm, "initial_capital": init, "realized_pnl": realized_pnl(acc),
                          "total_nav": round(total, 2)}, ensure_ascii=False, indent=2))
        return 0
    print(f"=== 模拟盘账户（本金 {init:,.0f} 元，创建于 {acc.get('created')}）===")
    float_pnl = round(sum(r["pnl"] for r in mtm["rows"]), 2)
    print(f"可用现金   {mtm['cash']:>12,.2f}")
    print(f"持仓市值   {mtm['market_value']:>12,.2f}")
    print(f"浮动盈亏   {float_pnl:>12,.2f}")
    print(f"账户净值   {mtm['nav']:>12,.2f}")
    print(f"已实现盈亏 {realized_pnl(acc):>12,.2f}")
    print(f"总权益     {total:>12,.2f}   收益率 {(total / init - 1) * 100:+.2f}%")
    if mtm["stale_codes"]:
        print(f"⚠️ 以下标的取不到实时价，已按成本价估值：{', '.join(mtm['stale_codes'])}")
    print(f"\n【持仓 {len(mtm['rows'])} 只】")
    if not mtm["rows"]:
        print("  (空仓)")
    for r in mtm["rows"]:
        print(f"  {r['code']} {r['name']} | {r['shares']}股 @{r['cost']} → {r['price']} | "
              f"浮盈 {r['pnl']:+,.2f} ({r['pnl_pct']:+.2f}%) | 档位{r['tier']} | 买入{_fmt_date(r['buy_date'])}")
    closed = acc.get("closed") or []
    print(f"\n【已平仓 {len(closed)} 笔】")
    if not closed:
        print("  (无)")
    for c in closed[-10:]:
        print(f"  {c.get('code')} {c.get('name')} | {c.get('shares')}股 {c.get('cost')}→{c.get('exit_price')} | "
              f"{float(c.get('pnl') or 0):+,.2f} ({float(c.get('pnl_pct') or 0):+.2f}%) | {c.get('exit_reason')} | {_fmt_date(c.get('exit_date'))}")
    return 0


def _fmt_date(value: Any) -> str:
    s = str(value or "-")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else s


def cmd_scan(args: argparse.Namespace) -> int:
    path = Path(args.report) if args.report else latest_report(args.date)
    if not path or not path.exists():
        print(f"未找到筛选报告（date={args.date or today_str()}）。")
        return 1
    built = build_candidates(path)
    now = now_dt()
    allowed, mult, wname = entry_window(now)
    if args.ignore_window:
        allowed, mult, wname = True, mult or 1.0, wname + "(已忽略窗口限制)"

    age_min = None
    if built["data_time"]:
        age_min = round((now - built["data_time"]).total_seconds() / 60, 1)
    fresh = age_min is None or age_min <= args.max_age_min

    acc = load_account()
    cands = []
    for c in built["candidates"]:
        if held_shares(acc, c["code"]):
            continue  # 框架第17行：绝不补仓
        price = None
        q = get_prices([c["code"]]).get(c["code"]) or {}
        price = q.get("price") or None
        if not price:
            try:
                price = float(str(c.get("price_raw")).replace(",", ""))
            except (TypeError, ValueError):
                price = None
        if not price:
            c["skip"] = "取不到价格"
            cands.append(c)
            continue
        lots, detail = calc_lots(float(price), c["tier"], acc, mult)
        c.update({"price": price, "lots": lots, "shares": lots * LOT,
                  "amount": detail.get("amount", 0.0), "sizing": detail,
                  "stop": round(price * (1 + EXIT_CFG["stop_loss_default_pct"] / 100), 2),
                  "target": round(price * (1 + EXIT_CFG["take_profit_pct"] / 100), 2)})
        if lots <= 0:
            c["skip"] = "现金或单股上限不足"
        cands.append(c)

    # 单日新开上限
    remaining = max(0, POS_CFG["max_new_positions_per_day"] -
                    len([p for p in acc.get("positions") or [] if str(p.get("buy_date")) == today_str()]))
    actionable = [c for c in cands if not c.get("skip")][:remaining]

    if args.json:
        print(json.dumps({"report": built["report"], "data_time": str(built["data_time"]),
                          "window": wname, "window_allows_entry": allowed,
                          "window_multiplier": mult, "data_age_minutes": age_min,
                          "data_fresh": fresh, "remaining_slots": remaining,
                          "actionable": actionable, "all": cands},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"=== 模拟盘开仓扫描 ===")
    print(f"报告       {built['report']}")
    print(f"数据时间   {built['data_time'] or '未解析到'}"
          + (f"（{age_min} 分钟前）" if age_min is not None else ""))
    print(f"执行窗口   {wname}｜允许开仓：{'是' if allowed else '否'}"
          + (f"｜仓位倍数 {mult}" if mult != 1.0 else ""))
    print(f"今日名额   剩余 {remaining} 个（上限 {POS_CFG['max_new_positions_per_day']}）")
    if not fresh:
        print(f"⚠️ 数据超过 {args.max_age_min} 分钟，陈旧快照不可用于开仓（框架：数据缺失/陈旧则暂不开仓）")
        allowed = False
    print("\n【可开仓】" if actionable else "\n【可开仓】(无)")
    for c in actionable:
        print(f"  {c['code']} {c['name']} | 档位{c['tier']}·{c['label']} | 现价 {c['price']} × {c['shares']}股 = {c['amount']:,.0f}元"
              f" | 止损 {c['stop']} 止盈 {c['target']} | 主力{c.get('main_pct')}% 5分{c.get('inc5_wan')}万 回落{c.get('pullback_pct')}%"
              f" | {c.get('sector')}")
        if c.get("real_blockers"):
            print(f"      真实仓卡点：{'；'.join(c['real_blockers'])}")
    blocked = [c for c in cands if c.get("skip")]
    if blocked:
        print("\n【已排除】")
        for c in blocked:
            print(f"  {c['code']} {c['name']} | {c['skip']}")
    if not cands:
        print("\n报告内无可判定的低吸候选（可能已全部被否决或表格为空）。")
    return 0 if (allowed and fresh) else 0


def cmd_open(args: argparse.Namespace) -> int:
    acc = load_account()
    code = str(args.code).strip()
    if held_shares(acc, code) and ENTRY_CFG["no_averaging_down"]:
        print(f"{code} 已在持仓中；框架第17行「绝不补仓」，拒绝加仓。")
        return 1

    allowed, mult, wname = entry_window(now_dt())
    if not allowed and not args.force:
        print(f"当前窗口「{wname}」不允许开新仓（14:20 后禁新仓／观察期禁买）。"
              f"如确认要记账，加 --force。")
        return 1

    tier = args.tier
    category, label, blockers = "manual", "手动指定", []
    auto_sector, auto_report = None, None
    if tier:
        # 调用方（如 auto）已判档：直接采用其结论，避免二次判档因报告刷新
        # 而得出不同档位，导致"打印的档位"与"实际成交"不一致。
        category = getattr(args, "category", None) or "manual"
        label = getattr(args, "label", None) or "手动指定档"
        blockers = [b for b in str(getattr(args, "blockers", "") or "").split("|") if b]
    else:
        path = Path(args.report) if args.report else latest_report(args.date)
        if not path or not path.exists():
            print("未找到报告，无法自动判档；请用 --tier 指定档位。")
            return 1
        built = build_candidates(path)
        hit = next((c for c in built["candidates"] if c["code"] == code), None)
        if not hit:
            print(f"{code} 未出现在报告的可开仓清单中，拒绝开仓（避免绕过门槛）。"
                  f"\n如需强制记账请显式加 --tier 与 --category。")
            return 1
        tier, category, label = hit["tier"], hit["category"], hit["label"]
        blockers = hit.get("real_blockers") or []
        auto_sector = hit.get("sector")
        auto_report = path.name

    price = args.price
    if not price:
        q = get_prices([code]).get(code) or {}
        price = q.get("price")
    if not price:
        print(f"{code} 取不到实时价，请用 --price 指定。")
        return 1
    price = float(price)

    lots, detail = calc_lots(price, tier, acc, mult, code)
    if args.lots:
        lots = int(args.lots)
    if lots <= 0:
        print(f"按档位 {tier} 算出可买 0 手（现金 {acc['cash']:.2f} 或单股上限不足）。")
        return 1
    shares = lots * LOT
    amount = round(shares * price, 2)

    q = get_prices([code]).get(code) or {}
    name = args.name or q.get("name") or code
    pos = {
        "code": code, "name": name, "shares": shares, "cost": price, "amount": amount,
        "buy_date": today_str(), "buy_time": now_dt().strftime("%H:%M"),
        "tier": tier, "category": category, "label": label,
        "real_blockers": blockers,
        "stop": round(price * (1 + EXIT_CFG["stop_loss_default_pct"] / 100), 2),
        "target": round(price * (1 + EXIT_CFG["take_profit_pct"] / 100), 2),
        "sector": args.sector or auto_sector or "-",
        "report_file": args.report or auto_report or "",
        "window": wname, "status": "open",
    }
    if args.dry_run:
        print(json.dumps(pos, ensure_ascii=False, indent=2))
        print("（--dry-run，未写入账本）")
        return 0

    acc["positions"].append(pos)
    acc["cash"] = round(float(acc["cash"]) - amount, 2)
    save_account(acc)
    note = (f"买入 {code} {name} {shares}股 @{price} = {amount:,.0f}元 | 档位{tier}·{label} | "
            f"止损 {pos['stop']} 止盈 {pos['target']} | 窗口 {wname}")
    p = sync_decision_record(acc, note=note)
    print(f"✅ 已开仓：{code} {name} {shares}股 @{price}（{amount:,.0f}元，档位 {tier}·{label}）")
    print(f"   剩余现金 {acc['cash']:,.2f} | 止损 {pos['stop']} | 止盈 {pos['target']}")
    print(f"   已同步 {p.name}")
    if blockers:
        print(f"   真实仓卡点：{'；'.join(blockers)}")
    return 0


def _exit_rows(acc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """逐仓判定离场动作：止损 / 止盈 / T+1 窗口截止。"""
    mtm = mark_to_market(acc)
    today = today_str()
    deadline_passed = past_t1_deadline()
    rows: List[Dict[str, Any]] = []
    for r in mtm["rows"]:
        pos = find_position(acc, r["code"]) or {}
        buy_date = str(pos.get("buy_date") or "")
        stop = float(pos.get("stop") or r["cost"] * (1 + EXIT_CFG["stop_loss_default_pct"] / 100))
        target = float(pos.get("target") or r["cost"] * (1 + EXIT_CFG["take_profit_pct"] / 100))
        base = {"stop": stop, "target": target}
        if buy_date == today and EXIT_CFG["t1_no_sell_on_buy_day"]:
            rows.append({**r, **base, "action": "hold", "reason": "T+1 当日不可卖"})
        elif r["stale"]:
            rows.append({**r, **base, "action": "hold", "reason": "无实时价，无法判定"})
        elif r["price"] <= stop:
            rows.append({**r, **base, "action": "sell", "reason": f"止损（{r['price']}≤{stop}）"})
        elif r["price"] >= target:
            rows.append({**r, **base, "action": "sell", "reason": f"止盈（{r['price']}≥{target}）"})
        elif deadline_passed:
            rows.append({**r, **base, "action": "sell",
                         "reason": f"{EXIT_CFG['t1_exit_deadline']} 常规退出截止"})
        else:
            rows.append({**r, **base, "action": "hold", "reason": "持有至窗口内择机退出"})
    return rows


def cmd_check_exit(args: argparse.Namespace) -> int:
    acc = load_account()
    if not (acc.get("positions") or []):
        print("无持仓。")
        return 0
    rows = _exit_rows(acc)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    now = now_dt()
    print(f"=== 持仓离场检查（{now.strftime('%Y-%m-%d %H:%M')}）===")
    print(f"T+1 卖出窗口 09:30–{EXIT_CFG['t1_exit_deadline']}｜当前{'已过截止' if past_t1_deadline(now) else '未到截止'}")
    for r in rows:
        mark = "🔴卖出" if r["action"] == "sell" else "⚪持有"
        print(f"  {mark} {r['code']} {r['name']} | {r['price']} (成本{r['cost']}) "
              f"浮盈{r['pnl']:+,.2f} | 止损{r['stop']} 止盈{r['target']} | {r['reason']}")
    print(f"\n应卖出 {len([r for r in rows if r['action'] == 'sell'])} 只。")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    acc = load_account()
    pos = find_position(acc, args.code)
    if not pos:
        print(f"{args.code} 不在持仓中。")
        return 1
    if str(pos.get("buy_date")) == today_str() and EXIT_CFG["t1_no_sell_on_buy_day"] and not args.force:
        print(f"{args.code} 为当日买入，T+1 不可卖出（如确认按纪律外操作记账，加 --force）。")
        return 1
    price = args.price
    if not price:
        q = get_prices([args.code]).get(args.code) or {}
        price = q.get("price")
    if not price:
        print(f"{args.code} 取不到实时价，请用 --price 指定。")
        return 1
    price = float(price)
    shares = int(pos["shares"])
    cost = float(pos["cost"])
    proceeds = round(price * shares, 2)
    pnl = round((price - cost) * shares, 2)
    pnl_pct = round((price / cost - 1) * 100, 2) if cost else 0.0
    record = {
        **pos, "exit_price": price, "exit_date": today_str(),
        "exit_time": now_dt().strftime("%H:%M"), "exit_reason": args.reason,
        "proceeds": proceeds, "pnl": pnl, "pnl_pct": pnl_pct, "status": "closed",
    }
    if args.dry_run:
        print(json.dumps(record, ensure_ascii=False, indent=2))
        print("（--dry-run，未写入账本）")
        return 0
    acc["positions"] = [p for p in acc["positions"] if str(p.get("code")) != str(args.code)]
    acc["closed"].append(record)
    acc["cash"] = round(float(acc["cash"]) + proceeds, 2)
    save_account(acc)
    note = (f"卖出 {args.code} {pos.get('name')} {shares}股 @{price} | 盈亏 {pnl:+,.2f} ({pnl_pct:+.2f}%) | "
            f"原因 {args.reason}")
    p = sync_decision_record(acc, note=note)
    print(f"✅ 已平仓：{args.code} {pos.get('name')} {shares}股 @{price} → {pnl:+,.2f} ({pnl_pct:+.2f}%)")
    print(f"   原因 {args.reason} | 可用现金 {acc['cash']:,.2f} | 已同步 {p.name}")
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    acc = load_account()
    mtm = mark_to_market(acc)
    d = args.date or today_str()
    entry = {"date": d, "ts": now_dt().strftime("%Y-%m-%d %H:%M:%S"),
             "cash": mtm["cash"], "market_value": mtm["market_value"],
             "nav": mtm["nav"], "positions": len(mtm["rows"]),
             "realized_pnl": realized_pnl(acc), "stale": mtm["stale_codes"]}
    hist = [h for h in acc.get("nav_history") or [] if str(h.get("date")) != d]
    hist.append(entry)
    acc["nav_history"] = sorted(hist, key=lambda x: str(x.get("date")))
    acc["meta"]["last_settle"] = entry["ts"]
    acc["meta"]["settle_count"] = int(acc["meta"].get("settle_count") or 0) + 1
    save_account(acc)
    p = sync_decision_record(acc, date_str=d)
    init = float(acc["initial_capital"])
    total = mtm["nav"] + realized_pnl(acc)
    print(f"✅ 已结算 {d}：净值 {total:,.2f}（{total / init - 1:+.2f}%）"
          f"｜现金 {mtm['cash']:,.2f}｜市值 {mtm['market_value']:,.2f}｜持仓 {len(mtm['rows'])} 只")
    if mtm["stale_codes"]:
        print(f"   ⚠️ 按成本价估值：{', '.join(mtm['stale_codes'])}")
    print(f"   已同步 {p.name}")
    return 0


def cmd_nav(args: argparse.Namespace) -> int:
    acc = load_account()
    hist = acc.get("nav_history") or []
    init = float(acc["initial_capital"])
    if not hist:
        print("尚无净值记录（先跑 settle）。")
        return 0
    print(f"=== 净值曲线（初始本金 {init:,.0f}）===")
    print(f"{'日期':<12}{'净值':>12}{'收益':>10}{'市值':>12}{'现金':>12}{'持仓':>6}")
    for h in hist:
        nav = float(h.get("nav") or 0) + float(h.get("realized_pnl") or 0)
        print(f"{_fmt_date(h.get('date')):<12}{nav:>12,.2f}{(nav / init - 1) * 100:>9.2f}%"
              f"{float(h.get('market_value') or 0):>12,.2f}{float(h.get('cash') or 0):>12,.2f}"
              f"{int(h.get('positions') or 0):>6}")
    first, last = hist[0], hist[-1]
    n0 = float(first.get("nav") or 0) + float(first.get("realized_pnl") or 0)
    n1 = float(last.get("nav") or 0) + float(last.get("realized_pnl") or 0)
    print(f"\n期初 {n0:,.2f} → 最新 {n1:,.2f}，区间 {n1 - n0:+,.2f}（{(n1 / n0 - 1) * 100 if n0 else 0:+.2f}%）")
    closed = acc.get("closed") or []
    wins = [c for c in closed if float(c.get("pnl") or 0) > 0]
    print(f"已平仓 {len(closed)} 笔，胜率 {len(wins) / len(closed) * 100:.1f}%" if closed else "尚无平仓样本")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    acc = load_account()
    p = sync_decision_record(acc, date_str=args.date)
    print(f"✅ 已同步账本到 {p}")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    """按当前时间自动执行对应阶段，供定时任务调用。

    所有判定复用本模块其它命令，**不接受外部参数覆盖仓位或门槛**，
    避免语言模型侧自由裁量导致结果不可复现。
    """
    dt = now_dt()
    print(f"=== 模拟盘自动裁决 {dt.strftime('%Y-%m-%d %H:%M')} ===")
    if dt.weekday() >= 5:
        print("非交易日，无动作。")
        return 0
    t = dt.hour * 60 + dt.minute

    # ---- 盘前：只读准备 ----
    if t < 9 * 60 + 30:
        print("[阶段] 盘前准备")
        return cmd_status(argparse.Namespace(json=False))

    # ---- 离场窗口 09:30–09:50（框架 09:45 为常规退出截止） ----
    if t <= 9 * 60 + 50:
        print("[阶段] T+1 离场窗口")
        acc = load_account()
        if not (acc.get("positions") or []):
            print("无持仓，无动作。")
            return 0
        rows = _exit_rows(acc)
        for r in rows:
            print(f"  {'🔴卖出' if r['action'] == 'sell' else '⚪持有'} "
                  f"{r['code']} {r['name']} | {r['reason']}")
        sells = [r for r in rows if r["action"] == "sell"]
        if not sells:
            print("无需卖出。")
            return 0
        for r in sells:
            if args.dry_run:
                print(f"  [dry-run] 将平仓 {r['code']}（{r['reason']}）")
                continue
            cmd_close(argparse.Namespace(code=r["code"], price=None, reason=r["reason"],
                                         force=False, dry_run=False))
        return 0

    # ---- 日终结算 ----
    if t >= 15 * 60:
        print("[阶段] 日终结算")
        return cmd_settle(argparse.Namespace(date=None))

    # ---- 买入窗口 09:50–14:20 ----
    if 9 * 60 + 50 < t < 14 * 60 + 20:
        print("[阶段] 买入裁决")
        path = latest_report()
        if not path:
            print("未找到当日报告，无动作。")
            return 0
        allowed, mult, wname = entry_window(dt)
        print(f"  窗口 {wname}｜允许开仓 {'是' if allowed else '否'}"
              + (f"｜仓位倍数 {mult}" if mult != 1.0 else ""))
        if not allowed:
            print("  窗口不允许开仓，无动作。")
            return 0
        built = build_candidates(path)
        age = None
        if built["data_time"]:
            age = (dt - built["data_time"]).total_seconds() / 60
            print(f"  报告 {built['report']}｜数据时间 {built['data_time']}（{age:.1f} 分钟前）")
        if age is not None and age > 15:
            print("  数据陈旧（>15 分钟），按框架「数据缺失/陈旧则暂不开仓」处理。")
            return 0
        acc = load_account()
        opened = 0
        for c in built["candidates"]:
            if opened >= POS_CFG["max_new_positions_per_day"]:
                break
            if held_shares(acc, c["code"]):
                continue  # 绝不补仓
            price = (get_prices([c["code"]]).get(c["code"]) or {}).get("price")
            if not price:
                print(f"  {c['code']} {c.get('name')} 取不到实时价，跳过")
                continue
            lots, _detail = calc_lots(float(price), c["tier"], acc, mult, c["code"])
            if lots <= 0:
                print(f"  {c['code']} {c.get('name')} 资金或单股上限不足，跳过")
                continue
            if args.dry_run:
                print(f"  [dry-run] 将开仓 {c['code']} {c.get('name')} 档位{c['tier']}·{c['label']} {lots}手 @{price}")
                opened += 1
                continue
            rc = cmd_open(argparse.Namespace(
                code=c["code"], tier=c["tier"], price=price, lots=lots,
                name=c.get("name"), sector=c.get("sector"), report=str(path), date=None,
                force=False, dry_run=False,
                category=c.get("category"), label=c.get("label"),
                blockers="|".join(c.get("real_blockers") or []),
            ))
            if rc == 0:
                opened += 1
            acc = load_account()  # 现金已变化
        print(f"\n本轮开仓 {opened} 只。")
        return 0

    # ---- 14:20–15:00 尾盘持仓管理 ----
    print("[阶段] 尾盘持仓管理（14:20 后禁新仓）")
    acc = load_account()
    if not (acc.get("positions") or []):
        print("无持仓，无动作。")
        return 0
    for r in _exit_rows(acc):
        print(f"  {r['code']} {r['name']} | 浮盈{r['pnl']:+,.2f} | {r['reason']}")
    return 0


# ══════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="资金约束模拟盘（10万本金）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="账户状态")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("scan", help="扫报告出可开仓清单")
    p.add_argument("--report", help="指定报告路径")
    p.add_argument("--date", help="日期 YYYYMMDD")
    p.add_argument("--max-age-min", type=int, default=15, help="数据新鲜度上限（分钟）")
    p.add_argument("--ignore-window", action="store_true", help="忽略执行窗口限制")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("open", help="开仓")
    p.add_argument("--code", required=True)
    p.add_argument("--tier", choices=sorted(POS_CFG["tiers"].keys()))
    p.add_argument("--category", help="内部使用：调用方已判定的实验类别")
    p.add_argument("--label", help="内部使用：调用方已判定的档位说明")
    p.add_argument("--blockers", help="内部使用：真实仓卡点，用 | 分隔")
    p.add_argument("--price", type=float)
    p.add_argument("--lots", type=int)
    p.add_argument("--name")
    p.add_argument("--sector")
    p.add_argument("--report")
    p.add_argument("--date")
    p.add_argument("--force", action="store_true", help="忽略窗口限制")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("check-exit", help="检查离场")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check_exit)

    p = sub.add_parser("close", help="平仓")
    p.add_argument("--code", required=True)
    p.add_argument("--price", type=float)
    p.add_argument("--reason", default="手动平仓")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("settle", help="日终结算")
    p.add_argument("--date")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("nav", help="净值曲线")
    p.set_defaults(func=cmd_nav)

    p = sub.add_parser("sync", help="同步到决策记录")
    p.add_argument("--date")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("auto", help="按当前时段自动裁决（供定时任务调用）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_auto)

    args = ap.parse_args()
    if not SIM.get("enabled", True):
        print("模拟盘已在 rule_config.sim.enabled 中关闭。")
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
