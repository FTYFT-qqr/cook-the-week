"""仓储层（async）：所有数据库读写都从这里走。

设计要点（docs/08 §7）：
- 返回/接收的都是**领域模型**（`RecipeDB / PlanResult / PlanRecord / dict`），ORM 对象不外泄；
- 语义与现有 `db.py / store.py / profile.py` 完全一致，这样适配层可以做到"同名同义"；
- 没有 ORM 关系的地方一律显式按顺序 flush（SQLite 外键是真的开着的）。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recipe_planner.models import (
    ChosenDish,
    DayPlan,
    Ingredient as IngredientModel,
    PlanRecord,
    PlanResult,
    Recipe,
    RecipeDB,
    ShoppingItem,
    UserConstraints,
    ValidationIssue,
)
from recipe_planner.storage import orm
from recipe_planner.storage.engine import session_scope

DEFAULT_HOUSEHOLD = "household_default"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------- 内部工具

async def ensure_household(session: AsyncSession) -> str:
    """P0–P2 只有一个默认家庭（08 §13）。"""
    if await session.get(orm.Household, DEFAULT_HOUSEHOLD) is None:
        session.add(orm.Household(id=DEFAULT_HOUSEHOLD, name="我的家"))
        await session.flush()
    return DEFAULT_HOUSEHOLD


def _week_label(start: date) -> str:
    end = start + timedelta(days=6)
    return f"{start.month}/{start.day}–{end.month}/{end.day}"


def _iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


async def _profile_sig_in_session(s: AsyncSession) -> str:
    """档案指纹（R5 缓存键成分）——注意：必须在同一 session 内算，不能调同步门面（会死锁）。"""
    rows = (await s.execute(select(orm.Preference.recipe_id, orm.Preference.kind))).all()
    return "|".join(sorted(f"{kind}:{rid}" for rid, kind in rows))


# ---------------------------------------------------------------- 菜谱

def _recipe_to_domain(row: orm.Recipe) -> Recipe:
    return Recipe(
        id=row.id, name=row.name, category=row.category, description=row.description,
        difficulty=row.difficulty, time_min=row.time_min, cost_yuan=float(row.cost_yuan),
        calories=row.calories, protein_g=float(row.protein_g) if row.protein_g is not None else None,
        spice_level=row.spice_level, taste_tags=list(row.taste_tags or []),
        goal_tags=list(row.goal_tags or []), allergens=list(row.allergens or []),
        ingredients=[IngredientModel(name=i.name, amount=i.amount, category=i.category,
                                     grams=float(i.grams) if i.grams is not None else None)
                     for i in sorted(row.ingredients, key=lambda x: x.seq)],
    )


class RecipeRepo:
    @staticmethod
    async def load_db() -> RecipeDB:
        async with session_scope() as s:
            rows = (await s.execute(select(orm.Recipe))).scalars().unique().all()
            return RecipeDB(recipes=[_recipe_to_domain(r) for r in rows])

    @staticmethod
    async def upsert_many(recipes: list[Recipe]) -> int:
        """导入/更新菜谱库（幂等：按 id 覆盖）。"""
        async with session_scope() as s:
            for r in recipes:
                row = await s.get(orm.Recipe, r.id)
                if row is None:
                    row = orm.Recipe(id=r.id)
                    s.add(row)
                row.name, row.category, row.description = r.name, r.category, r.description
                row.difficulty, row.time_min = r.difficulty, r.time_min
                row.cost_yuan, row.calories = r.cost_yuan, r.calories
                row.protein_g, row.spice_level = r.protein_g, r.spice_level
                row.taste_tags = list(r.taste_tags)
                row.goal_tags = list(r.goal_tags)
                row.allergens = list(r.allergens)
                await s.flush()
                await s.execute(delete(orm.Ingredient).where(orm.Ingredient.recipe_id == r.id))
                await s.flush()
                for seq, ing in enumerate(r.ingredients, start=1):
                    s.add(orm.Ingredient(recipe_id=r.id, seq=seq, name=ing.name,
                                         amount=ing.amount, category=ing.category, grams=ing.grams))
            await s.flush()
            return len(recipes)


# ---------------------------------------------------------------- 方案

def _plan_to_record(plan: orm.Plan, days: list[orm.PlanDay], checks: list[str],
                    shop_rows: list[orm.ShoppingItem]) -> PlanRecord:
    day_models: list[DayPlan] = []
    for d in sorted(days, key=lambda x: x.day_no):
        dishes = [ChosenDish(recipe_id=x.recipe_id, reason=x.reason)
                  for x in sorted(d.dishes, key=lambda y: y.seq)]
        day_models.append(DayPlan(day=d.day_no, dishes=dishes, skipped=d.skipped,
                                  people=d.people_override))
    c = UserConstraints(**plan.constraints)
    result = PlanResult(constraints=c, candidate_count=0, days=day_models,
                        shopping=[ShoppingItem(name=x.name, category=x.category,
                                               amount=x.amount_text, needed=x.needed,
                                               for_recipes=list(x.used_for or []))
                                  for x in shop_rows],
                        estimated_cost_yuan=0.0, final=True)
    return PlanRecord(id=plan.id, created_at=_iso(plan.created_at),
                      start_date=plan.week_start.isoformat(),
                      label=_week_label(plan.week_start), change_note=plan.change_note,
                      done_days=sorted(d.day_no for d in days if d.done_at is not None),
                      checked_items=sorted(checks), result=result)


async def _load_plan(s: AsyncSession, plan_id: str) -> Optional[orm.Plan]:
    return await s.get(orm.Plan, plan_id)


async def _days_of(s: AsyncSession, plan_id: str) -> list[orm.PlanDay]:
    rows = (await s.execute(select(orm.PlanDay).where(orm.PlanDay.plan_id == plan_id)
                            .order_by(orm.PlanDay.day_no))).scalars().unique().all()
    for row in rows:                     # 显式加载 dishes，避免离开 greenlet 上下文后懒加载
        await s.refresh(row, ["dishes"])
    return list(rows)


async def _record(s: AsyncSession, plan: orm.Plan) -> PlanRecord:
    days = await _days_of(s, plan.id)
    checks = (await s.execute(select(orm.ShoppingCheck.item_name)
                              .where(orm.ShoppingCheck.plan_id == plan.id))).scalars().all()
    shop = (await s.execute(select(orm.ShoppingItem)
                            .where(orm.ShoppingItem.plan_id == plan.id))).scalars().all()
    return _plan_to_record(plan, days, list(checks), list(shop))


class PlanRepo:
    @staticmethod
    async def load_records() -> list[PlanRecord]:
        async with session_scope() as s:
            plans = (await s.execute(select(orm.Plan)
                                     .order_by(orm.Plan.created_at.desc()))).scalars().unique().all()
            return [await _record(s, p) for p in plans]

    @staticmethod
    async def latest_record() -> Optional[PlanRecord]:
        recs = await PlanRepo.load_records()
        return recs[0] if recs else None

    @staticmethod
    async def get_record(plan_id: Optional[str]) -> Optional[PlanRecord]:
        if not plan_id:
            return None
        async with session_scope() as s:
            plan = await _load_plan(s, plan_id)
            return None if plan is None else await _record(s, plan)

    @staticmethod
    async def previous_record(current_id: Optional[str]) -> Optional[PlanRecord]:
        recs = await PlanRepo.load_records()
        if not recs:
            return None
        if current_id is None:
            return recs[0]
        for i, r in enumerate(recs):
            if r.id == current_id:
                return recs[i + 1] if i + 1 < len(recs) else None
        return recs[0]

    @staticmethod
    async def save_plan(result: PlanResult, start_date: Any = None, change_note: str = "",
                        now: Optional[datetime] = None, make_active: bool = True,
                        plan_id: Optional[str] = None) -> PlanRecord:
        from recipe_planner import store as store_mod

        now = now or datetime.now()
        start = store_mod.normalize_start(
            start_date if start_date is not None else store_mod.next_monday(now.date()))
        plan_id = plan_id or uuid4().hex[:8]
        async with session_scope() as s:
            hid = await ensure_household(s)
            if make_active:
                # 决策 4：只允许 1 份 active —— 先把其它置为 archived
                for other in (await s.execute(select(orm.Plan).where(
                        orm.Plan.household_id == hid, orm.Plan.status == "active"))).scalars().all():
                    other.status = "archived"
                await s.flush()
            plan = orm.Plan(id=plan_id, household_id=hid, week_start=start,
                            status="active" if make_active else "archived",
                            constraints=json.loads(result.constraints.model_dump_json()),
                            profile_sig=await _profile_sig_in_session(s),
                            change_note=change_note, created_at=now)
            s.add(plan)
            await s.flush()
            for idx, day in enumerate(result.days):
                s.add(orm.PlanDay(plan_id=plan_id, day_no=day.day,
                                  day_date=start + timedelta(days=idx), skipped=day.skipped,
                                  people_override=day.people))
            await s.flush()
            for day in result.days:
                for seq, dish in enumerate(day.dishes, start=1):
                    s.add(orm.PlanDish(plan_id=plan_id, day_no=day.day, seq=seq,
                                       recipe_id=dish.recipe_id, reason=dish.reason,
                                       locked=dish.recipe_id in set(result.constraints.must_include_recipes)))
            for it in result.shopping:
                s.add(orm.ShoppingItem(plan_id=plan_id, name=it.name, category=it.category,
                                       amount_text=it.amount, needed=it.needed,
                                       used_for=list(it.for_recipes)))
            await s.flush()
            return await _record(s, plan)

    @staticmethod
    async def update_result(plan_id: Optional[str], result: PlanResult,
                            change_note: str = "") -> Optional[PlanRecord]:
        if not plan_id:
            return None
        async with session_scope() as s:
            plan = await _load_plan(s, plan_id)
            if plan is None:
                return None
            plan.constraints = json.loads(result.constraints.model_dump_json())
            if change_note:
                plan.change_note = change_note
            locked = set(result.constraints.must_include_recipes)
            # 天：更新而不是重建，保住 done_at
            existing_days = {d.day_no: d for d in await _days_of(s, plan_id)}
            for day in result.days:
                row = existing_days.get(day.day)
                if row is None:
                    row = orm.PlanDay(plan_id=plan_id, day_no=day.day,
                                      day_date=plan.week_start + timedelta(days=day.day - 1))
                    s.add(row)
                row.skipped, row.people_override = day.skipped, day.people
            for day_no in list(existing_days):
                if day_no not in {d.day for d in result.days}:
                    await s.delete(existing_days[day_no])
            await s.flush()
            await s.execute(delete(orm.PlanDish).where(orm.PlanDish.plan_id == plan_id))
            await s.flush()
            for day in result.days:
                for seq, dish in enumerate(day.dishes, start=1):
                    s.add(orm.PlanDish(plan_id=plan_id, day_no=day.day, seq=seq,
                                       recipe_id=dish.recipe_id, reason=dish.reason,
                                       locked=dish.recipe_id in locked))
            await s.execute(delete(orm.ShoppingItem).where(orm.ShoppingItem.plan_id == plan_id))
            await s.flush()
            for it in result.shopping:
                s.add(orm.ShoppingItem(plan_id=plan_id, name=it.name, category=it.category,
                                       amount_text=it.amount, needed=it.needed,
                                       used_for=list(it.for_recipes)))
            await s.flush()
            return await _record(s, plan)

    @staticmethod
    async def delete_record(plan_id: str) -> None:
        async with session_scope() as s:
            plan = await _load_plan(s, plan_id)
            if plan is not None:
                await s.delete(plan)

    @staticmethod
    async def delete(session: AsyncSession, plan_id: str) -> bool:
        """删掉一份方案，**显式**连子表一起删；返回是否真删到了。

        为什么要一行行显式删，而不是 `session.delete(plan)` 或指望 `ON DELETE CASCADE`：
        - SQLite 的外键约束**默认是关的**（本项目的 engine 里显式 `pragma foreign_keys=ON`
          才有效），换到没开约束的环境就会留下一堆孤儿行（`plan_day` / 菜 / 清单 / 勾选 / 流水）；
        - 显式删除与约束开不开无关，行为在任何后端都一样。

        删除顺序按外键依赖从下往上：菜 → 天 → 清单 → 勾选 → 操作日志 → 方案本身。
        """
        plan = await _load_plan(session, plan_id)
        if plan is None:
            return False
        await session.execute(delete(orm.PlanDish).where(orm.PlanDish.plan_id == plan_id))
        await session.execute(delete(orm.PlanDay).where(orm.PlanDay.plan_id == plan_id))
        await session.execute(delete(orm.ShoppingItem).where(orm.ShoppingItem.plan_id == plan_id))
        await session.execute(delete(orm.ShoppingCheck).where(orm.ShoppingCheck.plan_id == plan_id))
        await session.execute(delete(orm.ActionLog).where(orm.ActionLog.plan_id == plan_id))
        await session.flush()
        result = await session.execute(delete(orm.Plan).where(orm.Plan.id == plan_id))
        await session.flush()
        return bool(result.rowcount)

    @staticmethod
    async def set_done(plan_id: Optional[str], day: int, done: bool = True) -> Optional[PlanRecord]:
        if not plan_id:
            return None
        async with session_scope() as s:
            row = await s.get(orm.PlanDay, (plan_id, day))
            if row is None:
                return None
            row.done_at = datetime.now(timezone.utc) if done else None
            await s.flush()
            plan = await _load_plan(s, plan_id)
            return await _record(s, plan)

    @staticmethod
    async def set_checked(plan_id: Optional[str], names: list[str]) -> Optional[PlanRecord]:
        if not plan_id:
            return None
        async with session_scope() as s:
            await s.execute(delete(orm.ShoppingCheck).where(orm.ShoppingCheck.plan_id == plan_id))
            await s.flush()
            for name in sorted(set(names)):
                s.add(orm.ShoppingCheck(plan_id=plan_id, item_name=name))
            await s.flush()
            plan = await _load_plan(s, plan_id)
            return await _record(s, plan)

    @staticmethod
    async def archive_old(weeks: int = 12) -> int:
        """决策 3：超过 N 周的老方案置 archived（不删数据）。"""
        cutoff = date.today() - timedelta(weeks=weeks)
        async with session_scope() as s:
            rows = (await s.execute(select(orm.Plan).where(
                orm.Plan.week_start < cutoff, orm.Plan.status == "active"))).scalars().all()
            for r in rows:
                r.status = "archived"
            await s.flush()
            return len(rows)


# ---------------------------------------------------------------- 档案（偏好 / 评分）

def _profile_dict(rows: list[orm.Preference], ratings: list[orm.Rating],
                  recipes_by_id: dict[str, orm.Recipe], customer_name: str = "默认客户") -> dict:
    liked, disliked, history, rating_map = [], [], {}, {}
    for p in rows:
        r = recipes_by_id.get(p.recipe_id)
        if r is None:
            continue                                    # 菜谱库变小时静默忽略（与 JSON 版一致）
        (liked if p.kind == "like" else disliked).append(r.name)
        history[r.name] = {"since": p.created_at.strftime("%m/%d") if p.created_at else "",
                           "source": p.source}
    for rt in ratings:
        r = recipes_by_id.get(rt.recipe_id)
        if r is not None:
            rating_map[r.name] = {"score": rt.score,
                                  "date": rt.created_at.strftime("%m/%d") if rt.created_at else ""}
    return {"customer_name": customer_name, "liked_dishes": liked,
            "disliked_dishes": disliked, "history": history, "ratings": rating_map}


class ProfileRepo:
    @staticmethod
    async def _names(s: AsyncSession) -> tuple[dict[str, orm.Recipe], dict[str, str]]:
        rows = (await s.execute(select(orm.Recipe))).scalars().unique().all()
        return {r.id: r for r in rows}, {r.name: r.id for r in rows}

    @staticmethod
    async def load_profile() -> dict:
        async with session_scope() as s:
            prefs = (await s.execute(select(orm.Preference))).scalars().all()
            ratings = (await s.execute(select(orm.Rating))).scalars().all()
            by_id, _ = await ProfileRepo._names(s)
            return _profile_dict(list(prefs), list(ratings), by_id)

    @staticmethod
    async def save_profile(profile: dict) -> None:
        """把完整档案字典写回库（与 JSON 版同义：整体覆盖）。"""
        async with session_scope() as s:
            by_id, name2id = await ProfileRepo._names(s)
            hid = await ensure_household(s)
            await s.execute(delete(orm.Preference))
            await s.execute(delete(orm.Rating))
            await s.flush()
            history = profile.get("history") or {}
            for kind, key in (("like", "liked_dishes"), ("dislike", "disliked_dishes")):
                for name in profile.get(key) or []:
                    rid = name2id.get(name)
                    if rid is None:
                        continue
                    src = (history.get(name) or {}).get("source", "口味档案")
                    s.add(orm.Preference(household_id=hid, recipe_id=rid, kind=kind, source=src))
            for name, info in (profile.get("ratings") or {}).items():
                rid = name2id.get(name)
                if rid is not None:
                    s.add(orm.Rating(household_id=hid, recipe_id=rid,
                                     score=int(info.get("score", 1))))
            await s.flush()

    @staticmethod
    async def clear_all() -> None:
        async with session_scope() as s:
            await s.execute(delete(orm.Preference))
            await s.execute(delete(orm.Rating))
            await s.flush()


# ---------------------------------------------------------------- 改动流水

class LogRepo:
    @staticmethod
    async def add(plan_id: Optional[str], kind: str, text: str,
                  payload: Optional[dict] = None) -> int:
        async with session_scope() as s:
            hid = await ensure_household(s)
            row = orm.ActionLog(plan_id=plan_id, household_id=hid, kind=kind, text=text,
                                payload=payload or {})
            s.add(row)
            await s.flush()
            return int(row.id)

    @staticmethod
    async def recent(plan_id: Optional[str] = None, limit: int = 12) -> list[dict]:
        async with session_scope() as s:
            q = select(orm.ActionLog).order_by(orm.ActionLog.id.desc()).limit(limit)
            if plan_id:
                q = q.where(orm.ActionLog.plan_id == plan_id)
            rows = (await s.execute(q)).scalars().all()
            return [{"id": r.id, "kind": r.kind, "text": r.text, "at": _iso(r.created_at)}
                    for r in rows]


# ---------------------------------------------------------------- 任务（P1-5）
#
# 状态机（docs/08 §6）：queued → running → succeeded | failed | cancelled
# 返回的是**纯字典**：任务会被后台线程读写、还要跨线程传，ORM 对象不能外泄。

JOB_KINDS = ("plan_week",)
ACTIVE_STATUSES = ("queued", "running")


def _job_dict(row: orm.Job) -> dict:
    return {
        "id": row.id,
        "plan_id": row.plan_id,
        "household_id": row.household_id,
        "kind": row.kind,
        "status": row.status,
        "stage": row.stage or "",
        "progress": float(row.progress or 0),
        "request": dict(row.request or {}),
        "error": row.error,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
    }


def _now() -> datetime:
    return datetime.now()


class JobRepo:
    """排菜任务（数据库实现）。

    `error` 列是 Text，里面存的是 JSON：`{"code","message","next_steps"}` ——
    docs/08 §3.2 的 DDL 没有单独的 code/next_steps 列，而这两样前端要用
    （05 §5.1 失败必须给可点击的补救项），所以塞进这一个人话字段里，读的时候再解开。
    """

    @staticmethod
    async def create(kind: str = "plan_week", request: Optional[dict] = None,
                     job_id: Optional[str] = None) -> dict:
        async with session_scope() as s:
            hid = await ensure_household(s)
            row = orm.Job(id=job_id or uuid4().hex[:12], household_id=hid, kind=kind,
                          status="queued", stage="queued", progress=0,
                          request=request or {}, created_at=_now())
            s.add(row)
            await s.flush()
            return _job_dict(row)

    @staticmethod
    async def get(job_id: str) -> Optional[dict]:
        async with session_scope() as s:
            row = await s.get(orm.Job, job_id)
            return None if row is None else _job_dict(row)

    @staticmethod
    async def list_recent(limit: int = 10) -> list[dict]:
        async with session_scope() as s:
            rows = (await s.execute(select(orm.Job)
                                    .order_by(orm.Job.created_at.desc()).limit(limit))
                    ).scalars().all()
            return [_job_dict(r) for r in rows]

    @staticmethod
    async def active_count(household_id: Optional[str] = None, kind: str = "plan_week") -> int:
        """还有几个没跑完的（用来算排队位置、实现"同 household 并发上限 1"）。"""
        async with session_scope() as s:
            q = select(func.count()).select_from(orm.Job).where(
                orm.Job.kind == kind, orm.Job.status.in_(ACTIVE_STATUSES))
            if household_id:
                q = q.where(orm.Job.household_id == household_id)
            return int((await s.execute(q)).scalar() or 0)

    @staticmethod
    async def set_status(job_id: str, status: str, *, stage: Optional[str] = None,
                         progress: Optional[float] = None,
                         plan_id: Optional[str] = None,
                         error: Optional[str] = None) -> Optional[dict]:
        async with session_scope() as s:
            row = await s.get(orm.Job, job_id)
            if row is None:
                return None
            row.status = status
            if stage is not None:
                row.stage = stage
            if progress is not None:
                row.progress = max(0.0, min(1.0, float(progress)))
            if plan_id is not None:
                row.plan_id = plan_id
            if error is not None:
                row.error = error
            if status == "running" and row.started_at is None:
                row.started_at = _now()
            if status in ("succeeded", "failed", "cancelled"):
                row.finished_at = _now()
            await s.flush()
            return _job_dict(row)
