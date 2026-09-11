"""单点改动：一次改一处 + 一句回执 + 一条撤销线索（docs/05 §4 R1、§5.3）。

为什么单独一层：docs/08 §5 要求"成功/失败/空/加载四类状态的措辞由后端产出，前端只负责展示与点击"。
所以"改了什么、没动什么"的句子必须和判定放在一起，且**只有一份**。
界面（P1-7 切到 API 后）与 API 都用这里的函数，不再各写一遍文案。

三条硬约束，改代码时必须守住：
1. **单点**：每个函数只允许动它自己那一天的菜（或档案里的一条），其余天原样返回；
2. **回执**：`message` 用 05 §5.3 的句式「已<做了什么>，<哪里没动>」；
3. **可撤销**：`undo` 给出精确的逆操作（用 `replace_day` + 原来的菜，而不是"再换一次"，
   因为再换一次不一定换回原来那道）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from recipe_planner.core import (cheapest_swap, fastest_day, refresh_result, restore_day,
                                 skip_day, swap_dish)
from recipe_planner.models import ChosenDish, DayPlan, PlanRecord, PlanResult, RecipeDB

MAX_UNDO_SNAPSHOT = 7          # 一周最多 7 天


class ActionError(Exception):
    """领域层的"这件事做不了"：人话 + 可点击的下一步（由 api/errors.py 转成 problem+json）。"""

    status = 409

    def __init__(self, code: str, message: str, next_steps: Optional[list[dict]] = None,
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_steps = next_steps or []
        if status is not None:
            self.status = status


@dataclass
class ActionOutcome:
    """一次单点改动的结果：新菜单 / 新档案 / 回执 / 逆操作。"""

    kind: str                                  # swap / like / dislike / save / info …
    message: str
    days: Optional[list[DayPlan]] = None       # None = 菜单没动
    profile: Optional[dict] = None             # None = 档案没动
    extra: dict = field(default_factory=dict)  # 例如 saving、replaced_by
    undo: Optional[dict] = None               # 精确逆操作
    next_steps: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- 小工具

def _day_of(record: PlanRecord, day: int) -> DayPlan:
    plan_day = next((p for p in record.result.days if p.day == day), None)
    if plan_day is None:
        raise ActionError("day_not_found", f"这份方案里没有第 {day} 天。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=404)
    return plan_day


def day_plan_of(days: list[DayPlan], day: int) -> DayPlan:
    return next((p for p in days if p.day == day), days[0])


def name_of(db: RecipeDB, recipe_id: str) -> str:
    """菜名（找不到就给一句能看的占位，不抛错 —— 回执文案不该因为脏数据崩掉）。"""
    return _name(db, recipe_id)


def _name(db: RecipeDB, recipe_id: str) -> str:
    r = db.by_id(recipe_id)
    return r.name if r else "这道菜"


def _other_days_text(total: int, day: int) -> str:
    """"其他六天没动" —— 几天要说清楚，不能含糊成"其余未变"。"""
    n = max(0, total - 1)
    return f"其他 {n} 天没动" if n else "只有这一天"


def _undo_days(record: PlanRecord, days: list[DayPlan]) -> list[DayPlan]:
    """给撤销用：把改动前的这些天原样存下来。"""
    return [p.model_copy(deep=True) for p in days[:MAX_UNDO_SNAPSHOT]]


def _replace_day_undo(plan_id: str, day: int, previous: list[DayPlan]) -> dict:
    """精确逆操作：把这一天恢复成改动前的样子（比"再换一次"可靠）。"""
    return {"label": "撤销", "method": "PATCH", "path": f"/api/v1/plans/{plan_id}/days/{day}",
            "body": {"op": "replace_day",
                     "recipe_ids": [d.recipe_id for p in previous if p.day == day
                                    for d in p.dishes]}}


def _relax_steps(reason: str) -> list[dict]:
    """换不动时给可点击的放宽项（05 §5.1：失败必须给补救项）。"""
    return [{"op": "relax_time", "label": "时长上限放宽到 60 分钟"},
            {"op": "relax_budget", "label": "预算放宽一点"},
            {"op": "keep_rest", "label": "保留其余各天，只重排这一天"}]


def _extra_shopping(day_plan: DayPlan, db: RecipeDB, extra_people: int) -> list[str]:
    """「来客人了」的临时补买清单：只算今晚这几道菜多出来的份量。"""
    out: list[str] = []
    for d in day_plan.dishes:
        r = db.by_id(d.recipe_id)
        if r is None:
            continue
        for ing in r.ingredients:
            if ing.grams and ing.category not in {"调料", "其他"}:
                grams = ing.grams * extra_people / 2.0
                if grams >= 50:
                    out.append(f"{ing.name} 多买约 {grams:.0f} 克")
    return out[:6]


def _replaced_names(db: RecipeDB, before: list[DayPlan], after: list[DayPlan]) -> tuple[str, str]:
    """改动前后差在哪（用于回执里的人话）。"""
    old_ids = [d.recipe_id for p in before for d in p.dishes]
    new_ids = [d.recipe_id for p in after for d in p.dishes]
    old = next((i for i in old_ids if i not in new_ids), "")
    new = next((i for i in new_ids if i not in old_ids), "")
    return _name(db, old) if old else "", _name(db, new) if new else ""


# ---------------------------------------------------------------- 改「这一天」

def skip(record: PlanRecord, db: RecipeDB, day: int) -> ActionOutcome:
    """这天不做饭：清空当天菜，不计花费、不进清单。"""
    plan_day = _day_of(record, day)
    if plan_day.skipped:
        raise ActionError("already_skipped", f"第 {day} 天本来就没安排做饭。")
    before = _undo_days(record, record.result.days)
    dropped = [_name(db, d.recipe_id) for d in plan_day.dishes]
    days = skip_day(record.result.days, day)
    msg = f"已把第 {day} 天标记成不做饭"
    if dropped:
        msg += f"（去掉「{'、'.join(dropped)}」）"
    msg += f"，{_other_days_text(len(record.result.days), day)}。"
    return ActionOutcome(
        kind="skip", message=msg, days=days,
        extra={"skipped_day": day, "dropped": dropped},
        undo={**_replace_day_undo(record.id, day, before), "label": "撤销：改回来做"})


def restore(record: PlanRecord, db: RecipeDB, day: int) -> ActionOutcome:
    """改回来做：重新给这天挑菜，不动其他天。"""
    plan_day = _day_of(record, day)
    if not plan_day.skipped:
        raise ActionError("not_skipped", f"第 {day} 天本来就在做饭。")
    before = _undo_days(record, record.result.days)
    days = restore_day(record.result.days, day, db, record.result.constraints)
    picked = [_name(db, d.recipe_id) for p in days if p.day == day for d in p.dishes]
    msg = (f"已把第 {day} 天恢复做饭（{'、'.join(picked) or '暂无可排的菜'}），"
           f"{_other_days_text(len(days), day)}。")
    return ActionOutcome(
        kind="restore", message=msg, days=days, extra={"restored_day": day, "dishes": picked},
        undo=_replace_day_undo(record.id, day, before))


def set_people(record: PlanRecord, db: RecipeDB, day: int, people: int) -> ActionOutcome:
    """来客人了：只改这一天的份量，其他天不变。

    `people` 是**这一天的总人数**（绝对值，不搞"多加几人"的相对语义 —— 接口要一次说清）。
    """
    plan_day = _day_of(record, day)
    base = record.result.constraints.people
    if people < 1:
        raise ActionError("invalid_people", "人数至少要是 1 个人。", status=422)
    before = _undo_days(record, record.result.days)
    days = [p.model_copy(deep=True) for p in record.result.days]
    for p in days:
        if p.day == day:
            p.people = None if people == base else people
    extra = max(0, people - base)
    extra_items = _extra_shopping(day_plan_of(days, day), db, extra) if extra else []
    if people == base:
        msg = f"已把第 {day} 天改回按 {base} 人算，{_other_days_text(len(days), day)}。"
    else:
        msg = f"已把第 {day} 天按 {people} 人算（多 {extra} 人），{_other_days_text(len(days), day)}。"
    if extra_items:
        msg += "临时要补买：" + "、".join(extra_items) + "。"
    return ActionOutcome(
        kind="people", message=msg, days=days,
        extra={"day": day, "people": people, "extra_shopping": extra_items},
        undo={"label": "撤销", "method": "PATCH",
              "path": f"/api/v1/plans/{record.id}/days/{day}",
              "body": {"op": "people", "people": base}})


def faster(record: PlanRecord, db: RecipeDB, day: int) -> ActionOutcome:
    """回家晚了：只把这一天换成最快能做完的组合。"""
    _day_of(record, day)
    c = record.result.constraints
    got = fastest_day(record.result.days, day, db, c)
    if got is None:
        raise ActionError("already_fastest", "这一天已经是最快的组合了，想更快得放宽一点。",
                          _relax_steps("time"), status=409)
    new_days, recipes = got
    before = _undo_days(record, record.result.days)
    minutes = sum(r.time_min for r in recipes)
    names = "、".join(r.name for r in recipes)
    msg = (f"已把第 {day} 天换成快手组合：「{names}」，约 {minutes} 分钟就能上桌，"
           f"{_other_days_text(len(new_days), day)}。")
    return ActionOutcome(
        kind="faster", message=msg, days=new_days,
        extra={"day": day, "dishes": [r.name for r in recipes], "minutes": minutes},
        undo=_replace_day_undo(record.id, day, before))


def swap(record: PlanRecord, db: RecipeDB, day: int, recipe_id: str) -> ActionOutcome:
    """换一道：只换这一天的这一道。"""
    plan_day = _day_of(record, day)
    if recipe_id not in [d.recipe_id for d in plan_day.dishes]:
        raise ActionError("dish_not_in_day", f"第 {day} 天没有这道菜。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=404)
    old_name = _name(db, recipe_id)
    before = _undo_days(record, record.result.days)
    new_days, new_recipe = swap_dish(record.result.days, day, recipe_id, db,
                                     record.result.constraints)
    if new_recipe is None:
        raise ActionError(
            "no_replacement",
            f"这一周没有能替换「{old_name}」的菜了。点下面的放宽项，我马上重排一版。",
            _relax_steps("no_candidate"))
    msg = f"已把第 {day} 天的「{old_name}」换成「{new_recipe.name}」，{_other_days_text(len(new_days), day)}。"
    return ActionOutcome(
        kind="swap", message=msg, days=new_days,
        extra={"day": day, "from": old_name, "to": new_recipe.name},
        undo=_replace_day_undo(record.id, day, before))


def replace_day(record: PlanRecord, db: RecipeDB, day: int,
                recipe_ids: list[str]) -> ActionOutcome:
    """把这一天整组换成指定的菜 —— 撤销用的精确逆操作，也是"换一整天"的底层。

    只动这一天：其他天的菜、清单里与其他天相关的项都由 `refresh_result` 重算，不由这里手工拼。
    """
    _day_of(record, day)
    known = {r.id for r in db.recipes}
    unknown = [rid for rid in recipe_ids if rid not in known]
    if unknown:
        raise ActionError("unknown_recipe", "要换的菜里有不认识的，可能菜谱库变了。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=422)
    if len(set(recipe_ids)) != len(recipe_ids):
        raise ActionError("duplicate_recipe", "同一天里不能重复同一道菜。", status=422)
    before = _undo_days(record, record.result.days)
    old_reasons = {d.recipe_id: d.reason for p in record.result.days for d in p.dishes}
    days = [p.model_copy(deep=True) for p in record.result.days]
    for p in days:
        if p.day == day:
            p.skipped = not recipe_ids
            p.dishes = [ChosenDish(recipe_id=rid, reason=old_reasons.get(rid, "按你的要求换的"))
                        for rid in recipe_ids]
    names = [_name(db, rid) for rid in recipe_ids]
    return ActionOutcome(
        kind="replace_day",
        message=(f"已把第 {day} 天换成「{'、'.join(names)}」，{_other_days_text(len(days), day)}。"
                 if names else f"已把第 {day} 天标记成不做饭，{_other_days_text(len(days), day)}。"),
        days=days, extra={"day": day, "dishes": names},
        undo=_replace_day_undo(record.id, day, before))


def mark_done(record: PlanRecord, day: int, done: bool = True) -> ActionOutcome:
    """做完了 / 再做一次（只动这一天的状态，菜单不变）。"""
    _day_of(record, day)
    days = [p.model_copy(deep=True) for p in record.result.days]
    msg = (f"已把第 {day} 天标记成做完了，{_other_days_text(len(days), day)}。"
           if done else f"已把第 {day} 天改回没做过。")
    return ActionOutcome(
        kind="done", message=msg, extra={"day": day, "done": done},
        undo={"label": "撤销", "method": "PATCH",
              "path": f"/api/v1/plans/{record.id}/days/{day}",
              "body": {"op": "done", "done": not done}})


# ---------------------------------------------------------------- 反馈与打分

def _profile_undo(plan_id: str, recipe_id: str, day: int, previous_profile: dict) -> dict:
    like = recipe_id in set(previous_profile.get("liked_dishes") or [])
    dislike = recipe_id in set(previous_profile.get("disliked_dishes") or [])
    if like or dislike:
        body = {"op": "like" if like else "dislike"}
    else:
        body = {"op": "remove"}
    return {"label": "撤销", "method": "POST",
            "path": f"/api/v1/plans/{plan_id}/dishes/{day}/{recipe_id}/feedback", "body": body}


def feedback(record: PlanRecord, db: RecipeDB, day: int, recipe_id: str, op: str,
             previous_profile: dict) -> ActionOutcome:
    """喜欢 / 不喜欢 / 定住 / 取消定住。

    - 喜欢：只写档案，**本次菜单不动**；
    - 不喜欢：写档案 + 把这道换掉（以后不再出现）；
    - 定住 / 取消定住：只改这一道菜的"必须保留"标记。
    """
    plan_day = _day_of(record, day)
    if recipe_id not in [d.recipe_id for d in plan_day.dishes]:
        raise ActionError("dish_not_in_day", f"第 {day} 天没有这道菜。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=404)
    name = _name(db, recipe_id)
    c = record.result.constraints

    if op in ("lock", "unlock"):
        locked = list(c.must_include_recipes or [])
        if op == "lock":
            if recipe_id not in locked:
                locked.append(recipe_id)
            msg = f"已定住「{name}」，以后重排会保留它（{_other_days_text(len(record.result.days), day)}）。"
        else:
            locked = [x for x in locked if x != recipe_id]
            msg = f"已取消定住「{name}」，重排时可以被换掉。"
        return ActionOutcome(kind=op, message=msg,
                             extra={"day": day, "recipe_id": recipe_id, "locked": locked,
                                    "must_include_recipes": locked},
                             undo={"label": "撤销", "method": "POST",
                                   "path": f"/api/v1/plans/{record.id}/dishes/{day}/{recipe_id}/feedback",
                                   "body": {"op": "unlock" if op == "lock" else "lock"}})

    if op == "like":
        return ActionOutcome(
            kind="like", message=f"已记住你喜欢「{name}」，以后会优先安排（本次菜单不变）。",
            extra={"day": day, "recipe_id": recipe_id},
            undo=_profile_undo(record.id, recipe_id, day, previous_profile))

    if op == "dislike":
        before = _undo_days(record, record.result.days)
        new_days, new_recipe = swap_dish(record.result.days, day, recipe_id, db, c)
        if new_recipe is not None:
            msg = (f"已记住不喜欢「{name}」，第 {day} 天换成「{new_recipe.name}」，以后不再出现。")
        else:
            msg = f"已记住不喜欢「{name}」（本次没有可替换的菜，{_other_days_text(len(record.result.days), day)}）。"
        return ActionOutcome(
            kind="dislike", message=msg, days=new_days if new_recipe else None,
            extra={"day": day, "recipe_id": recipe_id,
                   "replaced_by": new_recipe.name if new_recipe else None},
            undo=_profile_undo(record.id, recipe_id, day, previous_profile),
            next_steps=[] if new_recipe else _relax_steps("no_candidate"))

    raise ActionError("unknown_feedback_op", "不认识的反馈类型。", status=422)


def rate(record: PlanRecord, db: RecipeDB, day: int, score: int) -> ActionOutcome:
    """做完了打分：好吃(2) / 一般(1) / 下次不做(0)。只写档案，菜单不变。"""
    plan_day = _day_of(record, day)
    if score not in (0, 1, 2):
        raise ActionError("invalid_score", "打分只能是 好吃 / 一般 / 下次不做。", status=422)
    names = [_name(db, d.recipe_id) for d in plan_day.dishes]
    label = {2: "好吃", 1: "一般", 0: "下次不做"}[score]
    note = {2: "已记住这几道好吃，以后会多安排。",
            1: "已记下，下次不会特意多排。",
            0: "已记住不再做这几道。"}[score]
    return ActionOutcome(
        kind="rate", message=f"已给第 {day} 天打「{label}」：{note}（本次菜单不变）",
        extra={"day": day, "score": score, "dishes": names},
        # 撤销 = 把这几道从档案里移出去（PUT /profile 的 remove），而不是"再打一分"
        undo={"label": "撤销", "method": "PUT", "path": "/api/v1/profile",
              "body": {"op": "remove", "names": names}})


# ---------------------------------------------------------------- 省钱

def save_money(record: PlanRecord, db: RecipeDB) -> ActionOutcome:
    """哪里能省：挑最贵的一道换成更便宜的，只动那一道所在的那一天。"""
    c = record.result.constraints
    got = cheapest_swap(record.result.days, db, c)
    if got is None:
        raise ActionError("already_cheapest", "这一周已经没有明显更省的换法了。",
                          [{"op": "relax_budget", "label": "预算放宽一点"}])
    new_days, day_no, old_r, new_r, saving = got
    before = _undo_days(record, record.result.days)
    msg = (f"已把第 {day_no} 天的「{old_r.name}」换成「{new_r.name}」，"
           f"这周省了约 ¥{saving:.0f}（{_other_days_text(len(new_days), day_no)}）。")
    return ActionOutcome(
        kind="save", message=msg, days=new_days,
        extra={"day": day_no, "from": old_r.name, "to": new_r.name, "saving_yuan": round(saving, 2)},
        undo=_replace_day_undo(record.id, day_no, before))


# ---------------------------------------------------------------- 买菜清单

def set_checks(record: PlanRecord, names: list[str], label: str = "") -> ActionOutcome:
    """勾选进度（幂等 PUT，全量覆盖）。只动清单这一处，菜单与档案都不变。"""
    valid = {it.name for it in record.result.shopping if it.needed}
    unknown = [n for n in names if n not in valid]
    if unknown:
        raise ActionError("unknown_item", f"清单里没有「{'、'.join(unknown)}」。", status=422)
    count, total = len(set(names)), len(valid)
    msg = (f"已记下买到 {count}/{total} 样"
           + ("，全部买齐了。" if count == total and total else "。"))
    return ActionOutcome(
        kind="checks", message=msg, extra={"checked": sorted(set(names)), "total": total},
        undo={"label": "撤销", "method": "PUT",
              "path": f"/api/v1/plans/{record.id}/shopping/checks", "body": {"names": []}})


def apply_to_result(record: PlanRecord, outcome: ActionOutcome, db: RecipeDB) -> PlanResult:
    """把一次改动的结果落到一份**新的** PlanResult 上，并重算清单/花费/检查。

    路由必须用这个函数落库（而不是自己拼 result）：换了菜却忘了重算清单，
    买菜清单就会和菜单对不上 —— 这是最容易出的错，所以只留一条路径。
    """
    result = record.result.model_copy(deep=True)
    if outcome.days is not None:
        result.days = [p.model_copy(deep=True) for p in outcome.days]
    if "must_include_recipes" in outcome.extra:
        result.constraints.must_include_recipes = list(outcome.extra["must_include_recipes"])
    refresh_result(result, db)
    return result
