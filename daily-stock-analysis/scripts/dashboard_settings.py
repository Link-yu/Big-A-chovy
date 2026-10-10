#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板参数配置的持久化与校验（唯一写入入口）。

规则语义仍以《选股框架.md》为准；本模块只管理“看板展示与运行”参数，不授予
任何买入权限：

  - ``negative_super_view``：``strict``（默认，沿用现有正式列表）/ ``observe``
    （额外显示独立“负超单观察”列表）。两种取值都保持“超大单为负 = 真实仓
    一票否决”，只是看板是否把这些标的单列出来供观察。
  - ``enabled_boards``：交易板范围，``["main"]``（默认，仅沪深主板）/
    ``["main","chinext","star"]`` 等任意非空组合。**只决定「本轮筛哪些股票」**，
    不授予任何买入权限：勾选后该交易板股票统一参与完整筛选（与主板同一门槛、
    同一排名、同一条状态机），取消勾选则该板不进入新增候选；未选的交易板不得
    混入正式池。北交所暂不在选项内。注意它与按行业计算的「板块共振」是两个不同
    的概念，故命名避免使用“板块”。
  - ``top`` / ``interval`` / ``network_mode``：看板运行参数。

写入约束：白名单字段 + 取值/范围校验 + 原子写入；文件缺失或损坏时回退严格
默认值并把错误一并返回，不静默吞掉。配置写入与买入权限无关。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = SCRIPT_DIR / "dashboard_settings.json"

# 「读取版本 → 校验 → 写入」必须整体串行：并发提交靠它保证只有一个能落盘。
_WRITE_LOCK = threading.Lock()

SCHEMA_VERSION = 1

VIEW_STRICT = "strict"
VIEW_OBSERVE = "observe"
VIEW_CHOICES = (VIEW_STRICT, VIEW_OBSERVE)

NETWORK_MODE_CHOICES = ("auto", "direct", "proxy")

# 交易板：值用于配置与代码内部，标签用于界面/报告。
BOARD_MAIN = "main"
BOARD_CHINEXT = "chinext"
BOARD_STAR = "star"
BOARD_CHOICES = (BOARD_MAIN, BOARD_CHINEXT, BOARD_STAR)
BOARD_LABELS: Dict[str, str] = {
    BOARD_MAIN: "沪深主板",
    BOARD_CHINEXT: "创业板",
    BOARD_STAR: "科创板",
}
DEFAULT_BOARDS: List[str] = [BOARD_MAIN]

TOP_MIN, TOP_MAX = 3, 50
INTERVAL_MIN, INTERVAL_MAX = 10, 600

DEFAULT_DASHBOARD: Dict[str, Any] = {
    "negative_super_view": VIEW_STRICT,
    "enabled_boards": list(DEFAULT_BOARDS),
    "top": 15,
    "interval": 90,
    "network_mode": "auto",
}


def default_config() -> Dict[str, Any]:
    """严格默认配置（深拷贝，调用方可安全修改）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "updated_at": None,
        "dashboard": deepcopy(DEFAULT_DASHBOARD),
    }


def _check_int(value: Any, low: int, high: int, name: str) -> Tuple[Optional[int], Optional[str]]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{name} 必须是整数"
    if not (low <= value <= high):
        return None, f"{name} 必须在 {low}~{high} 之间"
    return value, None


def _check_boards(value: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """校验交易板范围：非空数组、取值在白名单内；去重并按固定顺序归一。

    归一顺序有两个好处：同一组选择无论提交顺序如何都得到同一个存储值（不会
    平白产生一次 revision 变化），报告与 meta 里的记录也可直接比较。
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return None, 'enabled_boards 必须是数组，例如 ["main"]'
    if not value:
        return None, "enabled_boards 至少要选择一个交易板（main/chinext/star）"
    unknown = [item for item in value if item not in BOARD_CHOICES]
    if unknown:
        return None, (
            "enabled_boards 含未知取值："
            + "、".join(str(item) for item in unknown)
            + "（可选 " + "/".join(BOARD_CHOICES) + "）"
        )
    chosen = set(value)
    return [board for board in BOARD_CHOICES if board in chosen], None


def validate_dashboard(dashboard: Any) -> Tuple[Dict[str, Any], list]:
    """校验 dashboard 段，返回 (规范化后的 dashboard, 错误列表)。"""
    errors: list = []
    if not isinstance(dashboard, dict):
        return deepcopy(DEFAULT_DASHBOARD), ["dashboard 必须是对象"]

    unknown = set(dashboard) - set(DEFAULT_DASHBOARD)
    if unknown:
        errors.append("未知字段：" + "、".join(sorted(unknown)))

    normalized: Dict[str, Any] = {}

    view = dashboard.get("negative_super_view", DEFAULT_DASHBOARD["negative_super_view"])
    if view not in VIEW_CHOICES:
        errors.append(f"negative_super_view 取值必须是 {'/'.join(VIEW_CHOICES)}")
    else:
        normalized["negative_super_view"] = view

    # 旧配置没有该字段时补默认 ["main"]（只筛主板），其余设置原样保留。
    boards, boards_error = _check_boards(
        dashboard.get("enabled_boards", DEFAULT_DASHBOARD["enabled_boards"])
    )
    if boards_error:
        errors.append(boards_error)
    else:
        normalized["enabled_boards"] = boards

    top, err = _check_int(dashboard.get("top", DEFAULT_DASHBOARD["top"]), TOP_MIN, TOP_MAX, "top")
    if err:
        errors.append(err)
    else:
        normalized["top"] = top

    interval, err = _check_int(
        dashboard.get("interval", DEFAULT_DASHBOARD["interval"]), INTERVAL_MIN, INTERVAL_MAX, "interval"
    )
    if err:
        errors.append(err)
    else:
        normalized["interval"] = interval

    network_mode = dashboard.get("network_mode", DEFAULT_DASHBOARD["network_mode"])
    if network_mode not in NETWORK_MODE_CHOICES:
        errors.append(f"network_mode 取值必须是 {'/'.join(NETWORK_MODE_CHOICES)}")
    else:
        normalized["network_mode"] = network_mode

    if errors:
        return deepcopy(DEFAULT_DASHBOARD), errors
    return normalized, []


def load(path: Optional[Path] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """读取配置；缺失/损坏/非法时回退严格默认值并返回错误文案。"""
    p = Path(path) if path else SETTINGS_PATH
    if not p.exists():
        return default_config(), None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return default_config(), f"配置文件无法读取，已回退严格默认值：{exc}"

    if not isinstance(raw, dict):
        return default_config(), "配置文件格式错误（顶层不是对象），已回退严格默认值"
    if raw.get("schema_version") != SCHEMA_VERSION:
        return (
            default_config(),
            f"配置文件 schema_version 不受支持（期望 {SCHEMA_VERSION}），已回退严格默认值",
        )

    dashboard, errors = validate_dashboard(raw.get("dashboard"))
    if errors:
        return default_config(), "配置文件校验失败，已回退严格默认值：" + "；".join(errors)

    config = default_config()
    config["dashboard"] = dashboard
    revision = raw.get("revision")
    config["revision"] = revision if isinstance(revision, int) and not isinstance(revision, bool) and revision > 0 else 1
    updated_at = raw.get("updated_at")
    config["updated_at"] = updated_at if isinstance(updated_at, str) else None
    return config, None


def _atomic_write(config: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = Path(path) if path else SETTINGS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply(payload: Any, path: Optional[Path] = None) -> Dict[str, Any]:
    """应用一次配置提交。

    payload: ``{"revision": <客户端读取到的版本>, "dashboard": {...}}``。
    **必须带整数 revision**（并发保护的前提）；版本不一致或字段非法时拒绝写入。
    整段「读取 → 比较 → 校验 → 写入」在同一把锁内完成，避免并发提交互相覆盖。
    返回 dict：{ok, errors, config, conflict?, warning?}
    """
    if not isinstance(payload, dict):
        return {"ok": False, "errors": ["提交内容必须是对象"], "config": load(path)[0]}

    expected = payload.get("revision")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        current, _ = load(path)
        return {
            "ok": False,
            "errors": ["提交必须带正整数 revision（用于并发版本校验）"],
            "config": current,
        }

    with _WRITE_LOCK:
        current, load_error = load(path)
        if expected != current["revision"]:
            return {
                "ok": False,
                "conflict": True,
                "errors": [
                    f"配置版本冲突：当前 v{current['revision']}，提交基于 v{expected}；请刷新后重试"
                ],
                "config": current,
            }

        submitted = payload.get("dashboard")
        if not isinstance(submitted, dict):
            return {"ok": False, "errors": ["dashboard 必须是对象"], "config": current}
        # 缺字段沿用当前值：只提交部分字段的客户端不得把未提交的字段重置为默认。
        # （2026-09-30：工作台改其它配置时漏传 enabled_boards，交易板范围被重置成主板。）
        merged = {**dict(current.get("dashboard") or {}), **submitted}
        dashboard, errors = validate_dashboard(merged)
        if errors:
            return {"ok": False, "errors": errors, "config": current}

        new_config = {
            "schema_version": SCHEMA_VERSION,
            "revision": int(current["revision"]) + 1,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dashboard": dashboard,
        }
        try:
            _atomic_write(new_config, path)
        except OSError as exc:
            return {"ok": False, "errors": [f"写入失败：{exc}"], "config": current}
    result: Dict[str, Any] = {"ok": True, "errors": [], "config": new_config}
    if load_error:
        result["warning"] = load_error
    return result


def view_of(config: Optional[Dict[str, Any]]) -> str:
    """安全提取观察模式；任何异常都回退严格模式。"""
    dashboard = (config or {}).get("dashboard") or {}
    view = dashboard.get("negative_super_view")
    return view if view in VIEW_CHOICES else VIEW_STRICT


def boards_of(config: Optional[Dict[str, Any]]) -> List[str]:
    """安全提取交易板范围；任何异常都回退 ['main']（默认只筛沪深主板）。"""
    dashboard = (config or {}).get("dashboard") or {}
    boards, error = _check_boards(dashboard.get("enabled_boards"))
    if error or not boards:
        return list(DEFAULT_BOARDS)
    return boards


def boards_label(boards: Optional[List[str]]) -> str:
    """交易板范围的可读标签，用于报告与状态条（如「仅沪深主板」「沪深主板 + 创业板」）。"""
    items = boards if boards else list(DEFAULT_BOARDS)
    return " + ".join(BOARD_LABELS.get(board, board) for board in items)


def parse_request_boards(value: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """判别一次请求里的交易板覆盖，返回 ``(boards, error)``。

    - **未提供**（``None``）：``(None, None)`` —— 调用方继承已保存配置；
    - **非法**：``(None, 错误文案)`` —— 调用方必须拒绝该请求（HTTP 400）。

    为什么不把非法值回退成已保存配置：回退**不一定更窄**。若保存的是三板，
    一个写错的请求照样会跑三板，而用户以为“我的覆盖没生效”，无从察觉。
    """
    if value is None:
        return None, None
    boards, error = _check_boards(value)
    if error:
        return None, error
    return boards, None


def normalize_request_boards(value: Any) -> Optional[List[str]]:
    """兼容旧调用点：非法一律回退 ``None``（由调用方继承配置）。

    新代码请用 :func:`parse_request_boards`，它能把“未提供”和“非法”区分开，
    从而对非法覆盖返回 400 而不是静默继承。
    """
    boards, error = parse_request_boards(value)
    return None if error else boards


def run_params(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """安全提取看板运行参数（top/interval/network_mode）。"""
    dashboard = (config or {}).get("dashboard") or {}
    top, _ = _check_int(dashboard.get("top"), TOP_MIN, TOP_MAX, "top")
    interval, _ = _check_int(dashboard.get("interval"), INTERVAL_MIN, INTERVAL_MAX, "interval")
    network_mode = dashboard.get("network_mode")
    return {
        "top": top if top is not None else DEFAULT_DASHBOARD["top"],
        "interval": interval if interval is not None else DEFAULT_DASHBOARD["interval"],
        "network_mode": network_mode if network_mode in NETWORK_MODE_CHOICES else DEFAULT_DASHBOARD["network_mode"],
    }
