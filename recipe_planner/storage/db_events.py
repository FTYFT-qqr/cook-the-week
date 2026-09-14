"""偏好事件的**数据库**实现：与 `events.py` 的 JSON 实现同名同义。

`events.py` 末尾按 `STORAGE=db` 决定是否用这里覆盖 JSON 实现
（与 `store.py`→`db_store.py`、`profile.py`→`db_profile.py` 同一套办法，docs/08 §7）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, select

from recipe_planner.storage import orm, sync_bridge
from recipe_planner.storage.engine import session_scope

run = sync_bridge.run


def events_path() -> Path:
    """数据库后端没有"事件文件" —— 返回一个只用于报错说明的占位路径。"""
    return Path("data/app.db#dish_event")


def _row_to_dict(row: orm.DishEvent) -> dict:
    return {"recipe_id": row.recipe_id, "action": row.action, "meal": row.meal or "",
            "day_no": row.day_no, "plan_id": row.plan_id, "source": row.source or "",
            "created_at": row.created_at.isoformat() if row.created_at else ""}


async def _load() -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(select(orm.DishEvent).order_by(orm.DishEvent.id))
                ).scalars().unique().all()
        return [_row_to_dict(r) for r in rows]


def load_events() -> list[dict]:
    return run(_load())


async def _record(events: list[dict], plan_id: Optional[str] = None) -> int:
    if not events:
        return 0
    # **延迟导入**：repositories 会用到本模块（写事件），本模块又要它的 ensure_household ——
    # 顶层互相 import 会在"谁先被导入"时炸掉（半初始化的模块没有那个名字）。
    from recipe_planner.storage.repositories import ensure_household

    async with session_scope() as s:
        hid = await ensure_household(s)
        for e in events:
            action = str(e.get("action") or "")
            if not action or not e.get("recipe_id"):
                continue
            s.add(orm.DishEvent(
                plan_id=e.get("plan_id") or plan_id, household_id=hid,
                recipe_id=str(e["recipe_id"]), action=action,
                meal=str(e.get("meal") or ""), day_no=e.get("day_no"),
                source=str(e.get("source") or "")[:24]))
        await s.flush()
    return len(events)


def record(events: list[dict], *, now: Optional[datetime] = None,
           plan_id: Optional[str] = None) -> int:
    """`now` 只为了与 JSON 实现同签名（数据库的 `created_at` 由库自己给 server_default）。"""
    return run(_record(events, plan_id))


async def _recent(days: int, today: Optional[date]) -> list[dict]:
    cutoff = (today or date.today()) - timedelta(days=max(1, days))
    async with session_scope() as s:
        rows = (await s.execute(
            select(orm.DishEvent)
            .where(orm.DishEvent.created_at >= cutoff)
            .order_by(orm.DishEvent.created_at, orm.DishEvent.id))
        ).scalars().unique().all()
        return [_row_to_dict(r) for r in rows]


def recent(days: int = 180, *, today: Optional[date] = None) -> list[dict]:
    return run(_recent(days, today))


async def _clear() -> None:
    async with session_scope() as s:
        await s.execute(delete(orm.DishEvent))
        await s.flush()


def clear() -> None:
    run(_clear())
