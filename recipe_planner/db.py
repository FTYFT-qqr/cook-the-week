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
import os
from pathlib import Path

from recipe_planner.models import Recipe, RecipeDB

DEFAULT_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "recipes.json"
# 保留 `DATA_FILE` 作为兼容别名；运行时读取走函数，迁移/测试可安全注入固定快照。
DATA_FILE = DEFAULT_DATA_FILE


def recipes_path() -> Path:
    """菜谱 JSON 路径。

    生产默认仍是 `data/recipes.json`；`RECIPE_DB_FILE` 只给隔离迁移校验和
    测试使用，避免验证脚本为了读 fixture 去碰真实用户数据。
    """
    return Path(os.environ.get("RECIPE_DB_FILE", str(DEFAULT_DATA_FILE)))


def _load_json_db(path: Path | str | None = None) -> RecipeDB:
    p = Path(path) if path else recipes_path()
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


def load_catalog() -> RecipeDB:
    """返回当前运行时菜谱目录。

    Streamlit 界面在 API 模式下通过服务端读取已发布数据库；API worker 自己
    仍使用 ``load_db()`` 读取服务端数据库，避免服务端反过来请求自己。
    ``STORAGE=json`` 仍是显式的本地降级/fixture 开关。
    """
    from recipe_planner.infra import settings

    if settings.use_api():
        from recipe_planner.client import load_recipe_db

        return load_recipe_db()
    return load_db()
