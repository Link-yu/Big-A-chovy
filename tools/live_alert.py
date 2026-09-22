#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘定时提醒引擎。

按 `docs/实盘操作时间节点.md` 的时刻表，在**需要鱼泡泡动手**的时候发钉钉。

设计原则（与项目一致）：
- **全部判定在本文件内完成**，automation 只负责按时唤醒；不依赖 LLM 判断，结果可复现、不耗 token。
- **只在需要操作时发**：窗口开启必发一次，窗口内的后续时点仅在出现正式候选时发。
- 提醒里出现的候选**只是报告层面门槛全过**，基本面盈利与分笔五档尚未核验，
  所以措辞永远是「去发 ggp 核验」，不得写成「可买」。

用法：
  python tools/live_alert.py auto     # automation 用：按当前时间决定并发送
  python tools/live_alert.py check    # 只打印不发（诊断）
  python tools/live_alert.py test     # 发一条测试钉钉
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tools.get_position import get_latest_decision_file, load_position_snapshot  # noqa: E402
from tools.sim_account import (  # noqa: E402
    build_candidates,
    entry_window,
    latest_report,
    now_dt,
    today_str,
)

CONFIG_FILE = BASE_DIR / "tools" / "alert_config.json"
STALE_MINUTES = 10

# ── 时点表 ─────────────────────────────────────────────────────
# (phase, 起 HH:MM, 止 HH:MM, 无内容时是否仍发, 标题)
# 边界比 automation 时点放宽约 ±10 分钟，容忍调度晚点。
PHASES: List[Tuple[str, str, str, bool, str]] = [
    ("pre_open", "09:18", "09:38", False, "竞价与观察期"),
    ("main_open", "09:45", "10:05", True, "主买窗口开启"),
    ("main_mid", "10:08", "10:30", False, "主买窗口中段"),
    ("main_late", "10:33", "10:52", False, "主买窗口末段"),
    ("pm_open", "12:55", "13:12", True, "午后回流·仓位减半"),
    ("late_open", "13:38", "13:58", True, "午后尾段"),
    ("hold_only", "14:12", "14:32", True, "停止开仓·转持仓监控"),
    ("closed", "14:52", "15:40", True, "收盘复盘"),
]

BUY_PHASES = {"main_open", "main_mid", "main_late", "pm_open", "late_open"}


# ══════════════════════════════════════════════════════════════
# 基础
# ══════════════════════════════════════════════════════════════

def _hhmm(dt: datetime) -> int:
    return dt.hour * 100 + dt.minute


def pick_phase(dt: datetime) -> Optional[Tuple[str, bool, str]]:
    """返回 (phase, 无内容时是否仍发, 标题)；不在任何窗口内返回 None。"""
    now = _hhmm(dt)
    for phase, start, end, force, title in PHASES:
        s = int(start[:2]) * 100 + int(start[3:])
        e = int(end[:2]) * 100 + int(end[3:])
        if s <= now <= e:
            return phase, force, title
    return None


def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise SystemExit(f"缺少配置文件：{CONFIG_FILE}")
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"配置文件损坏：{CONFIG_FILE}\n{exc}") from exc


def send_dingtalk(cfg: Dict[str, Any], content: str) -> Dict[str, Any]:
    """发送文本消息。

    钉钉机器人若启用了「自定义关键词」安全设置，消息必须包含该词才放行
    （否则报 errcode 310000）。关键词来自配置，追加在消息尾部而不是开头，
    避免把「每日复盘」这类旧关键词顶到实盘提醒的标题位置。
    """
    keyword = str(cfg.get("keyword") or "").strip()
    if keyword and keyword not in content:
        content = f"{content}\n（通道标识：{keyword}）"
    payload: Dict[str, Any] = {"msgtype": "text", "text": {"content": content}}
    mobiles = cfg.get("at_mobiles") or []
    if mobiles:
        payload["at"] = {"atMobiles": list(mobiles), "isAtAll": False}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        cfg["dingtalk_webhook"], data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ══════════════════════════════════════════════════════════════
# 数据
# ══════════════════════════════════════════════════════════════

def load_positions() -> Dict[str, Any]:
    fpath = get_latest_decision_file(today_str())
    if not fpath:
        fpath = get_latest_decision_file()
    if not fpath:
        return {"ok": False, "real": [], "real_mother": [], "t1_plan": [], "file": None}
    try:
        snap = load_position_snapshot(fpath)
    except Exception:  # noqa: BLE001
        return {"ok": False, "real": [], "real_mother": [], "t1_plan": [], "file": None}
    pos = snap.get("positions") or {}
    return {
        "ok": bool(snap.get("has_yaml")),
        "file": snap.get("file"),
        "simulated": pos.get("simulated") or [],
        "real": pos.get("real") or [],
        "real_mother": pos.get("real_mother") or [],
        "t1_plan": snap.get("t1_plan") or [],
        "watchlist": snap.get("watchlist") or [],
    }


def scan_candidates() -> Dict[str, Any]:
    """读今日最新报告，分出正式候选（A/B）与实验候选（S）。"""
    path = latest_report()
    if not path:
        return {"ok": False, "reason": "今日暂无筛选报告（看板未运行？）",
                "real": [], "sim": [], "name": None, "stale": True, "data_time": "?"}
    try:
        res = build_candidates(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"报告解析失败：{exc}",
                "real": [], "sim": [], "name": path.name, "stale": True, "data_time": "?"}

    dtime = res.get("data_time")
    stale = True
    label = "?"
    if isinstance(dtime, datetime):
        stale = (now_dt() - dtime).total_seconds() > STALE_MINUTES * 60
        label = dtime.strftime("%H:%M")
    cands = res.get("candidates") or []
    return {
        "ok": True,
        "name": res.get("report"),
        "stale": stale,
        "data_time": label,
        "real": [c for c in cands if c.get("tier") in ("A", "B")],
        "sim": [c for c in cands if c.get("tier") == "S"],
    }


# ══════════════════════════════════════════════════════════════
# 消息组装
# ══════════════════════════════════════════════════════════════

def _fmt_candidate(c: Dict[str, Any]) -> str:
    mp = c.get("main_pct")
    i5 = c.get("inc5_wan")
    bits = [f"{c.get('code')} {c.get('name') or ''}".strip(), f"{c.get('tier')}档"]
    if mp is not None:
        bits.append(f"主力{mp:+.1f}%")
    if i5 is not None:
        bits.append(f"5分{i5:+.0f}万")
    bits.append(str(c.get("sector") or "-"))
    if c.get("price_raw"):
        bits.append(f"现价{c.get('price_raw')}")
    return " | ".join(bits)


def _header(phase_title: str, dt: datetime) -> List[str]:
    return [f"【实盘·{phase_title}】{dt.strftime('%H:%M')}"]


def _short_report(name: Optional[str]) -> str:
    if not name:
        return "?"
    m = re.search(r"_(\d{8})_(\d{4})\.md$", name)
    return m.group(2) if m else name


def _data_line(scan: Dict[str, Any]) -> str:
    if not scan.get("ok"):
        return f"⚠️ {scan.get('reason')}"
    stale = "⚠️ 数据已超10分钟，先补查实时行情" if scan.get("stale") else "数据新鲜"
    return f"报告 {_short_report(scan.get('name'))} · 数据 {scan.get('data_time')} · {stale}"


def build_message(phase: str, title: str, dt: datetime,
                  scan: Dict[str, Any], pos: Dict[str, Any]) -> Optional[str]:
    """按 phase 组装消息；返回 None 表示无需打扰。"""
    lines: List[str] = []

    if phase == "pre_open":
        holds = (pos.get("real") or []) + (pos.get("real_mother") or [])
        t1 = pos.get("t1_plan") or []
        if not holds and not t1:
            return None
        lines += _header("竞价与观察期", dt)
        lines.append(_data_line(scan))
        if holds:
            lines.append("")
            lines.append(f"【持仓 {len(holds)} 只】09:25 看开盘价：")
            for p in holds:
                lines.append(f"  {p.get('code')} {p.get('name') or ''} "
                             f"{p.get('qty')}股 @{p.get('cost')} 止损{p.get('stop')}")
            lines.append("→ 开盘价低于止损线，或已退潮且开盘未回暖：开盘立即出，不等 09:45")
        if t1:
            lines.append("")
            lines.append("【T+1 预案】")
            for t in t1:
                lines.append(f"  {t.get('code')} [{t.get('priority', '-')}] "
                             f"{t.get('action')} ← {t.get('condition')}")
        lines.append("")
        lines.append("09:30 起发 ggp 核对在盯资金是否延续；09:50 前禁买。")
        return "\n".join(lines)

    if phase in BUY_PHASES:
        allowed, mult, win = entry_window(dt)
        real = scan.get("real") or []
        sim = scan.get("sim") or []
        mandatory = PHASES_BY_NAME[phase][3]
        if not allowed:
            if not mandatory:
                return None
            lines += _header(title, dt)
            lines.append(f"当前窗口「{win}」不允许新开仓，仅持仓监控。")
            return "\n".join(lines)
        if not mandatory and not real:
            return None
        lines += _header(title, dt)
        lines.append(_data_line(scan))
        if mult < 1.0:
            lines.append("⚠️ 本窗口买入仓位减半")
        if real:
            lines.append("")
            lines.append(f"⚠️ 报告层面正式门槛全过 {len(real)} 只"
                         "（基本面盈利与分笔五档未核验，**不等于可买**）：")
            for c in real[:6]:
                lines.append(f"  {_fmt_candidate(c)}")
        if sim:
            lines.append("")
            lines.append(f"实验候选 {len(sim)} 只（仅模拟盘自动处理，无需你操作）")
        if not real:
            lines.append("")
            lines.append("无正式门槛全过标的 · 无需操作")
        else:
            lines.append("")
            lines.append("→ 发 ggp 做七项支撑核验；通过后按买点区间手动挂单，止损止盈一并定。")
        return "\n".join(lines)

    if phase == "hold_only":
        lines += _header(title, dt)
        lines.append(_data_line(scan))
        holds = (pos.get("real") or []) + (pos.get("real_mother") or [])
        lines.append("")
        lines.append("14:20 起停止新增开仓，转入持仓与板块退潮监控。")
        if holds:
            lines.append(f"在持 {len(holds)} 只，盯止损与板块入池数变化。")
        else:
            lines.append("当前无真实仓持仓。")
        return "\n".join(lines)

    if phase == "closed":
        holds = (pos.get("real") or []) + (pos.get("real_mother") or [])
        sim = pos.get("simulated") or [] if isinstance(pos.get("simulated"), list) else []
        lines += _header(title, dt)
        lines.append(_data_line(scan))
        lines.append("")
        lines.append(f"收盘：真实仓 {len(holds)} 只 · 模拟仓 {len(sim)} 只")
        lines.append("→ 发「复盘」，更新持仓快照与次日 T+1 计划。")
        return "\n".join(lines)

    return None


PHASES_BY_NAME: Dict[str, Tuple[str, str, str, bool, str]] = {p[0]: p for p in PHASES}


# ══════════════════════════════════════════════════════════════
# 命令
# ══════════════════════════════════════════════════════════════

def cmd_auto(args: argparse.Namespace) -> int:
    dt = now_dt()
    if getattr(args, "at", None):
        try:
            hh, mm = str(args.at).split(":")
            dt = dt.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except Exception:  # noqa: BLE001
            print(f"⚠️ --at 格式应为 HH:MM，收到 {args.at}", file=sys.stderr)
            return 2
    print(f"=== 实盘提醒 {dt.strftime('%Y-%m-%d %H:%M')} ===")
    if dt.weekday() >= 5:
        print("非交易日，无动作。")
        return 0

    picked = pick_phase(dt)
    if not picked:
        print("当前不在任何提醒窗口内，静默。")
        return 0
    phase, _force, title = picked
    print(f"命中阶段：{phase}（{title}）")

    scan = scan_candidates()
    pos = load_positions()
    print(f"候选：正式 {len(scan.get('real') or [])} 只 / 实验 {len(scan.get('sim') or [])} 只 · 报告 {scan.get('name')}")
    print(f"持仓：真实 {len(pos.get('real') or [])} 只 / 解套 {len(pos.get('real_mother') or [])} 只 / T+1计划 {len(pos.get('t1_plan') or [])} 条")

    msg = build_message(phase, title, dt, scan, pos)
    if msg is None:
        print("本时点无需你操作，不发送通知。")
        return 0

    print("\n--- 待发消息 ---")
    print(msg)
    print("----------------\n")

    if args.dry_run:
        print("dry-run：未发送。")
        return 0

    cfg = load_config()
    if not cfg.get("enabled", True):
        print("钉钉推送已在配置中关闭，未发送。")
        return 0
    try:
        result = send_dingtalk(cfg, msg)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 发送异常：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    if result.get("errcode") == 0:
        print("✅ 钉钉通知已发送")
        return 0
    print(f"❌ 发送失败：{result.get('errmsg')}", file=sys.stderr)
    return 1


def cmd_test(args: argparse.Namespace) -> int:
    dt = now_dt()
    msg = (f"【实盘提醒·连通测试】{dt.strftime('%Y-%m-%d %H:%M')}\n"
           "定时提醒通道已打通。触发时点：09:26 / 09:50 / 10:20 / 10:40 / "
           "13:00 / 13:45 / 14:20 / 15:00（工作日）。")
    print(msg)
    if args.dry_run:
        print("dry-run：未发送。")
        return 0
    cfg = load_config()
    result = send_dingtalk(cfg, msg)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("errcode") == 0:
        print("✅ 测试通知已发送")
        return 0
    print(f"❌ 发送失败：{result.get('errmsg')}", file=sys.stderr)
    return 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="实盘定时提醒引擎")
    parser.add_argument("command", choices=["auto", "check", "test"], help="auto=按时间判定并发送；check=只打印不发；test=连通测试")
    parser.add_argument("--dry-run", action="store_true", help="不实际发送")
    parser.add_argument("--at", type=str, default=None, help="仅测试用：按 HH:MM 模拟当前时刻")
    args = parser.parse_args()
    if args.command in ("auto", "check"):
        if args.command == "check":
            args.dry_run = True
        return cmd_auto(args)
    return cmd_test(args)


if __name__ == "__main__":
    raise SystemExit(main())
