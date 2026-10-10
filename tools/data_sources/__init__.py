"""Shared, fail-closed public-data adapters for the A-share workbench.

The package deliberately contains data access and evidence normalization only.
It does not contain trading rules, position handling, or buy/sell decisions.
"""

from .contracts import Result, ResultStatus, SourceError
from .calendar import TradingCalendarService
from .cninfo import CNInfoAnnouncementSource
from .context import ContextSource
from .events import EastmoneyEventSource
from .sina import SinaFinancialSource
from .sentiment import EastmoneySentimentSource
from .symbols import SecuritySymbol, normalize_security
from .tencent import TencentTickSource

__all__ = [
    "Result",
    "ResultStatus",
    "SecuritySymbol",
    "SourceError",
    "CNInfoAnnouncementSource",
    "ContextSource",
    "EastmoneyEventSource",
    "EastmoneySentimentSource",
    "SinaFinancialSource",
    "TradingCalendarService",
    "TencentTickSource",
    "normalize_security",
]
