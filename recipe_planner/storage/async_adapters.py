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


# ---------------------------------------------------------------- 写入（P1-3）

async def update_result(plan_id: Optional[str], result, change_note: str = "") -> Optional[PlanRecord]:
    if _is_db():
        return await PlanRepo.update_result(plan_id, result, change_note)
    from recipe_planner import store

    return store.update_result(plan_id, result, change_note)


async def set_done(plan_id: Optional[str], day: int, done: bool = True) -> Optional[PlanRecord]:
    if _is_db():
        return await PlanRepo.set_done(plan_id, day, done)
    from recipe_planner import store

    return store.set_done(plan_id, day, done)


async def set_checked(plan_id: Optional[str], names: list[str]) -> Optional[PlanRecord]:
    if _is_db():
        return await PlanRepo.set_checked(plan_id, names)
    from recipe_planner import store

    return store.set_checked(plan_id, names)


async def save_profile(profile: dict) -> None:
    if _is_db():
        await ProfileRepo.save_profile(profile)
        return
    from recipe_planner import profile as profile_mod

    profile_mod.save_profile(profile)


async def clear_profile() -> None:
    if _is_db():
        await ProfileRepo.clear_all()
        return
    from recipe_planner import profile as profile_mod

    profile_mod.clear_all()


async def add_log(plan_id: Optional[str], kind: str, text: str,
                  payload: Optional[dict] = None) -> Optional[int]:
    """写一条操作日志（审计 + 回执追踪）。

    JSON 后端没有 `action_log` 表（docs/08 §3.2 是数据库表），返回 None —— 调用方要能接受。
    """
    if not _is_db():
        return None
    from recipe_planner.storage.repositories import LogRepo

    return await LogRepo.add(plan_id, kind, text, payload)


async def recent_logs(plan_id: Optional[str] = None, limit: int = 12) -> list[dict]:
    if not _is_db():
        return []
    from recipe_planner.storage.repositories import LogRepo

    return await LogRepo.recent(plan_id, limit)
