"""菜谱库加载。

`STORAGE=db` 从数据库读（docs/08 §7）；否则读 `data/recipes.json`。
两种模式返回同一个 `RecipeDB`，调用方（core/界面/测试）无需区分。
"""
from __future__ import annotations

import json
from pathlib import Path

from recipe_planner.models import Recipe, RecipeDB

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "recipes.json"


def _load_json_db(path: Path | str | None = None) -> RecipeDB:
    p = Path(path) if path else DATA_FILE
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    return RecipeDB(recipes=[Recipe(**r) for r in raw.get("recipes", [])])


def load_db(path: Path | str | None = None) -> RecipeDB:
    if path is None:
        from recipe_planner.infra.settings import storage_kind

        if storage_kind() == "db":
            from recipe_planner.storage import sync_bridge
            from recipe_planner.storage.repositories import RecipeRepo

            return sync_bridge.run(RecipeRepo.load_db())
    return _load_json_db(path)
