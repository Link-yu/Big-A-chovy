#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only market sentiment summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.data_sources.sentiment import EastmoneySentimentSource  # noqa: E402
from tools.data_sources.http import project_http_client  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="涨停/炸板/跌停与连板背景摘要")
    parser.add_argument("--date", help="数据日 YYYY-MM-DD，默认北京时间今天")
    parser.add_argument("--force", action="store_true", help="忽略情绪短缓存")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="输出 JSON")
    args = parser.parse_args(argv)
    result = EastmoneySentimentSource(client=project_http_client()).fetch(args.date, force=args.force)
    payload = result.to_dict()
    if args.json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"状态: {payload['status']} | 数据日: {payload.get('data_date') or '-'} | 来源: {payload['source']}")
        if payload.get("error"):
            print(f"❌ {payload['error'].get('message')}")
        data = payload.get("data") or {}
        metrics = data.get("metrics") if isinstance(data, dict) else None
        if metrics:
            print(f"涨停 {metrics.get('limit_up_count')} | 炸板 {metrics.get('broken_count')} | 跌停 {metrics.get('limit_down_count')} | 炸板率 {metrics.get('break_rate') if metrics.get('break_rate') is not None else '-'}%")
            print(f"最高连板 {metrics.get('max_streak') or '-'} | 昨涨停今日平均 {metrics.get('yesterday_limit_up_today_average_change_pct') if metrics.get('yesterday_limit_up_today_average_change_pct') is not None else '-'}% | 晋级率 {metrics.get('promotion_rate') if metrics.get('promotion_rate') is not None else '-'}%")
            print(f"覆盖: {metrics.get('coverage', {}).get('scope')}；仅背景摘要，不改变现有评分/权限")
        for warning in payload.get("warnings") or []:
            print(f"⚠ {warning}")
    return 0 if result.status in {"ok", "empty"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
