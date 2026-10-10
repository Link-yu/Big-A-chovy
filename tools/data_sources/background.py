"""Optional market-background composition for reports and the workbench."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import inspect
import threading
import time
from typing import Any, Callable

from .calendar import TradingCalendarService
from .http import HTTPClient, project_http_client as _project_http_client
from .sentiment import EastmoneySentimentSource


BEIJING = timezone(timedelta(hours=8))
BACKGROUND_DEFAULT_BUDGET_SECONDS = 8.0
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_THREADS: set[threading.Thread] = set()


def project_http_client(*, deadline: float | None = None) -> HTTPClient:
    """Compatibility export for callers that historically imported it here."""
    return _project_http_client(deadline=deadline)


def _budget_result(message: str = "市场背景查询超过总预算") -> dict[str, Any]:
    return {
        "status": "unavailable",
        "error": {"code": "background_budget_exceeded", "message": message},
        "warnings": ["背景查询已限时，不影响核心筛选结果"],
    }


def _call_with_optional_deadline(function: Callable[..., Any], value: str, deadline: float) -> Any:
    """Call old injected test doubles and new deadline-aware sources safely."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "deadline" in parameters:
        return function(value, deadline=deadline)
    return function(value)


def build_market_background(
    data_date: str | None = None,
    *,
    calendar_service: TradingCalendarService | None = None,
    sentiment_source: EastmoneySentimentSource | None = None,
    include_sentiment: bool = True,
    budget_seconds: float = BACKGROUND_DEFAULT_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Return independently degradable evidence within a process-level budget.

    The workers are daemon threads, so a stalled provider cannot keep a CLI
    process alive after the budget.  A later invocation refuses to overlap an
    earlier still-running provider; late worker results are discarded rather
    than being published as if they completed inside the budget.
    """
    if data_date is None:
        data_date = datetime.now(BEIJING).date().isoformat()
    budget = max(0.05, float(budget_seconds))
    deadline = time.monotonic() + budget
    names = ["calendar"] + (["sentiment"] if include_sentiment else [])
    background: dict[str, Any] = {
        "data_date": data_date,
        "note": "市场情绪与事件仅作背景证据，不改变现有评分、门禁或真实仓权限",
    }

    with _ACTIVE_LOCK:
        _ACTIVE_THREADS.difference_update({thread for thread in _ACTIVE_THREADS if not thread.is_alive()})
        if any(thread.is_alive() for thread in _ACTIVE_THREADS):
            for name in names:
                background[name] = {
                    "status": "unavailable",
                    "error": {"code": "background_inflight", "message": "上一轮背景查询仍在后台，拒绝重叠请求"},
                    "warnings": ["背景查询未重叠；不影响核心筛选结果"],
                }
            return background

    results: dict[str, Any] = {}
    completed = {name: threading.Event() for name in names}
    client_lock = threading.Lock()
    shared_client: HTTPClient | None = None

    def get_shared_client() -> HTTPClient:
        nonlocal shared_client
        with client_lock:
            if shared_client is None:
                shared_client = project_http_client(deadline=deadline)
            return shared_client

    def worker(name: str) -> None:
        try:
            if time.monotonic() >= deadline:
                return
            if name == "calendar":
                service = calendar_service or TradingCalendarService(
                    client=get_shared_client(),
                    request_timeout=max(0.1, min(10.0, budget / 3)),
                )
                result = _call_with_optional_deadline(service.is_open, data_date, deadline)
            else:
                service = sentiment_source or EastmoneySentimentSource(
                    client=get_shared_client(),
                    request_timeout=max(0.1, min(10.0, budget / 3)),
                )
                result = _call_with_optional_deadline(service.fetch, data_date, deadline)
            if time.monotonic() < deadline:
                results[name] = result.to_dict() if hasattr(result, "to_dict") else result
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() < deadline:
                results[name] = {
                    "status": "unavailable",
                    "error": {"code": "background_source_error", "message": f"{type(exc).__name__}: {exc}"},
                    "warnings": ["背景源失败，不影响核心筛选结果"],
                }
        finally:
            completed[name].set()

    threads: list[threading.Thread] = []
    for name in names:
        thread = threading.Thread(target=worker, args=(name,), name=f"a-share-background-{name}", daemon=True)
        threads.append(thread)
        with _ACTIVE_LOCK:
            _ACTIVE_THREADS.add(thread)
        thread.start()

    while time.monotonic() < deadline and any(not event.is_set() for event in completed.values()):
        remaining = max(0.0, deadline - time.monotonic())
        next((event for event in completed.values() if not event.is_set()), threading.Event()).wait(timeout=remaining)

    for name in names:
        background[name] = results.get(name, _budget_result())
    # Removing only finished threads keeps the overlap guard active for a
    # late daemon worker; the worker itself is never allowed to publish late.
    with _ACTIVE_LOCK:
        _ACTIVE_THREADS.difference_update({thread for thread in _ACTIVE_THREADS if not thread.is_alive()})
    return background
