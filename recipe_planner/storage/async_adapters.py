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


async def set_done(plan_id: Optional[str], day: int, done: bool = True,
                   meal: Optional[str] = None) -> Optional[PlanRecord]:
    if _is_db():
        return await PlanRepo.set_done(plan_id, day, done, meal)
    from recipe_planner import store

    return store.set_done(plan_id, day, done, meal)


async def set_checked(plan_id: Optional[str], names: list[str]) -> Optional[PlanRecord]:
    if _is_db():
        return await PlanRepo.set_checked(plan_id, names)
    from recipe_planner import store

    return store.set_checked(plan_id, names)


async def save_plan(result, start_date: Optional[str] = None, change_note: str = "",
                    make_active: bool = True) -> PlanRecord:
    """存一份新方案（排菜任务完成后调用）。"""
    if _is_db():
        return await PlanRepo.save_plan(result, start_date, change_note,
                                        make_active=make_active)
    from recipe_planner import store

    return store.save_plan(result, start_date, change_note)


async def delete_record(plan_id: str) -> bool:
    """删掉一份方案（连同它的子表行）；返回是否真删到了。

    JSON 后端的同步门面 `store.delete_record` 不返回东西，所以先看一眼在不在，
    这样两个后端对"删到了没有"的回答是一致的（路由据此区分 404 与成功）。
    """
    if _is_db():
        from recipe_planner.storage.engine import session_scope

        async with session_scope() as s:
            return await PlanRepo.delete(s, plan_id)
    from recipe_planner import store

    existed = store.get_record(plan_id) is not None
    store.delete_record(plan_id)
    return existed


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


# ---------------------------------------------------------------- 任务（P1-5）


class MemoryJobRepo:
    """`STORAGE=json` 时的任务实现：进程内字典（任务本来就短命，重启丢了也无所谓）。

    与 `JobRepo` **同签名**，路由拿到哪个都能用。
    """

    _rows: dict[str, dict] = {}

    @staticmethod
    def _now() -> str:
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M")

    @staticmethod
    async def create(kind: str = "plan_week", request: Optional[dict] = None,
                     job_id: Optional[str] = None) -> dict:
        from uuid import uuid4

        row = {"id": job_id or uuid4().hex[:12], "plan_id": None,
               "household_id": "household_default", "kind": kind, "status": "queued",
               "stage": "queued", "progress": 0.0, "request": request or {},
               "error": None, "created_at": MemoryJobRepo._now(),
               "started_at": "", "finished_at": ""}
        MemoryJobRepo._rows[row["id"]] = row
        return dict(row)

    @staticmethod
    async def get(job_id: str) -> Optional[dict]:
        row = MemoryJobRepo._rows.get(job_id)
        return None if row is None else dict(row)

    @staticmethod
    async def list_recent(limit: int = 10) -> list[dict]:
        rows = sorted(MemoryJobRepo._rows.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    @staticmethod
    async def active_count(household_id: Optional[str] = None, kind: str = "plan_week") -> int:
        return len([r for r in MemoryJobRepo._rows.values()
                    if r["kind"] == kind and r["status"] in ("queued", "running")
                    and (not household_id or r["household_id"] == household_id)])

    @staticmethod
    async def set_status(job_id: str, status: str, *, stage: Optional[str] = None,
                         progress: Optional[float] = None, plan_id: Optional[str] = None,
                         error: Optional[str] = None) -> Optional[dict]:
        row = MemoryJobRepo._rows.get(job_id)
        if row is None:
            return None
        row["status"] = status
        if stage is not None:
            row["stage"] = stage
        if progress is not None:
            row["progress"] = max(0.0, min(1.0, float(progress)))
        if plan_id is not None:
            row["plan_id"] = plan_id
        if error is not None:
            row["error"] = error
        if status == "running" and not row["started_at"]:
            row["started_at"] = MemoryJobRepo._now()
        if status in ("succeeded", "failed", "cancelled"):
            row["finished_at"] = MemoryJobRepo._now()
        return dict(row)


def job_repo():
    """任务仓储：db 模式用数据库表，json 模式用进程内字典（同签名）。"""
    if _is_db():
        from recipe_planner.storage.repositories import JobRepo

        return JobRepo
    return MemoryJobRepo


async def recent_events(days: int = 180) -> list[dict]:
    """最近 N 天的偏好事件（docs/12 阶段二）。

    **路由要算权重就得走这里**：`events.recent()` 是同步门面（内部 `sync_bridge.run`），
    在事件循环里直接调会阻塞循环 —— 与"路由不要调 store/profile 这些同步门面"是同一条纪律。
    """
    if _is_db():
        from recipe_planner.storage import db_events

        return await db_events._recent(days, None)
    from recipe_planner import events as events_mod

    return events_mod.recent(days)


async def commit() -> None:
    """把**本次请求**的会话立刻提交（`session_scope()` 拿到的就是请求级那一个）。

    全项目只有一处该用它，而且理由必须是同一条：**把活儿交给别人之前，数据得先落库**。
    现在的唯一调用点是 `POST /plans` —— 任务行交给执行器之前必须先提交，
    因为执行器在**另一条线程 + 另一个事件循环**里，未提交的写它根本看不见
    （docs/11 §4.1 P0-3 的同一条根因：请求的会话原本是在响应之后才提交的）。

    除此之外，写接口一律**只在请求末尾提交一次**（由 Session 中间件做）——
    中途提交会让"报错但已生效"变成可能：响应 4xx/5xx 时中间件的回滚对已提交的写是空操作。
    """
    from recipe_planner.storage.engine import session_scope

    async with session_scope() as session:
        await session.commit()
