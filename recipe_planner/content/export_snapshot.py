"""从数据库导出可读菜谱快照。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from recipe_planner.storage import migrate, sync_bridge
from recipe_planner.storage.repositories import RecipeRepo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出菜谱数据库快照")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--include-archived", action="store_true")
    args = parser.parse_args(argv)
    migrate.ensure_schema()
    snapshot = sync_bridge.run(RecipeRepo.export_snapshot(
        include_archived=args.include_archived))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"output": str(args.output), "recipes": len(snapshot["recipes"]),
                      "catalog_version": snapshot["catalog_version"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
