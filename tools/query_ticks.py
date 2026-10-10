#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Tencent tick evidence query.

This command exposes the shared adapter used by the workbench.  It reports
source status, coverage and the 5/15-minute B/S/M aggregates; it never writes
orders or changes screening permissions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.data_sources.tencent import TencentTickSource  # noqa: E402
from tools.data_sources.http import project_http_client  # noqa: E402


def _print_result(result: Any, *, json_mode: bool) -> None:
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"状态: {payload.get('status')} | 来源: {payload.get('source')} | 数据日: {payload.get('data_date') or '-'} | 截止: {payload.get('as_of') or '-'}")
    for warning in payload.get("warnings") or []:
        print(f"⚠ {warning}")
    if payload.get("error"):
        print(f"❌ {payload['error'].get('message', payload['error'])}")
    data = payload.get("data") or {}
    if isinstance(data, dict):
        print(f"分笔行数: {data.get('row_count', 0)} | 连续竞价成交额: {data.get('continuous_amount', 0):,.2f} | 快照成交额: {data.get('snapshot_amount', 0):,.2f}")
        for minutes, window in (data.get("windows") or {}).items():
            print(f"{minutes}分钟: 买 {window.get('buy_amount', 0):,.2f} / 卖 {window.get('sell_amount', 0):,.2f} / 中性 {window.get('neutral_amount', 0):,.2f} | 覆盖 {window.get('coverage_minutes', 0):.2f} 分钟 | 比值 {window.get('buy_sell_ratio') if window.get('buy_sell_ratio') is not None else '-'}")
        print(data.get("note", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="腾讯约3秒分笔与5/15分钟买卖方向证据查询")
    parser.add_argument("codes", nargs="+", help="股票代码，如 600519、sz000001")
    parser.add_argument("--max-pages", type=int, default=300, help="最多读取分笔页数")
    parser.add_argument("--force", action="store_true", help="忽略本地增量缓存")
    parser.add_argument("--no-verify", action="store_true", help="不比较分笔成交额与腾讯快照成交额")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="输出 JSON")
    args = parser.parse_args(argv)
    source = TencentTickSource(client=project_http_client())
    exit_code = 0
    for raw in args.codes:
        for code in (part.strip() for part in raw.split(",")):
            if not code:
                continue
            result = source.fetch(code, max_pages=args.max_pages, verify_amount=not args.no_verify, force=args.force)
            if len(args.codes) > 1 and not args.json_mode:
                print(f"\n### {code}")
            _print_result(result, json_mode=args.json_mode)
            if result.status not in {"ok", "empty"}:
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
