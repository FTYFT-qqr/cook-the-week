"""菜谱库加载。"""
from __future__ import annotations

import json
from pathlib import Path

from recipe_planner.models import Recipe, RecipeDB

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "recipes.json"


def load_db(path: Path | str | None = None) -> RecipeDB:
    p = Path(path) if path else DATA_FILE
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    return RecipeDB(recipes=[Recipe(**r) for r in raw.get("recipes", [])])
