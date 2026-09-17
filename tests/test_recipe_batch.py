"""C-01 菜谱批次的可回退发布护栏。"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_recipe_batch import read_jsonl, validate_batch
from recipe_planner.db import _load_json_db


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data" / "staging"


def test_c01_批次通过结构校验并有完整审核清单():
    batch = read_jsonl(STAGING / "recipes.jsonl")
    review = json.loads((STAGING / "reviewed" / "c01-2026-09-17.json").read_text(encoding="utf-8"))
    assert len(batch) == 27
    assert validate_batch(batch) == []
    assert review["status"] == "approved"
    assert {item["id"] for item in batch} == set(review["recipe_ids"])


def test_c01_正式库已包含本批菜单并保持分类覆盖():
    db = _load_json_db(ROOT / "data" / "recipes.json")
    by_id = {recipe.id: recipe for recipe in db.recipes}
    batch_ids = {f"r{i}" for i in range(101, 128)}
    assert batch_ids <= set(by_id)
    assert len(db.recipes) == 127
    assert sum(by_id[rid].category == "主食" for rid in batch_ids) >= 7
    assert sum(by_id[rid].category == "凉菜" for rid in batch_ids) >= 5
    assert sum(not by_id[rid].all_allergen_names() for rid in batch_ids) >= 16
