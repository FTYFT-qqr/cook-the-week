"""Publish an approved JSONL recipe batch to the canonical JSON catalogue.

Publishing is explicit and review-manifest driven. It never writes the real
database; STORAGE=db is synchronized by the existing application path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from check_recipe_duplicates import duplicate_issues
from validate_recipe_batch import STAGING_FIELDS, read_jsonl, validate_batch


def publish(batch_path: Path, review_path: Path, output_path: Path, check_only: bool = False) -> int:
    batch = read_jsonl(batch_path)
    errors = validate_batch(batch)
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("status") != "approved":
        print("[FAIL] 审核清单不是 approved，禁止发布")
        return 1
    reviewed_ids = {str(rid) for rid in review.get("recipe_ids", [])}
    batch_ids = {str(item["id"]) for item in batch}
    if reviewed_ids != batch_ids:
        print(f"[FAIL] 审核清单与批次 ID 不一致：缺少 {sorted(batch_ids - reviewed_ids)}，"
              f"多出 {sorted(reviewed_ids - batch_ids)}")
        return 1
    current = json.loads(output_path.read_text(encoding="utf-8"))
    existing = list(current.get("recipes", []))
    exact, similar = duplicate_issues(batch, existing)
    if exact:
        for item in exact:
            print(f"[FAIL] {item}")
        return 1
    if similar:
        print("[FAIL] 存在高相似度菜名，需先完成审核清单后再发布")
        for item in similar:
            print(f"  - {item}")
        return 1
    published = [{k: v for k, v in item.items() if k not in STAGING_FIELDS} for item in batch]
    result = {**current, "recipes": existing + published}
    if check_only:
        print(f"[OK] 发布预检通过：将新增 {len(published)} 道，合计 {len(result['recipes'])} 道")
        return 0
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] 已发布 {len(published)} 道到 {output_path}，合计 {len(result['recipes'])} 道")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="发布通过审核的菜谱批次")
    parser.add_argument("path", nargs="?", default="data/staging/recipes.jsonl")
    parser.add_argument("--review", default="data/staging/reviewed/c01-2026-09-17.json")
    parser.add_argument("--output", default="data/recipes.json")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        return publish(Path(args.path), Path(args.review), Path(args.output), args.check_only)
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
