"""归档菜谱（保留历史引用，不物理删除）。"""
from __future__ import annotations

import argparse
import json
import sys

from recipe_planner.storage import migrate, sync_bridge
from recipe_planner.storage.repositories import RecipeRepo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="归档菜谱")
    parser.add_argument("--recipe-id", required=True)
    parser.add_argument("--expected-version", type=int)
    args = parser.parse_args(argv)
    migrate.ensure_schema()
    try:
        archived = sync_bridge.run(RecipeRepo.archive(args.recipe_id, args.expected_version))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"recipe_id": args.recipe_id, "archived": archived}, ensure_ascii=False))
    return 0 if archived else 2


if __name__ == "__main__":
    sys.exit(main())
