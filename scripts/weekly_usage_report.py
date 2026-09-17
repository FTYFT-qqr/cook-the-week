"""生成本地真实使用周报，不写入用户数据。"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recipe_planner.usage_report import build_report, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="生成本地真实使用周报")
    parser.add_argument("--weeks", type=int, default=8, help="回看周数，默认 8")
    parser.add_argument("--today", default="", help="测试用截止日期，格式 YYYY-MM-DD")
    parser.add_argument("--output-dir", default=".tmp/usage-report")
    args = parser.parse_args()
    try:
        cutoff = date.fromisoformat(args.today) if args.today else None
        report = build_report(today=cutoff, weeks=args.weeks)
        json_path, md_path = write_report(report, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[OK] {report['sample']['status']}：观察 {report['sample']['observed_plan_weeks']} 周")
    print(f"[OK] JSON：{json_path}")
    print(f"[OK] Markdown：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
