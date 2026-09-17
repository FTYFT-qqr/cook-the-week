"""Validate a staged recipe JSONL batch before human review or publishing.

The script deliberately validates content without touching the live database.
Staging records may carry review metadata, but only fields understood by the
Recipe model are published to data/recipes.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 允许从仓库根目录或直接以 `python scripts/...` 运行
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recipe_planner.allergens import hidden_allergens
from recipe_planner.models import ALLERGENS, GOALS, SPICE_LEVELS, TASTE_TAGS, Recipe

ALLOWED_CATEGORIES = {"热菜", "凉菜", "汤", "主食", "早餐"}
ALLOWED_DIFFICULTIES = {"简单", "中等", "较难"}
STAGING_FIELDS = {"source_type", "source_url", "source_creator", "reviewed_at",
                  "review_status", "gap_tags", "batch_id", "review_notes"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_no} 行不是合法 JSON：{exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"第 {line_no} 行必须是 JSON 对象")
        records.append(value)
    return records


def validate_batch(records: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    names: set[str] = set()
    for index, raw in enumerate(records, 1):
        label = f"第 {index} 条"
        try:
            recipe = Recipe.model_validate(raw)
        except Exception as exc:  # Pydantic formats the field-level details for us
            errors.append(f"{label} {raw.get('id', '?')} 字段校验失败：{exc}")
            continue
        if recipe.id in ids:
            errors.append(f"{label} ID 重复：{recipe.id}")
        ids.add(recipe.id)
        if recipe.name in names:
            errors.append(f"{label} 菜名重复：{recipe.name}")
        names.add(recipe.name)
        if recipe.category not in ALLOWED_CATEGORIES:
            errors.append(f"{recipe.id} 使用未受控菜品分类：{recipe.category}")
        if recipe.difficulty not in ALLOWED_DIFFICULTIES:
            errors.append(f"{recipe.id} 使用未受控难度：{recipe.difficulty}")
        if recipe.spice_level not in SPICE_LEVELS:
            errors.append(f"{recipe.id} 使用未受控辣度：{recipe.spice_level}")
        for tag in recipe.taste_tags:
            if tag not in TASTE_TAGS:
                errors.append(f"{recipe.id} 使用未受控口味标签：{tag}")
        for tag in recipe.goal_tags:
            if tag not in GOALS[1:]:
                errors.append(f"{recipe.id} 使用未受控目标标签：{tag}")
        for tag in recipe.allergens:
            if tag not in ALLERGENS:
                errors.append(f"{recipe.id} 使用未受控过敏原：{tag}")
        if not 3 <= len(recipe.steps) <= 5:
            errors.append(f"{recipe.id} 做法必须为 3–5 步，实际 {len(recipe.steps)} 步")
        for step in recipe.steps:
            if not 6 <= len(step) <= 40:
                errors.append(f"{recipe.id} 做法长度不合规范：{step}")
            if any("a" <= char.lower() <= "z" for char in step):
                errors.append(f"{recipe.id} 做法含英文：{step}")
            if step.endswith(("。", "；", ";")) or " " in step or "　" in step:
                errors.append(f"{recipe.id} 做法格式不统一：{step}")
        for ingredient in recipe.ingredients:
            if ingredient.category not in {"蔬菜", "肉蛋", "水产", "调料", "干货",
                                            "豆制品", "菌菇", "其他", "主食"}:
                errors.append(f"{recipe.id} 使用未受控食材分类：{ingredient.category}")
            if ingredient.category not in {"调料", "其他"} and not ingredient.grams:
                errors.append(f"{recipe.id} 非调料食材缺少克数：{ingredient.name}")
        detected = hidden_allergens(recipe.name, [i.name for i in recipe.ingredients])
        missing = detected - set(recipe.allergens)
        if missing:
            errors.append(f"{recipe.id} 隐性过敏原未显式标注：{'、'.join(sorted(missing))}")
        if raw.get("review_status") not in {None, "draft", "reviewed"}:
            errors.append(f"{recipe.id} review_status 不受支持：{raw.get('review_status')}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验待审核菜谱 JSONL 批次")
    parser.add_argument("path", nargs="?", default="data/staging/recipes.jsonl")
    args = parser.parse_args()
    path = Path(args.path)
    try:
        records = read_jsonl(path)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    errors = validate_batch(records)
    if errors:
        print(f"[FAIL] {path}: {len(errors)} 个问题")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"[OK] {path}: {len(records)} 道菜通过结构化校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
