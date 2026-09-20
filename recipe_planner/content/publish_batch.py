"""发布一个已导入并人工审核的菜谱批次。"""
from __future__ import annotations

import argparse
import json
import sys

from recipe_planner.storage import migrate, sync_bridge
from recipe_planner.storage.repositories import RecipeRepo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布菜谱审核批次")
    parser.add_argument("--batch", required=True, help="import_batch 输出的批次号")
    args = parser.parse_args(argv)
    migrate.ensure_schema()
    published = sync_bridge.run(RecipeRepo.publish_batch(args.batch))
    result = {"batch_id": args.batch, "published": published}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if published else 2


if __name__ == "__main__":
    sys.exit(main())
