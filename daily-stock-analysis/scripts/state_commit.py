#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回合状态提交门（round commit gate）。

背景：看板/工作台在守护线程里跑引擎，``join(timeout)`` 超时后线程仍在跑。
旧实现里线程会在 ``save_flow_history``、``save_intersection_state``、
``save_watchlist_breakout_state`` 等路径继续写正式状态，于是：

* 一轮已被宣布超时/失败，却仍提交了运行状态；
* 下一轮可能与旧轮并发读改写同一份状态文件。

本模块把「计算出的状态增量」和「落盘」分开：一轮把要写的状态 ``stage`` 进
``RoundCommit``，只有调度器在确认该轮有效后才 ``commit``。调度器一旦判定超时/
失败就 ``abort``，该轮所有暂存写入被丢弃；此后引擎再 stage 也只是空操作。

* ``stage`` / ``commit`` / ``abort`` 由同一把锁保护，二者互斥：要么整轮提交，
  要么整轮丢弃，不存在一半提交。
* 实际落盘由模块级 ``_commit_lock`` 串行化，避免两个轮次交叉覆盖同一文件。
* 每个文件自身必须是临时文件 + ``os.replace`` 的原子写（由写入闭包负责），
  这样即便进程在提交途中被杀，也不会留下半截 JSON。

没有绑定 ``RoundCommit`` 的调用（CLI、单测）保持历史行为，由各自的 save 函数
立即安全落盘。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# 串行化所有回合的提交，防止两轮交叉覆盖同一状态文件。
_commit_lock = threading.RLock()

Writer = Callable[[], None]


def atomic_write_text(path: Path, text: str) -> None:
    """临时文件 + os.replace 原子写入。失败时抛错，由调用方决定是否记录。"""
    path = Path(path)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class RoundCommit:
    """一轮筛选的状态提交门。

    ``round_id`` 只用于日志/诊断；生命周期为 ``staging`` → ``committed`` 或
    ``aborted``，且不可逆。
    """

    def __init__(self, round_id: Optional[str] = None) -> None:
        self.round_id = round_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        self._lock = threading.Lock()
        self._pending: List[Tuple[Path, Writer]] = []
        self._active = True
        self.committed = False

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def stage(self, path: Path, writer: Writer) -> bool:
        """暂存一次写入。轮次已结束（提交/中止）时返回 False 并丢弃。"""
        with self._lock:
            if not self._active:
                return False
            self._pending.append((Path(path), writer))
            return True

    def abort(self) -> bool:
        """中止本轮。返回 True 表示成功拦下（尚未提交），False 表示已经提交过。"""
        with self._lock:
            if not self._active:
                return False
            self._active = False
            self._pending.clear()
            return True

    def commit(self) -> bool:
        """提交本轮。返回 True 表示已提交，False 表示本轮已被中止。"""
        with self._lock:
            if not self._active:
                return False
            pending = list(self._pending)
            self._pending.clear()
            self._active = False
        with _commit_lock:
            for _path, writer in pending:
                writer()
        with self._lock:
            self.committed = True
        return True


def write_or_stage(commit: Optional[RoundCommit], path: Path, text: str) -> bool:
    """有回合门则暂存；没有则立即原子落盘（历史行为）。

    立即落盘路径吞掉异常（与既有 save_* 一致：状态写失败不该让整轮崩掉）；
    暂存路径把异常留给 commit 时抛出，由调度器记录为可诊断错误。
    """
    if commit is not None:
        return commit.stage(path, lambda: atomic_write_text(path, text))
    try:
        atomic_write_text(path, text)
        return True
    except Exception:
        return False
