"""Versioned result and error contracts shared by all data sources.

The screener historically returned plain dictionaries.  New adapters use this
small contract so a failed or stale source cannot be mistaken for a valid empty
result.  ``Result.to_dict`` keeps the boundary JSON-friendly for the CLI and
the local HTTP workbench.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class ResultStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SourceError:
    """Machine-readable error information; never use an error as an empty row."""

    code: str
    message: str
    retryable: bool = False
    http_status: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Result:
    """Common source result envelope.

    ``data_date`` is the market/source date.  ``fetched_at`` is when this
    process got the response.  They are intentionally separate so a cached
    older response cannot masquerade as current data.
    """

    status: ResultStatus | str
    data: Any = None
    source: str = ""
    source_url: str = ""
    fetched_at: str = field(default_factory=utc_now_iso)
    data_date: str | None = None
    as_of: str | None = None
    freshness: str = "unknown"
    warnings: list[str] = field(default_factory=list)
    error: SourceError | Mapping[str, Any] | None = None
    schema_version: str = "a-share-data.v1"
    cache: dict[str, Any] | None = None
    request_count: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.status, ResultStatus):
            self.status = self.status.value
        if self.status not in {item.value for item in ResultStatus}:
            raise ValueError(f"未知结果状态: {self.status!r}")
        if self.error is not None and isinstance(self.error, SourceError):
            self.error = self.error.to_dict()
        self.warnings = [str(item) for item in (self.warnings or [])]

    @property
    def ok(self) -> bool:
        return self.status == ResultStatus.OK.value

    @property
    def usable(self) -> bool:
        return self.status in {
            ResultStatus.OK.value,
            ResultStatus.EMPTY.value,
            ResultStatus.PARTIAL.value,
            ResultStatus.STALE.value,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = str(payload["status"])
        return payload

    def with_warning(self, warning: str) -> "Result":
        self.warnings.append(str(warning))
        return self


def result_ok(data: Any, *, source: str, source_url: str, **kwargs: Any) -> Result:
    return Result(
        status=ResultStatus.OK,
        data=data,
        source=source,
        source_url=source_url,
        freshness=kwargs.pop("freshness", "fresh"),
        **kwargs,
    )


def result_empty(*, source: str, source_url: str, data: Any = None, **kwargs: Any) -> Result:
    return Result(
        status=ResultStatus.EMPTY,
        data=[] if data is None else data,
        source=source,
        source_url=source_url,
        freshness=kwargs.pop("freshness", "fresh"),
        **kwargs,
    )


def result_error(
    status: ResultStatus | str,
    *,
    source: str,
    source_url: str,
    code: str,
    message: str,
    retryable: bool = False,
    data: Any = None,
    warnings: list[str] | None = None,
    **kwargs: Any,
) -> Result:
    normalized_status = status.value if isinstance(status, ResultStatus) else str(status)
    if normalized_status not in {item.value for item in ResultStatus}:
        raise ValueError(f"未知结果状态: {status!r}")
    return Result(
        status=normalized_status,
        data=data,
        source=source,
        source_url=source_url,
        freshness=kwargs.pop("freshness", "unknown"),
        warnings=warnings or [],
        error=SourceError(code, message, retryable=retryable),
        **kwargs,
    )
