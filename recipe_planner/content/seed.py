"""新库 seed：只导入已发布菜谱，不触碰方案、档案和事件。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from recipe_planner.db import _load_json_db
from recipe_planner.storage import migrate, sync_bridge
from recipe_planner.storage.repositories import RecipeRepo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 seed JSON 初始化已发布菜谱")
    parser.add_argument("--file", type=Path, default=Path("data/recipes.json"))
    args = parser.parse_args(argv)
    if not args.file.exists():
        print(json.dumps({"error": f"文件不存在：{args.file}"}, ensure_ascii=False))
        return 2
    migrate.ensure_schema()
    db = _load_json_db(args.file)
    count = sync_bridge.run(RecipeRepo.upsert_many(db.recipes, status="published"))
    print(json.dumps({"seeded": count, "source": str(args.file)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
