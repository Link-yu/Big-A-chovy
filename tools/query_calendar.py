#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Query the official Shenzhen Exchange trading calendar."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.data_sources.calendar import TradingCalendarService  # noqa: E402
from tools.data_sources.http import project_http_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="查询深交所官方整月交易日历和T+1日期")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD 或 YYYYMMDD")
    parser.add_argument("--action", choices=("is_open", "next", "session"), default="is_open", help="默认查询是否开市")
    parser.add_argument("--force", action="store_true", help="忽略当月缓存")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()
    source = TradingCalendarService(client=project_http_client())
    if args.action == "next":
        result = source.next_trading_day(args.date, force=args.force)
    elif args.action == "session":
        result = source.next_session(args.date, force=args.force)
    else:
        result = source.is_open(args.date, force=args.force)
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"status={payload['status']} source={payload['source']} data={json.dumps(payload['data'], ensure_ascii=False)}")
        if payload.get("error"):
            print(f"error={payload['error'].get('message', payload['error'])}")
    return 0 if payload["status"] not in {"unavailable", "unsupported"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
