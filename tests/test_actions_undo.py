"""撤销（精确逆操作）的回归测试 —— docs/11 §4.1 P0-1。

用户点「撤销」的意思是"回到刚才那样"。这里曾经**静默删菜**：
`_undo_days` 按 `days[:MAX_UNDO_SNAPSHOT]`（=7）**按下标**截断，而一天多顿时
`result.days` 是**按天×餐**排列的 —— 3 餐 × 7 天 = 21 条，快照只装得下前 7 条
（第 1 天三顿 + 第 2 天三顿 + 第 3 天早餐）。于是第 3 天午餐之后的**每一个**槽位
都查不到"改动前是什么"，`recipe_ids` 落成 `[]`，而 `replace_day([])` 的语义正是
**这一顿不做饭**：用户以为在恢复，实际把那一顿的菜删了。21 个槽位里 15 个中招。

所以这里不测"能撤销"，测的是**每一顿**撤销之后，整份菜单逐槽回到改动前一模一样 ——
包括"其他餐次一道都没被动"。
"""
from __future__ import annotations

import pytest

from recipe_planner import actions
from recipe_planner.core import plan_deterministic, retrieve_candidates
from recipe_planner.db import _load_json_db
from recipe_planner.models import PlanRecord, PlanResult, UserConstraints

DB = _load_json_db()
MEALS3 = ["早餐", "午餐", "晚餐"]

# 4 天 × 3 顿 = **12 个槽位**：跨过第 7 个槽位那条截断线（第 3 天午餐正好是第 8 个）。
# 不用 7 天：7×3=21 顿 × 平均 2 道 = 42 道 > 库里 38 道，排不满就测不出东西了。
DAYS = 4
DISHES = {"早餐": 1, "午餐": 2, "晚餐": 3}
SLOTS = [(d, m) for d in range(1, DAYS + 1) for m in MEALS3]


def _week_record() -> PlanRecord:
    """一份**真排出来**的一周三餐（不是手搓的种子方案）。"""
    c = UserConstraints(people=2, days=DAYS, meals=list(MEALS3), dishes_per_meal=dict(DISHES))
    plans, _warns = plan_deterministic(retrieve_candidates(DB, c), DB, c)
    assert len(plans) == len(SLOTS), f"{DAYS} 天 × 3 顿应该是 {len(SLOTS)} 条"
    rec = PlanRecord(id="p_undo", start_date="2026-09-14",
                     result=PlanResult(constraints=c, candidate_count=len(DB.recipes), days=plans))
    assert all(d.dishes for d in rec.result.days), "每一顿都得有菜，否则测的是空对空"
    return rec


def _by_slot(days) -> dict:
    return {(p.day, p.meal): [d.recipe_id for d in p.dishes] for p in days}


def _after(rec: PlanRecord, out: actions.ActionOutcome) -> PlanRecord:
    """把一次改动的结果做成"改动之后"的存档 —— 撤销必须打在它身上才像真的。"""
    return rec.model_copy(update={"result": rec.result.model_copy(update={"days": out.days})})


@pytest.mark.parametrize("day,meal", SLOTS)
def test_每一顿的撤销都逐槽还回原样(day, meal):
    rec = _week_record()
    before = _by_slot(rec.result.days)

    out = actions.skip(rec, DB, day, meal)
    assert _by_slot(out.days)[(day, meal)] == [], "先确认这一顿真被标成不做饭了"

    hint = out.undo
    assert hint["body"]["op"] == "replace_day"
    assert hint["body"]["recipe_ids"] == before[(day, meal)], (
        f"第 {day} 天{meal}的撤销里没有原来的菜 —— 原样发出去就变成了「删掉这一顿」")

    back = actions.replace_day(_after(rec, out), DB, day,
                               hint["body"]["recipe_ids"], hint["body"].get("meal"))
    assert _by_slot(back.days) == before, "撤销之后整份菜单必须逐槽回到改动前"


def test_撤销落在点名的那一顿():
    """第 8 个槽位（旧实现正好取不到）不能落到早餐或晚餐上。"""
    day, meal = 3, "午餐"
    rec = _week_record()
    before = _by_slot(rec.result.days)
    out = actions.skip(rec, DB, day, meal)
    hint = out.undo

    assert hint["body"]["meal"] == meal, "撤销必须点名是哪一顿"
    back = actions.replace_day(_after(rec, out), DB, day,
                               hint["body"]["recipe_ids"], hint["body"]["meal"])
    after = _by_slot(back.days)
    assert after[(day, meal)] == before[(day, meal)]
    assert after[(day, "早餐")] == before[(day, "早餐")], "撤销动到了同一顿之外的餐次"
    assert after[(day, "晚餐")] == before[(day, "晚餐")], "撤销动到了同一顿之外的餐次"
