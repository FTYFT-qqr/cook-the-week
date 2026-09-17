"""Check exact and high-similarity duplicates in a staged recipe batch."""
from __future__ import annotations

import argparse
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from validate_recipe_batch import read_jsonl


def _norm(text: str) -> str:
    return "".join(str(text).lower().split()).replace("（", "(").replace("）", ")")


def duplicate_issues(batch: list[dict[str, Any]], existing: list[dict[str, Any]],
                    similarity_threshold: float = 0.86) -> tuple[list[str], list[str]]:
    exact: list[str] = []
    similar: list[str] = []
    existing_ids = {str(item.get("id")) for item in existing}
    existing_names = {_norm(str(item.get("name", ""))) for item in existing}
    batch_ids: set[str] = set()
    batch_names: set[str] = set()
    for item in batch:
        rid = str(item.get("id", ""))
        name = str(item.get("name", ""))
        normalized = _norm(name)
        if rid in existing_ids:
            exact.append(f"ID 已存在：{rid}")
        if normalized in existing_names:
            exact.append(f"菜名已存在：{name}")
        if rid in batch_ids:
            exact.append(f"批次内 ID 重复：{rid}")
        if normalized in batch_names:
            exact.append(f"批次内菜名重复：{name}")
        batch_ids.add(rid)
        batch_names.add(normalized)
        for old_name in existing_names:
            score = SequenceMatcher(None, normalized, old_name).ratio()
            if score >= similarity_threshold and normalized != old_name:
                similar.append(f"{name} 与已有菜名相似度 {score:.2f}（人工复核）")
                break
    return exact, similar


def main() -> int:
    parser = argparse.ArgumentParser(description="检查菜谱批次重复和高相似度名称")
    parser.add_argument("path", nargs="?", default="data/staging/recipes.jsonl")
    parser.add_argument("--existing", default="data/recipes.json")
    parser.add_argument("--strict-similarity", action="store_true")
    args = parser.parse_args()
    try:
        batch = read_jsonl(Path(args.path))
        existing = json.loads(Path(args.existing).read_text(encoding="utf-8"))["recipes"]
        exact, similar = duplicate_issues(batch, existing)
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    for item in similar:
        print(f"[REVIEW] {item}")
    if exact or (args.strict_similarity and similar):
        for item in exact:
            print(f"[FAIL] {item}")
        if args.strict_similarity:
            for item in similar:
                print(f"[FAIL] {item}")
        return 1
    print(f"[OK] 未发现精确重复；高相似度名称 {len(similar)} 条需人工复核")
    return 0


if __name__ == "__main__":
    sys.exit(main())
