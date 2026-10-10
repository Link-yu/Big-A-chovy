#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""On-demand overnight event evidence query."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.data_sources.events import EVENT_TYPES, EastmoneyEventSource  # noqa: E402
from tools.data_sources.http import project_http_client  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解禁/增减持/业绩预告/回购/质押事件证据查询")
    parser.add_argument("codes", nargs="+", help="股票代码")
    parser.add_argument("--types", default=",".join(EVENT_TYPES), help="逗号分隔事件类型")
    parser.add_argument("--date", help="观察日 YYYY-MM-DD，默认北京时间今天")
    parser.add_argument("--forward-days", type=int, default=90, help="解禁向前看的自然日范围")
    parser.add_argument("--force", action="store_true", help="忽略事件缓存")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="输出 JSON")
    args = parser.parse_args(argv)
    types = [value.strip() for value in args.types.split(",") if value.strip()]
    source = EastmoneyEventSource(client=project_http_client())
    results = []
    for raw in args.codes:
        for code in (value.strip() for value in raw.split(",")):
            if code:
                results.append(source.fetch(code, event_types=types, as_of=args.date, forward_days=args.forward_days, force=args.force).to_dict())
    if args.json_mode:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"{result['status']} | {result['source']} | {result.get('data_date') or '-'}")
            if result.get("error"):
                print(f"  ❌ {result['error'].get('message')}")
            data = result.get("data") or {}
            for row in data.get("rows", []) if isinstance(data, dict) else []:
                print(f"  {row.get('event_type_name')} | 公告 {row.get('notice_date') or '-'} | 生效 {row.get('effective_date') or '-'} | {row.get('title') or '-'}")
    return 0 if all(result["status"] in {"ok", "empty"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
