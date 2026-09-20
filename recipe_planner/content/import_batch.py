"""把 JSONL 菜谱批次导入数据库草稿/审核状态。

示例：
    python -m recipe_planner.content.import_batch --file data/staging/recipes.jsonl --dry-run
    python -m recipe_planner.content.import_batch --file data/staging/recipes.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from recipe_planner.content.common import read_batch, report
from recipe_planner.storage import migrate, sync_bridge
from recipe_planner.storage.repositories import RecipeRepo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入菜谱审核批次")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.file.exists():
        print(json.dumps({"errors": [f"文件不存在：{args.file}"]}, ensure_ascii=False))
        return 2
    batch_id, recipes, errors = read_batch(args.file)
    result = report(batch_id, recipes, errors, dry_run=args.dry_run)
    if errors or not recipes:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    if not args.dry_run:
        migrate.ensure_schema()
        result["imported"] = sync_bridge.run(
            RecipeRepo.upsert_many(recipes, status="review", batch_id=batch_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
