"""FastAPI 依赖注入：一次请求共享一份菜谱/方案/档案（docs/08 §2 deps.py）。"""
from __future__ import annotations

from typing import Optional

from recipe_planner.models import PlanRecord, RecipeDB
from recipe_planner.storage import async_adapters as data

from .errors import PlanNotFoundError, step


async def get_db() -> RecipeDB:
    """菜谱库（FastAPI 按依赖缓存，一次请求只读一次）。"""
    return await data.load_db()


async def get_profile() -> dict:
    return await data.load_profile()


async def get_records() -> list[PlanRecord]:
    return await data.load_records()


async def get_current_record() -> Optional[PlanRecord]:
    return await data.latest_record()


async def require_record(plan_id: str) -> PlanRecord:
    record = await data.get_record(plan_id)
    if record is None:
        raise PlanNotFoundError(next_steps=[step("list_plans", "看看还有哪些方案")])
    return record
