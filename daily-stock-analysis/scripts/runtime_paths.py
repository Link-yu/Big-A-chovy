#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行状态路径的唯一来源（不依赖其它模块，避免循环导入）。

默认不设环境变量时与历史行为逐字节一致：状态文件写在脚本目录下。

设置 ``A_SHARE_STATE_DIR`` 可把**所有运行时状态**定向到别的目录。这条能力是为
端到端验证准备的：验证跑会覆盖盘中的真实状态，而交集状态机与观察池突破状态机
依赖这些文件（首次交集锁存、等待回踩、突破确认次数、资金 5/15 分钟基准），
被覆盖后当天剩余快照的确认链会断裂。

跟随 ``A_SHARE_STATE_DIR`` 的（运行状态/缓存）：
    ``flow_snapshot.json``、``intersection_state.json``、
    ``watchlist_breakout_state.json``、``.kline_cache.json``、
    ``.announcement_risk_cache.json``、``.em_cooldown``、``last_valid_result.json``

**不**跟随的（配置、输入与产物）：
    ``dashboard_settings.json``（用户配置）、``holdings.json``（持仓）、
    ``proxy_ports.json``、``intersection_calibration.json``（标定配置），
    以及看板的报告归档目录 ``筛选结果/``（另见 ``A_SHARE_REPORT_DIR``）。

环境变量在**进程启动（导入）时**读取一次。因此验证时必须先设置再启动进程：

    A_SHARE_STATE_DIR=/tmp/zcode-e2e/state \
    A_SHARE_REPORT_DIR=/tmp/zcode-e2e/reports \
    python3 daily-stock-analysis/scripts/a_share_daily_screen.py --boards main chinext
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR_ENV = "A_SHARE_STATE_DIR"
REPORT_DIR_ENV = "A_SHARE_REPORT_DIR"

DEFAULT_DIR = Path(__file__).resolve().parent


def _override(env_name: str) -> Path | None:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except (OSError, RuntimeError, ValueError):
        # 路径无法解析（含空字节、无法展开等）时退回默认目录：环境变量写错了不该
        # 让进程起不来。真正不可用（指向文件、无权限）在 state_file 里报错。
        return None


STATE_DIR = _override(STATE_DIR_ENV) or DEFAULT_DIR


def state_dir() -> Path:
    """运行时状态目录。"""
    return STATE_DIR


def state_file(name: str) -> Path:
    """运行时状态文件路径。

    目录不可用（指向了文件、无写权限等）时**抛出** ``RuntimeError``，而不是回退到
    真实状态目录：静默回退会让“隔离”变成假象，验证跑照样污染盘中状态，且没人会发现。
    """
    path = STATE_DIR / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        # ValueError 覆盖含空字节等非法路径（Path 构造时不报错，落盘时才炸）。
        raise RuntimeError(
            f"无法创建运行状态目录 {path.parent}：{exc}。"
            f"检查 {STATE_DIR_ENV} 是否指向可写目录，或取消该变量后用默认目录。"
        ) from exc
    return path


def report_dir(default: Path) -> Path:
    """报告归档目录；设了 ``A_SHARE_REPORT_DIR`` 时改写到该目录。

    验证跑同样不该把产物写进 ``筛选结果/``：那份序列是盘中工作流（``scan_reports``、
    “继续看筛选”）的输入，混入验证产物会污染当日结论。
    """
    return _override(REPORT_DIR_ENV) or default
