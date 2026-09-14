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
                                 skip_day, slot_of, swap_dish, where_text, with_slot)
from recipe_planner.models import (ChosenDish, DayPlan, PlanRecord, PlanResult, RecipeDB,
                                   UserConstraints)

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

def _day_of(record: PlanRecord, day: int, meal: Optional[str] = None) -> DayPlan:
    """取「这一天这一顿」。

    docs/10：一天多顿之后**不能**再 `next(p for p in record.result.days if p.day == day)` ——
    那会静默拿到**早餐**，于是"今晚换一道"改掉了早上的粥，而回执还说"第 3 天"。
    只做一顿时 `meal=None` 与老行为完全等价（当天只有一顿）。`meal=None` = 当天最后一顿。
    """
    plan_day = record.result.slot(day, meal)
    if plan_day is None:
        raise ActionError("day_not_found", f"这份方案里没有第 {day} 天。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=404)
    return plan_day


def day_plan_of(days: list[DayPlan], day: int, meal: Optional[str] = None) -> DayPlan:
    got = slot_of(days, day, meal)
    return got if got is not None else days[0]


def _place(c: UserConstraints, day: int, meal: Optional[str]) -> str:
    """"第 3 天" / "第 3 天午餐"（措辞规则在 core.where_text 里，只写一份）。"""
    return where_text(day, meal, c)


def _meal_field(meal: Optional[str]) -> dict:
    """回执/撤销里的"哪一顿"字段：多餐时才带（只做晚餐时不带，老客户端也看得懂）。"""
    return {"meal": meal} if meal else {}


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


def _day_count(record: PlanRecord) -> int:
    """这份方案一共几天。

    docs/10：一天多顿时 `len(result.days)` 是**顿数**不是天数，
    直接用会说出"其他二十天没动"这种话。
    """
    return len({p.day for p in record.result.days}) or len(record.result.days)


def _replace_day_undo(plan_id: str, day: int, before: Optional[DayPlan],
                      meal: Optional[str] = None) -> dict:
    """精确逆操作：把**这一顿**恢复成改动前的样子（比"再换一次"可靠）。

    docs/10：逆操作也必须点名是哪一顿。不带 `meal` 的话，撤销"今晚换的一道"会落到早餐上 ——
    而且改的是菜、看不出痕迹，是最难查的一类错。

    `before` 直接传**改动前的那一条 `DayPlan`**（调用方手里的 `plan_day`），不做二次查找。

    docs/11 §4.1 P0-1：这里原来是"从一份按 `days[:7]` 截断的快照里再查一次槽位"。
    一天三顿时 `days` 是**按天×餐**排列的 21 条，而快照只装得下前 7 条
    （第 1 天三顿 + 第 2 天三顿 + 第 3 天早餐），于是第 3 天午餐之后**每一个**槽位都查不到，
    `recipe_ids` 落成 `[]` —— 而 `replace_day([])` 的语义是"这一顿不做饭"。
    用户点「撤销」以为在恢复，实际把那一顿的菜删了。21 个槽位里有 15 个会中招。
    现在改成直接收下那一条，**不再有"查不到"这个分支**。
    """
    body: dict = {"op": "replace_day",
                  "recipe_ids": [d.recipe_id for d in (before.dishes if before else [])]}
    body.update(_meal_field(meal))                      # 多餐时才带 meal
    return {"label": "撤销", "method": "PATCH", "path": f"/api/v1/plans/{plan_id}/days/{day}",
            "body": body}


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

def skip(record: PlanRecord, db: RecipeDB, day: int,
         meal: Optional[str] = None) -> ActionOutcome:
    """这顿不做饭：清空这一顿的菜，不计花费、不进清单。"""
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    if plan_day.skipped:
        raise ActionError("already_skipped", f"{place}本来就没安排做饭。")
    dropped = [_name(db, d.recipe_id) for d in plan_day.dishes]
    days = skip_day(record.result.days, day, meal=meal)
    msg = f"已把{place}标记成不做饭"
    if dropped:
        msg += f"（去掉「{'、'.join(dropped)}」）"
    msg += f"，{_other_days_text(_day_count(record), day)}。"
    return ActionOutcome(
        kind="skip", message=msg, days=days,
        extra={"skipped_day": day, "dropped": dropped, **_meal_field(meal)},
        undo={**_replace_day_undo(record.id, day, plan_day, meal), "label": "撤销：改回来做"})


def restore(record: PlanRecord, db: RecipeDB, day: int,
            meal: Optional[str] = None) -> ActionOutcome:
    """改回来做：重新给这一顿挑菜，不动其他天。"""
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    if not plan_day.skipped:
        raise ActionError("not_skipped", f"{place}本来就在做饭。")
    days = restore_day(record.result.days, day, db, c, meal=meal)
    slot = slot_of(days, day, meal)
    picked = [_name(db, d.recipe_id) for d in (slot.dishes if slot else [])]
    msg = (f"已把{place}恢复做饭（{'、'.join(picked) or '暂无可排的菜'}），"
           f"{_other_days_text(_day_count(record), day)}。")
    return ActionOutcome(
        kind="restore", message=msg, days=days,
        extra={"restored_day": day, "dishes": picked, **_meal_field(meal)},
        undo=_replace_day_undo(record.id, day, plan_day, meal))


def set_people(record: PlanRecord, db: RecipeDB, day: int, people: int,
               meal: Optional[str] = None) -> ActionOutcome:
    """来客人了：只改这一顿的份量，其他天不变。

    `people` 是**这一顿的总人数**（绝对值，不搞"多加几人"的相对语义 —— 接口要一次说清）。
    """
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    base = c.people
    if people < 1:
        raise ActionError("invalid_people", "人数至少要是 1 个人。", status=422)
    target = plan_day.model_copy(deep=True)
    target.people = None if people == base else people
    days = with_slot(record.result.days, plan_day, target)
    extra = max(0, people - base)
    extra_items = _extra_shopping(target, db, extra) if extra else []
    if people == base:
        msg = f"已把{place}改回按 {base} 人算，{_other_days_text(_day_count(record), day)}。"
    else:
        msg = f"已把{place}按 {people} 人算（多 {extra} 人），{_other_days_text(_day_count(record), day)}。"
    if extra_items:
        msg += "临时要补买：" + "、".join(extra_items) + "。"
    return ActionOutcome(
        kind="people", message=msg, days=days,
        extra={"day": day, "people": people, "extra_shopping": extra_items, **_meal_field(meal)},
        undo={"label": "撤销", "method": "PATCH",
              "path": f"/api/v1/plans/{record.id}/days/{day}",
              "body": {"op": "people", "people": base, **_meal_field(meal)}})


def faster(record: PlanRecord, db: RecipeDB, day: int,
           meal: Optional[str] = None) -> ActionOutcome:
    """回家晚了：只把这一顿换成最快能做完的组合。"""
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    got = fastest_day(record.result.days, day, db, c, meal=meal)
    if got is None:
        raise ActionError("already_fastest", f"{place}已经是最快的组合了，想更快得放宽一点。",
                          _relax_steps("time"), status=409)
    new_days, recipes = got
    minutes = sum(r.time_min for r in recipes)
    names = "、".join(r.name for r in recipes)
    msg = (f"已把{place}换成快手组合：「{names}」，约 {minutes} 分钟就能上桌，"
           f"{_other_days_text(_day_count(record), day)}。")
    return ActionOutcome(
        kind="faster", message=msg, days=new_days,
        extra={"day": day, "dishes": [r.name for r in recipes], "minutes": minutes,
               **_meal_field(meal)},
        undo=_replace_day_undo(record.id, day, plan_day, meal))


def swap(record: PlanRecord, db: RecipeDB, day: int, recipe_id: str,
         meal: Optional[str] = None) -> ActionOutcome:
    """换一道：只换这一顿的这一道。"""
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    if recipe_id not in [d.recipe_id for d in plan_day.dishes]:
        raise ActionError("dish_not_in_day", f"{place}没有这道菜。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=404)
    old_name = _name(db, recipe_id)
    new_days, new_recipe = swap_dish(record.result.days, day, recipe_id, db, c, meal)
    if new_recipe is None:
        raise ActionError(
            "no_replacement",
            f"这一周没有能替换「{old_name}」的菜了。点下面的放宽项，我马上重排一版。",
            _relax_steps("no_candidate"))
    msg = f"已把{place}的「{old_name}」换成「{new_recipe.name}」，{_other_days_text(_day_count(record), day)}。"
    return ActionOutcome(
        kind="swap", message=msg, days=new_days,
        extra={"day": day, "from": old_name, "to": new_recipe.name, **_meal_field(meal)},
        undo=_replace_day_undo(record.id, day, plan_day, meal))


def replace_day(record: PlanRecord, db: RecipeDB, day: int,
                recipe_ids: list[str], meal: Optional[str] = None) -> ActionOutcome:
    """把这一顿整组换成指定的菜 —— 撤销用的精确逆操作，也是"换一整天"的底层。

    只动这一顿：其他天的菜、清单里与其他天相关的项都由 `refresh_result` 重算，不由这里手工拼。
    """
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    known = {r.id for r in db.recipes}
    unknown = [rid for rid in recipe_ids if rid not in known]
    if unknown:
        raise ActionError("unknown_recipe", "要换的菜里有不认识的，可能菜谱库变了。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=422)
    if len(set(recipe_ids)) != len(recipe_ids):
        raise ActionError("duplicate_recipe", "同一顿里不能重复同一道菜。", status=422)
    old_reasons = {d.recipe_id: d.reason for p in record.result.days for d in p.dishes}
    target = plan_day.model_copy(deep=True)
    target.skipped = not recipe_ids
    target.dishes = [ChosenDish(recipe_id=rid, reason=old_reasons.get(rid, "按你的要求换的"))
                     for rid in recipe_ids]
    days = with_slot(record.result.days, plan_day, target)
    names = [_name(db, rid) for rid in recipe_ids]
    return ActionOutcome(
        kind="replace_day",
        message=(f"已把{place}换成「{'、'.join(names)}」，{_other_days_text(_day_count(record), day)}。"
                 if names else f"已把{place}标记成不做饭，{_other_days_text(_day_count(record), day)}。"),
        days=days, extra={"day": day, "dishes": names, **_meal_field(meal)},
        undo=_replace_day_undo(record.id, day, plan_day, meal))


def mark_done(record: PlanRecord, day: int, done: bool = True,
              meal: Optional[str] = None) -> ActionOutcome:
    """做完了 / 再做一次（只动这一顿的状态，菜单不变）。"""
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    msg = (f"已把{place}标记成做完了，{_other_days_text(_day_count(record), day)}。"
           if done else f"已把{place}改回没做过。")
    return ActionOutcome(
        kind="done", message=msg, extra={"day": day, "done": done, **_meal_field(meal)},
        undo={"label": "撤销", "method": "PATCH",
              "path": f"/api/v1/plans/{record.id}/days/{day}",
              "body": {"op": "done", "done": not done, **_meal_field(meal)}})


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
             previous_profile: dict, meal: Optional[str] = None) -> ActionOutcome:
    """喜欢 / 不喜欢 / 定住 / 取消定住。

    - 喜欢：只写档案，**本次菜单不动**；
    - 不喜欢：写档案 + 把这道换掉（以后不再出现）；
    - 定住 / 取消定住：只改这一道菜的"必须保留"标记。
    """
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    if recipe_id not in [d.recipe_id for d in plan_day.dishes]:
        raise ActionError("dish_not_in_day", f"{place}没有这道菜。",
                          [{"op": "view_plan", "label": "看看这一周"}], status=404)
    name = _name(db, recipe_id)
    meal = plan_day.meal      # 以菜实际所在的那一顿为准（调用方给不准也不会改错顿）

    if op in ("lock", "unlock"):
        locked = list(c.must_include_recipes or [])
        if op == "lock":
            if recipe_id not in locked:
                locked.append(recipe_id)
            msg = f"已定住「{name}」，以后重排会保留它（{_other_days_text(_day_count(record), day)}）。"
        else:
            locked = [x for x in locked if x != recipe_id]
            msg = f"已取消定住「{name}」，重排时可以被换掉。"
        return ActionOutcome(kind=op, message=msg,
                             extra={"day": day, "recipe_id": recipe_id, "locked": locked,
                                    "must_include_recipes": locked},
                             undo={"label": "撤销", "method": "POST",
                                   "path": f"/api/v1/plans/{record.id}/dishes/{day}/{recipe_id}/feedback",
                                   "body": {"op": "unlock" if op == "lock" else "lock",
                                            **_meal_field(meal)}})

    if op == "like":
        return ActionOutcome(
            kind="like", message=f"已记住你喜欢「{name}」，以后会优先安排（本次菜单不变）。",
            extra={"day": day, "recipe_id": recipe_id},
            undo=_profile_undo(record.id, recipe_id, day, previous_profile))

    if op == "dislike":
        new_days, new_recipe = swap_dish(record.result.days, day, recipe_id, db, c, meal)
        if new_recipe is not None:
            msg = (f"已记住不喜欢「{name}」，{place}换成「{new_recipe.name}」，以后不再出现。")
        else:
            msg = f"已记住不喜欢「{name}」（本次没有可替换的菜，{_other_days_text(_day_count(record), day)}）。"
        return ActionOutcome(
            kind="dislike", message=msg, days=new_days if new_recipe else None,
            extra={"day": day, "recipe_id": recipe_id,
                   "replaced_by": new_recipe.name if new_recipe else None},
            undo=_profile_undo(record.id, recipe_id, day, previous_profile),
            next_steps=[] if new_recipe else _relax_steps("no_candidate"))

    raise ActionError("unknown_feedback_op", "不认识的反馈类型。", status=422)


def rate(record: PlanRecord, db: RecipeDB, day: int, score: int,
         meal: Optional[str] = None) -> ActionOutcome:
    """做完了打分：好吃(2) / 一般(1) / 下次不做(0)。只写档案，菜单不变。"""
    c = record.result.constraints
    plan_day = _day_of(record, day, meal)
    place = _place(c, plan_day.day, plan_day.meal if c.is_multi_meal() else None)
    if score not in (0, 1, 2):
        raise ActionError("invalid_score", "打分只能是 好吃 / 一般 / 下次不做。", status=422)
    names = [_name(db, d.recipe_id) for d in plan_day.dishes]
    label = {2: "好吃", 1: "一般", 0: "下次不做"}[score]
    note = {2: "已记住这几道好吃，以后会多安排。",
            1: "已记下，下次不会特意多排。",
            0: "已记住不再做这几道。"}[score]
    return ActionOutcome(
        kind="rate", message=f"已给{place}打「{label}」：{note}（本次菜单不变）",
        extra={"day": day, "score": score, "dishes": names, **_meal_field(meal)},
        # 撤销 = 把这几道从档案里移出去（PUT /profile 的 remove），而不是"再打一分"
        undo={"label": "撤销", "method": "PUT", "path": "/api/v1/profile",
              "body": {"op": "remove", "names": names}})


# ---------------------------------------------------------------- 省钱

def save_money(record: PlanRecord, db: RecipeDB) -> ActionOutcome:
    """哪里能省：挑最贵的一道换成更便宜的，只动那一道所在的那一顿。"""
    c = record.result.constraints
    got = cheapest_swap(record.result.days, db, c)
    if got is None:
        raise ActionError("already_cheapest", "这一周已经没有明显更省的换法了。",
                          [{"op": "relax_budget", "label": "预算放宽一点"}])
    new_days, day_no, meal, old_r, new_r, saving = got
    place = _place(c, day_no, meal)
    # 撤销要的是**被动的那一顿改动前**的样子 —— 它在原菜单里直接查，不做截断快照。
    prev = slot_of(record.result.days, day_no, meal)
    if prev is None:                       # pragma: no cover - 防御：day_no/meal 就是这份菜单里挑出来的
        raise ActionError("undo_snapshot_missing", "这一顿的改动记录没取到，暂时不能撤销。",
                          [{"op": "view_plan", "label": "看看这一周"}])
    msg = (f"已把{place}的「{old_r.name}」换成「{new_r.name}」，"
           f"这周省了约 ¥{saving:.0f}（{_other_days_text(_day_count(record), day_no)}）。")
    return ActionOutcome(
        kind="save", message=msg, days=new_days,
        extra={"day": day_no, "meal": meal, "from": old_r.name, "to": new_r.name,
               "saving_yuan": round(saving, 2)},
        undo=_replace_day_undo(record.id, day_no, prev, meal))


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
