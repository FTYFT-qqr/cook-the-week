"""方案相关接口（docs/08 §6 / docs/09 P1-2、P1-6）。

路由顺序有讲究：`/plans/current` 必须声明在 `/plans/{plan_id}` **之前**，
否则 "current" 会被当成 plan_id 匹配掉。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from recipe_planner import reporting as rep
from recipe_planner import store
from recipe_planner import tonight as tonight_mod
from recipe_planner.models import PlanRecord, RecipeDB
from recipe_planner.storage import async_adapters as data

from ..deps import get_current_record, get_db, get_records, require_record
from ..errors import ConfirmRequiredError, InvalidRequestError, NotFoundError, step
from ..schemas import (ConstraintsOut, DayOut, DishOut, IssueOut, MutationOut, PlanDetailOut,
                       PlanListItem, PlanListOut, ShoppingItemOut, SummaryOut, TonightOut)

router = APIRouter()


async def _commit() -> None:
    """写接口必须在返回前提交（Session 中间件是响应发出**之后**才提交的）。"""
    from recipe_planner.storage.engine import session_scope

    async with session_scope() as session:          # 请求级会话，拿到的是同一个
        await session.commit()


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


def _tonight_payload(record: Optional[PlanRecord], db: RecipeDB,
                     day: Optional[int]) -> TonightOut:
    """「今晚」页的响应体（`/plans/current` 与 `/plans/{id}/tonight` 共用，判定只有一份）。"""
    payload = tonight_mod.tonight_view(record, db, day=day).to_dict()
    payload["plan_id"] = record.id if record else None
    return TonightOut(**payload)


@router.get("/plans/current", response_model=TonightOut, tags=["plans"])
async def current_plan(db: RecipeDB = Depends(get_db),
                       day: Optional[int] = Query(None, ge=1, le=7,
                                                  description="手动切到第几天看")) -> TonightOut:
    """今晚页首页数据：状态①–⑤ + 今晚菜/时间/金额/理由/几点能吃上（**没有方案也返回 200**）。

    这里刻意不返回 404：没有方案是正常的"状态④"，不是错误 —— 前端要拿它渲染引导卡。
    `day` 是"手动切到第 N 天"：状态判定仍然只有 `tonight` 这一份实现，界面不用自己再推一遍。
    """
    return _tonight_payload(await data.latest_record(), db, day)


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

    days = [DayOut(day=row.day, meal=row.meal, weekday=row.weekday, date_label=row.date_label,
                   skipped=bool(dp and dp.skipped),
                   people=dp.people if dp else None,
                   minutes=row.minutes, cost=row.cost, done=record.is_done(row.day, row.meal),
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
            budget_per_person_day=c.budget_per_person_day, pantry_items=c.pantry_items,
            must_include_recipes=list(c.must_include_recipes or []),
            meals=list(c.active_meals()), dishes_per_meal=dict(c.dishes_per_meal or {}),
            breakfast_max_time_min=c.breakfast_max_time_min),
        days=days, shopping=shopping, summary=_summary_out(record, db),
        checked_items=sorted(checked), done_days=sorted(done),
        issues=[_issue_out(i) for i in result.issues])


@router.get("/plans/{plan_id}/tonight", response_model=TonightOut, tags=["plans"],
            responses={404: {"description": "方案不存在"}})
async def plan_tonight(record: PlanRecord = Depends(require_record),
                       db: RecipeDB = Depends(get_db),
                       day: Optional[int] = Query(None, ge=1, le=7,
                                                  description="手动切到第几天看")) -> TonightOut:
    """**指定某一版方案**的「今晚」（界面"切到以前的某一版"之后要看的就是它）。

    与 `/plans/current` 是同一份判定（`tonight_view`），只是看的是旧那一版，
    所以响应形状完全一样，`plan_id` 是这一版自己的 id。
    """
    return _tonight_payload(record, db, day)


@router.delete("/plans/{plan_id}", response_model=MutationOut, tags=["plans"],
               responses={404: {"description": "方案不存在"},
                          409: {"description": "缺 confirm=true"}})
async def delete_plan(plan_id: str, confirm: bool = Query(False, description="必须显式传 true"),
                      record: PlanRecord = Depends(require_record)) -> MutationOut:
    """删掉这一版方案。**破坏性操作，必须二次确认**；子表（天/菜/清单/勾选/流水）一起删掉。

    删除后无法找回，所以 `undo_hint` 给 `None`（见下）；`data.remaining` 是删完之后还剩几份方案。
    """
    if not confirm:
        raise ConfirmRequiredError(
            "删除后无法找回，其它版本不受影响。要删就带上 confirm=true。",
            next_steps=[step("view_plan", "先看看这一周"),
                        step("cancel", "算了，先不删")])
    label = _label(record)
    await data.delete_record(plan_id)
    remaining = len(await data.load_records())
    log_id = await data.add_log(None, "plan_delete", f"已删除「{label}」这一版方案",
                                {"deleted_id": plan_id, "remaining": remaining})
    await _commit()
    # undo_hint=None 是**故意的**：删掉的数据找不回来（子表也一起没了），
    # 给一个假的"可以撤销"比没有更糟 —— 用户点了才发现恢复不了。
    return MutationOut(
        kind="plan_delete",
        message=f"已删除「{label}」这一版方案（其它版本没动，删除后无法找回）。",
        data={"deleted_id": plan_id, "remaining": remaining},
        action_log_id=log_id, undo_hint=None,
        next_steps=[step("list_plans", "看看还有哪些方案"),
                    step("create_plan", "重新排一周")])


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


EXPORT_FORMATS = {
    "csv": ("text/csv; charset=utf-8", "买菜清单"),
    "txt": ("text/plain; charset=utf-8", "买菜清单"),
    "html": ("text/html; charset=utf-8", "本周晚餐菜单"),
}


def _download_name(start_date: str, fmt: str) -> str:
    """下载文件名：用 ISO 日期，别用 "9/14–9/20" 这种带斜杠的周标签。

    斜杠在 Windows 上是非法文件名字符，浏览器只能自己替换 —— 与其指望它，
    不如给一个到哪都能存的文件名。中文要按 RFC 5987 编码，否则乱码。
    """
    day = (start_date or "").replace("-", "")[:8] or "本周"
    base = f"{EXPORT_FORMATS[fmt][1]}-{day}.{fmt}"
    return f"attachment; filename*=UTF-8''{quote(base)}"


@router.get("/plans/{plan_id}/export/{fmt}", tags=["plans"],
            responses={200: {"content": {"text/csv": {}, "text/plain": {}, "text/html": {}}},
                       404: {"description": "方案不存在"},
                       422: {"description": "不支持的格式"}})
async def export_plan(fmt: str, record: PlanRecord = Depends(require_record),
                      db: RecipeDB = Depends(get_db)) -> Response:
    """带走这一周：`csv`（买菜清单，Excel 打开不乱码）/ `txt`（贴微信或备忘录）/ `html`（A4 打印）。

    内容与界面里的「带走清单」**同一个来源**（`reporting`），所以从哪个口子拿走都一样。
    """
    fmt = fmt.lower()
    if fmt not in EXPORT_FORMATS:
        raise InvalidRequestError(
            f"「{fmt}」这种格式我这边没有。",
            next_steps=[step("pick_format", "可选：" + "、".join(EXPORT_FORMATS))])
    media_type, _ = EXPORT_FORMATS[fmt]
    checked = set(record.checked_items or [])

    if fmt == "csv":
        body = rep.shopping_csv(record.result, checked)
    elif fmt == "txt":
        body = rep.printable_text(record.result, db, record.start_date, checked)
    else:
        body = rep.printable_document(record.result, db, record.start_date, checked)

    return Response(content=body, media_type=media_type,
                    headers={"Content-Disposition": _download_name(record.start_date, fmt)})


@router.get("/plans/{plan_id}/share", tags=["plans"])
async def share_plan(record: PlanRecord = Depends(require_record),
                     db: RecipeDB = Depends(get_db)) -> dict:
    """分享给家人的干净文本（E-08）：只有日期、菜名、时间、金额，没有按钮和技术字样。"""
    return {"text": rep.share_text(record.result, db, record.start_date),
            "label": store.week_label(record.start_date)}
