"""菜谱库加载。

`STORAGE=db` 从数据库读（docs/08 §7）；否则读 `data/recipes.json`。
两种模式返回同一个 `RecipeDB`，调用方（core/界面/测试）无需区分。

`USE_API=1` 时**一律读 `data/recipes.json`**，即使 `STORAGE=db`：
服务化之后界面进程不应该再去碰数据库文件（它是服务端的），而且菜谱目录是
**随应用一起发布的静态资源**，不是用户数据 —— 用户数据（方案/档案/勾选）才走 API。
两边的菜谱 id 同源（库里的菜谱就是从这份 JSON 导进去的），所以名称与 id 对得上。
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
        from recipe_planner.infra import settings

        if settings.use_api():
            return _load_json_db()
        if settings.storage_kind() == "db":
            from recipe_planner.storage import sync_bridge
            from recipe_planner.storage.repositories import RecipeRepo

            return sync_bridge.run(RecipeRepo.load_db())
    return _load_json_db(path)
