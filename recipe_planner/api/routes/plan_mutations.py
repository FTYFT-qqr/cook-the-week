"""方案的单点写入接口（docs/08 §6 / docs/09 P1-3）。

三条规矩（写在最前面，改代码时先读）：

1. **单点改动**（05 §4 R1）：一次请求只动一处 —— 某一天的菜、某一道菜、或档案里的一条。
   测试会对比改动前后的数据库快照，多动了别的地方就是 bug。
2. **响应即回执**：返回 `{kind, message, data, action_log_id, undo_hint, next_steps}`，
   `message` 是人话（05 §5.3 句式），`undo_hint` 是**精确逆操作**。
3. **先落库再回话**：路由在返回前 `await session.commit()`，否则客户端可能看到 200 而库没变
   （Session 中间件是在响应发出之后才提交的）。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends

from recipe_planner import actions
from recipe_planner import profile as prof
from recipe_planner.models import PlanRecord, RecipeDB
from recipe_planner.storage import async_adapters as data
from recipe_planner.storage.engine import session_scope

from ..deps import get_db, get_profile, require_record
from ..errors import InvalidRequestError
from ..schemas import (ChecksIn, DayOut, DayPatchIn, FeedbackIn, MutationOut, RateIn)

router = APIRouter()


async def _commit() -> None:
    """把本次请求的会话提交掉（写接口必须在返回前调用）。"""
    async with session_scope() as session:          # 请求级会话，拿到的是同一个
        await session.commit()


def _envelope(outcome: actions.ActionOutcome, log_id: Optional[int],
              payload: Optional[dict] = None) -> MutationOut:
    return MutationOut(kind=outcome.kind, message=outcome.message,
                       data=payload or {}, action_log_id=log_id,
                       undo_hint=outcome.undo, next_steps=outcome.next_steps)


async def _persist_plan(record: PlanRecord, outcome: actions.ActionOutcome, db: RecipeDB,
                        note: Optional[str] = None) -> Optional[int]:
    """把改动落到库里：菜单/清单/花费重算 → 更新方案 → 记一条操作日志。"""
    if outcome.days is not None or "must_include_recipes" in outcome.extra:
        result = actions.apply_to_result(record, outcome, db)
        await data.update_result(record.id, result, note or "")
    log_id = await data.add_log(record.id, outcome.kind, outcome.message,
                                {**outcome.extra, "undo_hint": outcome.undo or {}})
    await _commit()
    return log_id


@router.patch("/plans/{plan_id}/days/{day}", response_model=MutationOut, tags=["plans"],
              responses={404: {"description": "方案或这一天不存在"},
                         409: {"description": "当前状态下做不了（例如已经是最快的了）"}})
async def patch_day(body: DayPatchIn, day: int, record: PlanRecord = Depends(require_record),
                    db: RecipeDB = Depends(get_db)) -> MutationOut:
    """改这一顿：不做饭 / 改回来 / 换份量 / 换快手组合 / 换一道 / 整组替换 / 标记做完。

    `body.meal` 对所有 op 都有效（docs/10）：一天多顿时"换一道"必须说清是哪一顿，
    否则会去改**早餐**而界面上看不出来。
    """
    op = body.op
    meal = body.meal
    if op == "skip":
        outcome = actions.skip(record, db, day, meal)
    elif op == "restore":
        outcome = actions.restore(record, db, day, meal)
    elif op == "people":
        if body.people is None:
            raise InvalidRequestError("改份量要告诉我这一顿总共几个人。",
                                      next_steps=[{"op": "retry_with_people", "label": "填上人数"}])
        outcome = actions.set_people(record, db, day, body.people, meal)
    elif op == "faster":
        outcome = actions.faster(record, db, day, meal)
    elif op == "swap":
        if not body.recipe_id:
            raise InvalidRequestError("换菜要告诉我换掉哪一道。",
                                      next_steps=[{"op": "view_plan", "label": "看看这一周"}])
        outcome = actions.swap(record, db, day, body.recipe_id, meal)
    elif op == "replace_day":
        outcome = actions.replace_day(record, db, day, list(body.recipe_ids or []), meal)
    elif op == "done":
        outcome = actions.mark_done(record, day, body.done, meal)
        await data.set_done(record.id, day, body.done, body.meal)
        log_id = await data.add_log(record.id, outcome.kind, outcome.message, outcome.extra)
        await _commit()
        return _envelope(outcome, log_id, {"day": day, "done": body.done, "meal": body.meal})
    else:                                                  # pragma: no cover - Literal 兜住了
        raise InvalidRequestError("不认识的改动类型。")

    log_id = await _persist_plan(record, outcome, db)
    updated = await _fresh(record, db)
    # 所有写入接口的 data 形状保持一致：day_detail + 各自的附加字段
    return _envelope(outcome, log_id,
                     {"day_detail": _day_payload(updated or record, day, db, meal), **outcome.extra})


@router.post("/plans/{plan_id}/dishes/{day}/{recipe_id}/feedback", response_model=MutationOut,
             tags=["plans"], responses={404: {"description": "方案或这一天不存在"}})
async def dish_feedback(body: FeedbackIn, day: int, recipe_id: str,
                        record: PlanRecord = Depends(require_record),
                        db: RecipeDB = Depends(get_db),
                        profile: dict = Depends(get_profile)) -> MutationOut:
    """喜欢 / 不喜欢 / 定住 / 取消定住。

    - 喜欢：只写档案，**菜单一行不动**；不喜欢：写档案 + 换掉这一道；
    - 定住：只给这道菜加"必须保留"，其他菜不受影响。
    """
    outcome = actions.feedback(record, db, day, recipe_id, body.op, profile, body.meal)
    new_profile = None

    if body.op in ("like", "dislike"):
        known = {r.name for r in db.recipes}
        action = "like" if body.op == "like" else "dislike"
        new_profile = prof.apply_feedback_to_dict(profile, actions.name_of(db, recipe_id),
                                                 action, known, source="菜单页")
        await data.save_profile(new_profile)

    log_id = await _persist_plan(record, outcome, db)
    payload: dict = {"day_detail": _day_payload(await _fresh(record, db) or record,
                                                day, db, body.meal),
                     **outcome.extra}
    if new_profile is not None:
        payload["profile"] = {"liked_dishes": new_profile.get("liked_dishes", []),
                             "disliked_dishes": new_profile.get("disliked_dishes", []),
                             "signature": prof.profile_signature_of(new_profile)}
    return _envelope(outcome, log_id, payload)


@router.post("/plans/{plan_id}/save-money", response_model=MutationOut, tags=["plans"],
             responses={409: {"description": "已经没有明显更省的换法了"}})
async def save_money(record: PlanRecord = Depends(require_record),
                     db: RecipeDB = Depends(get_db)) -> MutationOut:
    """「哪里能省」：挑最贵的一道换成更便宜的，只动那一道所在的那一天。"""
    outcome = actions.save_money(record, db)
    log_id = await _persist_plan(record, outcome, db)
    updated = await _fresh(record, db)
    payload = dict(outcome.extra)
    payload["day_detail"] = _day_payload(updated or record, outcome.extra.get("day", 1), db,
                                         outcome.extra.get("meal"))
    return _envelope(outcome, log_id, payload)


@router.post("/plans/{plan_id}/rate", response_model=MutationOut, tags=["plans"])
async def rate_day(body: RateIn, record: PlanRecord = Depends(require_record),
                   db: RecipeDB = Depends(get_db),
                   profile: dict = Depends(get_profile)) -> MutationOut:
    """做完了打分：好吃 / 一般 / 下次不做。只写档案 + 这一顿的"做过了"，菜单不变。"""
    outcome = actions.rate(record, db, body.day, body.score, body.meal)
    known = {r.name for r in db.recipes}
    new_profile = profile
    for name in outcome.extra["dishes"]:
        new_profile = prof.apply_rating_to_dict(new_profile, name, body.score, known,
                                               source="做完了打分", today=date.today())
    await data.save_profile(new_profile)
    await data.set_done(record.id, body.day, True, body.meal)
    log_id = await data.add_log(record.id, outcome.kind, outcome.message, outcome.extra)
    await _commit()
    return _envelope(outcome, log_id, {"day": body.day, "score": body.score,
                                       "meal": body.meal, "dishes": outcome.extra["dishes"]})


@router.get("/plans/{plan_id}/shopping/checks", tags=["shopping"])
async def get_checks(record: PlanRecord = Depends(require_record)) -> dict:
    needed = [it.name for it in record.result.shopping if it.needed]
    checked = set(record.checked_items or [])
    return {"names": sorted(checked), "total": len(needed),
            "remaining": [n for n in needed if n not in checked]}


@router.put("/plans/{plan_id}/shopping/checks", response_model=MutationOut, tags=["shopping"])
async def put_checks(body: ChecksIn, record: PlanRecord = Depends(require_record)) -> MutationOut:
    """勾选进度（**幂等**：全量覆盖，重复提交同样的内容结果一样）。"""
    outcome = actions.set_checks(record, body.names)
    await data.set_checked(record.id, sorted(set(body.names)))
    log_id = await data.add_log(record.id, outcome.kind, outcome.message, outcome.extra)
    await _commit()
    return _envelope(outcome, log_id, {"names": sorted(set(body.names)),
                                       "total": outcome.extra["total"]})


async def _fresh(record: PlanRecord, db: RecipeDB) -> Optional[PlanRecord]:
    """改动落库后重新读一遍（回执里的 day_detail 必须是库里的新样子）。"""
    return await data.get_record(record.id)


def _day_payload(record: PlanRecord, day: int, db: RecipeDB,
                 meal: Optional[str] = None) -> dict:
    """改动后这一顿的样子（前端不用再拉整个方案）。"""
    import recipe_planner.reporting as rep

    plan_day = record.result.slot(day, meal)      # docs/10：按天取一顿只有这一条路
    if plan_day is None:
        return {}
    summary = rep.plan_summary(record.result, db, record.start_date)
    row = next((r for r in summary.rows
                if r.day == plan_day.day and r.meal == plan_day.meal), None)
    if row is None:
        return {}
    order, has_slow = rep.cook_order(plan_day, db)
    return DayOut(day=row.day, meal=row.meal, weekday=row.weekday, date_label=row.date_label,
                  skipped=bool(plan_day.skipped),
                  people=plan_day.people,
                  minutes=row.minutes, cost=row.cost,
                  done=record.is_done(row.day, row.meal),
                  dishes=[{"recipe_id": d.recipe_id,
                           "name": (r.name if (r := db.by_id(d.recipe_id)) else ""),
                           "category": r.category if r else "",
                           "difficulty": r.difficulty if r else "",
                           "time_min": r.time_min if r else 0,
                           "reason": d.reason,
                           "locked": d.recipe_id in set(record.result.constraints.must_include_recipes)}
                          for d in plan_day.dishes],
                  cook_order=list(order), has_parallel=has_slow).model_dump()
