"""方案相关只读接口（docs/08 §6 / docs/09 P1-2）。

路由顺序有讲究：`/plans/current` 必须声明在 `/plans/{plan_id}` **之前**，
否则 "current" 会被当成 plan_id 匹配掉。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from recipe_planner import reporting as rep
from recipe_planner import store
from recipe_planner import tonight as tonight_mod
from recipe_planner.models import PlanRecord, RecipeDB
from recipe_planner.storage import async_adapters as data

from ..deps import get_current_record, get_db, get_records, require_record
from ..errors import NotFoundError, step
from ..schemas import (ConstraintsOut, DayOut, DishOut, IssueOut, PlanDetailOut, PlanListItem,
                       PlanListOut, ShoppingItemOut, SummaryOut, TonightOut)

router = APIRouter()


def _day_plan(record: PlanRecord, day: int):
    return next((p for p in record.result.days if p.day == day), None)


def _dishes_of(record: PlanRecord, day: int, db: RecipeDB) -> list[DishOut]:
    plan_day = _day_plan(record, day)
    if plan_day is None:
        return []
    locked = set(record.result.constraints.must_include_recipes)
    out: list[DishOut] = []
    for dish in plan_day.dishes:
        r = db.by_id(dish.recipe_id)
        out.append(DishOut(
            recipe_id=dish.recipe_id,
            name=r.name if r else "",
            category=r.category if r else "",
            difficulty=r.difficulty if r else "",
            time_min=r.time_min if r else 0,
            reason=dish.reason,
            locked=dish.recipe_id in locked))
    return out


def _issue_out(issue) -> IssueOut:
    return IssueOut(level=issue.level, code=issue.code, message=issue.message,
                    day=issue.day, recipe_id=issue.recipe_id)


def _summary_out(record: PlanRecord, db: RecipeDB) -> SummaryOut:
    s = rep.plan_summary(record.result, db, record.start_date)
    return SummaryOut(
        days=s.days, dishes=s.dishes, total_cost=s.total_cost, budget_total=s.budget_total,
        over_budget=s.over_budget, liked_hit=s.liked_hit, goal=s.goal, goal_hit=s.goal_hit,
        hardest_day=s.hardest_day, hardest_minutes=s.hardest_minutes,
        structure=rep.structure_line(record.result, db),
        warnings=[_issue_out(i) for i in s.warnings])


def _label(record: PlanRecord) -> str:
    return record.label or store.week_label(record.start_date)


def _list_item(record: PlanRecord, db: RecipeDB, current_id: Optional[str]) -> PlanListItem:
    s = rep.plan_summary(record.result, db, record.start_date)
    return PlanListItem(
        id=record.id, label=_label(record), created_at=record.created_at,
        start_date=record.start_date, change_note=record.change_note,
        days=s.days, dishes=s.dishes, total_cost=s.total_cost,
        done_days=list(record.done_days or []), is_current=record.id == current_id)


@router.get("/plans", response_model=PlanListOut, tags=["plans"])
async def list_plans(records: list[PlanRecord] = Depends(get_records),
                     db: RecipeDB = Depends(get_db)) -> PlanListOut:
    """以前的方案（最新在前）。`current_id` = 现在正在用的那一份。"""
    current_id = records[0].id if records else None
    return PlanListOut(items=[_list_item(r, db, current_id) for r in records],
                       total=len(records), current_id=current_id)


@router.get("/plans/current", response_model=TonightOut, tags=["plans"])
async def current_plan(db: RecipeDB = Depends(get_db)) -> TonightOut:
    """今晚页首页数据：状态①–⑤ + 今晚菜/时间/金额/理由/几点能吃上（**没有方案也返回 200**）。

    这里刻意不返回 404：没有方案是正常的"状态④"，不是错误 —— 前端要拿它渲染引导卡。
    """
    record = await data.latest_record()
    payload = tonight_mod.tonight_view(record, db).to_dict()
    payload["plan_id"] = record.id if record else None
    return TonightOut(**payload)


@router.get("/plans/{plan_id}", response_model=PlanDetailOut, tags=["plans"],
            responses={404: {"description": "方案不存在"}})
async def plan_detail(record: PlanRecord = Depends(require_record),
                      db: RecipeDB = Depends(get_db),
                      current: Optional[PlanRecord] = Depends(get_current_record)) -> PlanDetailOut:
    """方案详情：7 天 + 每日菜 + 清单 + 概览 + 结构统计。"""
    result = record.result
    c = result.constraints
    checked = set(record.checked_items or [])
    done = set(record.done_days or [])
    summary = rep.plan_summary(result, db, record.start_date)

    rows = {row["食材"]: row for row in rep.shopping_rows(result, checked)}
    first, second = rep.split_batches(list(rows.values()))
    batch_of = {row["食材"]: 1 for row in first}
    batch_of.update({row["食材"]: 2 for row in second})

    days = [DayOut(day=row.day, weekday=row.weekday, date_label=row.date_label,
                   skipped=bool(dp and dp.skipped),
                   people=dp.people if dp else None,
                   minutes=row.minutes, cost=row.cost, done=row.day in done,
                   dishes=_dishes_of(record, row.day, db))
            for row in summary.rows
            for dp in [_day_plan(record, row.day)]]

    shopping = [
        ShoppingItemOut(name=it.name, category=it.category, amount=it.amount,
                        needed=it.needed, checked=it.name in checked,
                        batch=batch_of.get(it.name) if it.needed else None,
                        optional=bool(rep.optional_hint(rows[it.name])) if it.name in rows else False,
                        for_recipes=list(it.for_recipes))
        for it in result.shopping]

    return PlanDetailOut(
        id=record.id, label=_label(record), created_at=record.created_at,
        start_date=record.start_date, change_note=record.change_note,
        is_current=bool(current and current.id == record.id),
        constraints=ConstraintsOut(
            people=c.people, days=c.days, dishes_per_day=c.dishes_per_day,
            allergens=c.allergens, spice_level=c.spice_level, taste_tags=c.taste_tags,
            goal=c.goal, max_time_min=c.max_time_min, skill=c.skill, cook_start=c.cook_start,
            budget_per_person_day=c.budget_per_person_day, pantry_items=c.pantry_items),
        days=days, shopping=shopping, summary=_summary_out(record, db),
        checked_items=sorted(checked), done_days=sorted(done),
        issues=[_issue_out(i) for i in result.issues])


@router.get("/plans/{plan_id}/days/{day}", response_model=DayOut, tags=["plans"],
            responses={404: {"description": "方案或这一天不存在"}})
async def day_detail(day: int, record: PlanRecord = Depends(require_record),
                     db: RecipeDB = Depends(get_db)) -> DayOut:
    """单独取某一天（局部刷新用），带上「下锅顺序」。"""
    summary = rep.plan_summary(record.result, db, record.start_date)
    row = next((r for r in summary.rows if r.day == day), None)
    if row is None:
        raise NotFoundError("这份方案里没有这一天。", next_steps=[step("view_plan", "看看这一周")])
    plan_day = _day_plan(record, day)
    order, has_slow = rep.cook_order(plan_day, db) if plan_day else ([], False)
    return DayOut(day=row.day, weekday=row.weekday, date_label=row.date_label,
                  skipped=bool(plan_day and plan_day.skipped),
                  people=plan_day.people if plan_day else None,
                  minutes=row.minutes, cost=row.cost,
                  done=day in set(record.done_days or []),
                  dishes=_dishes_of(record, day, db),
                  cook_order=list(order), has_parallel=has_slow)
