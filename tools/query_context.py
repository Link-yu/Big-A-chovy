#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Allowlisted single-security context query."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.data_sources.context import CONTEXT_TOPICS, ContextSource  # noqa: E402
from tools.data_sources.http import project_http_client  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="个股按需上下文：监控/异动/题材/新闻/研报/互动/龙虎榜/商品")
    parser.add_argument("code", help="股票代码")
    parser.add_argument("--topic", required=True, choices=CONTEXT_TOPICS, help="一次按需读取一个白名单主题")
    parser.add_argument("--date", help="观察日期 YYYY-MM-DD")
    parser.add_argument("--contract", help="商品 allowlist key 或合约代码，仅 commodity 使用")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    args = parser.parse_args(argv)
    result = ContextSource(client=project_http_client()).fetch(args.code, topic=args.topic, as_of=args.date, contract=args.contract, limit=args.limit, force=args.force)
    if args.json_mode:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"状态: {result.status} | topic: {args.topic} | 来源: {result.source} | 时点: {result.as_of or '-'}")
        if result.error:
            print(f"❌ {result.error.get('message')}")
        print(json.dumps(result.data, ensure_ascii=False, indent=2))
        for warning in result.warnings:
            print(f"⚠ {warning}")
    return 0 if result.status in {"ok", "empty"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
