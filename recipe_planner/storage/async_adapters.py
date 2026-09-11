"""服务端用的**异步**数据入口：按 `STORAGE` 分派到数据库或 JSON 文件。

为什么单独一个模块（名字刻意不叫 `adapters.py`，避免和 docs/08 §2 里那个同签名适配层混淆）：
- 界面用的**同步**适配层是 `store.py` / `profile.py` / `db.py` 末尾那几行"同名同义覆盖"；
- 服务端（FastAPI 路由）里不能再调那些同步门面——它们内部会 `sync_bridge.run()`，
  而请求本身就跑在事件循环里，再往循环里塞协程会直接报错（见 sync_bridge 的重入保护）。

所以：**路由一律用这里的 async 函数**，`STORAGE=db|json` 的差异只在这一层。
"""
from __future__ import annotations

from typing import Optional

from recipe_planner.infra.settings import storage_kind
from recipe_planner.models import PlanRecord, RecipeDB
from recipe_planner.storage.repositories import PlanRepo, ProfileRepo, RecipeRepo


def _is_db() -> bool:
    return storage_kind() == "db"


async def load_db() -> RecipeDB:
    if _is_db():
        return await RecipeRepo.load_db()
    from recipe_planner.db import _load_json_db

    return _load_json_db()


async def load_records() -> list[PlanRecord]:
    if _is_db():
        return await PlanRepo.load_records()
    from recipe_planner import store

    return store.load_records()


async def latest_record() -> Optional[PlanRecord]:
    if _is_db():
        return await PlanRepo.latest_record()
    from recipe_planner import store

    return store.latest_record()


async def get_record(plan_id: Optional[str]) -> Optional[PlanRecord]:
    if not plan_id:
        return None
    if _is_db():
        return await PlanRepo.get_record(plan_id)
    from recipe_planner import store

    return store.get_record(plan_id)


async def previous_record(current_id: Optional[str]) -> Optional[PlanRecord]:
    if _is_db():
        return await PlanRepo.previous_record(current_id)
    from recipe_planner import store

    return store.previous_record(current_id)


async def load_profile() -> dict:
    if _is_db():
        return await ProfileRepo.load_profile()
    from recipe_planner import profile as profile_mod

    return profile_mod.load_profile()
