"""餐次模型（docs/10 第①步）的测试。

这一步是**纯增量**：默认只勾晚餐时，行为必须与 P1 完全一致（所以原有 165/161/192 三套
回归继续有效）。这里只测新增的读取规则，不碰排菜逻辑。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from recipe_planner.core import recipe_conflicts, retrieve_candidates
from recipe_planner.db import _load_json_db
from recipe_planner.models import (BREAKFAST_MAX_TIME_DEFAULT, MEALS, ChosenDish, DayPlan,
                                   PlanRecord, PlanResult, UserConstraints, parse_slot_key,
                                   slot_key)


def _c(**kw) -> UserConstraints:
    return UserConstraints(**kw)


def _result(days: list[DayPlan]) -> PlanResult:
    return PlanResult(constraints=_c(), candidate_count=0, days=days, final=True)


# ---------------------------------------------------------------- 排哪几顿


def test_默认只做晚餐():
    assert _c().active_meals() == ["晚餐"]
    assert _c().is_multi_meal() is False


def test_选中的餐次按早午晚排序():
    assert _c(meals=["晚餐", "早餐", "午餐"]).active_meals() == ["早餐", "午餐", "晚餐"]
    assert _c(meals=["晚餐", "早餐"]).active_meals() == ["早餐", "晚餐"]


def test_只做早餐是合法的一种用法():
    c = _c(meals=["早餐"])
    assert c.active_meals() == ["早餐"]
    assert c.is_multi_meal() is False


@pytest.mark.parametrize("meals", [[], ["宵夜"], ["早餐", "宵夜"], None])
def test_认不出的餐次被丢掉_空则回落到只做晚餐(meals):
    """不报错、也不试图猜：宁可排成"只做晚餐"，也不能让整份方案排不出来。"""
    c = _c(meals=meals if meals is not None else [])
    assert c.active_meals() in (["晚餐"], ["早餐"])


# ---------------------------------------------------------------- 每餐几道菜


def test_每餐道数缺省回落到老字段():
    """老方案里只有 dishes_per_day，读取时必须还能用 —— 这是不做数据迁移的代价换来的。"""
    c = _c(dishes_per_day=2)
    assert c.dishes_for("早餐") == 2 and c.dishes_for("晚餐") == 2


def test_每餐道数优先于老字段():
    c = _c(dishes_per_day=2, dishes_per_meal={"早餐": 1, "午餐": 2, "晚餐": 4})
    assert (c.dishes_for("早餐"), c.dishes_for("午餐"), c.dishes_for("晚餐")) == (1, 2, 4)


def test_每餐道数上限是5而不是原来的4():
    """「增加菜品数量」落地在这：默认晚餐 3 道、上限 5 道。"""
    c = _c(dishes_per_meal={"晚餐": 99})
    assert c.dishes_for("晚餐") == 5


def test_每餐道数的0当作没填():
    """0 不是合法道数：按"没填"回落，而不是硬夹成 1（夹成 1 会让人以为只排一道）。"""
    c = _c(dishes_per_day=3, dishes_per_meal={"晚餐": 0})
    assert c.dishes_for("晚餐") == 3


# ---------------------------------------------------------------- 时长口径


def test_早餐有自己的时长上限():
    c = _c(max_time_min=45)
    assert c.time_cap_for("早餐") == BREAKFAST_MAX_TIME_DEFAULT == 15
    assert c.time_cap_for("午餐") == 45 and c.time_cap_for("晚餐") == 45


def test_早餐时长可以调():
    assert _c(breakfast_max_time_min=25).time_cap_for("早餐") == 25


@pytest.mark.parametrize("value", [4, 31])
def test_早餐时长有范围限制(value):
    with pytest.raises(ValidationError):
        _c(breakfast_max_time_min=value)


# ---------------------------------------------------------------- 一天里的哪一顿


def _three_meals() -> PlanResult:
    return _result([
        DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r31")]),
        DayPlan(day=1, meal="午餐", dishes=[ChosenDish(recipe_id="r01")]),
        DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r05")]),
        DayPlan(day=2, meal="晚餐", dishes=[ChosenDish(recipe_id="r10")]),
    ])


def test_同一天的每一顿都能取到():
    """这条守着本轮最大的地雷：`p.day == 1` 会静默拿到早餐，
    于是"今晚吃什么"会显示成早上的粥。"""
    result = _three_meals()
    assert [p.meal for p in result.slots_for(1)] == ["早餐", "午餐", "晚餐"]
    assert result.slot(1, "午餐").recipe_ids() == ["r01"]
    assert result.slot(1, "晚餐").recipe_ids() == ["r05"]
    assert result.slot(2, "晚餐").recipe_ids() == ["r10"]


def test_不指定餐次时取当天最后一顿():
    """「今晚」页默认看的就是它：勾了三餐看晚餐，只勾早餐就看早餐。"""
    assert _three_meals().slot(1).meal == "晚餐"
    only_breakfast = _result([DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r31")])])
    assert only_breakfast.slot(1).meal == "早餐"


def test_取不存在的天或餐返回None():
    result = _three_meals()
    assert result.slot(9) is None
    assert result.slot(1, "夜宵") is None
    assert result.slots_for(9) == []


def test_槽位键的往返():
    assert slot_key(3, "晚餐") == "3|晚餐"
    assert parse_slot_key("3|晚餐") == (3, "晚餐")
    assert parse_slot_key("坏了") is None, "认不出来要明说，别造一个假结果"
    assert parse_slot_key("3|") is None
    assert parse_slot_key("abc|晚餐") is None


# ---------------------------------------------------------------- "这一顿做过了"


def _record(**kw) -> PlanRecord:
    return PlanRecord(id="p1", result=_result([DayPlan(day=3, meal="晚餐")]), **kw)


def test_老存档的done_days按当天最后一顿解释():
    """`done_days` 是"一天一顿"时代的写法，说的是**那一顿**（当时只有晚餐）。

    docs/11 §4.1 P0-5：原来这里对**任意餐次**都返回 True，于是"我勾了今晚做完了"
    会让早/午/晚三顿全变已做过 —— 状态不可信，等于把这个 App 的信任基础拆了。
    新口径：老字段说的是"当天最后一顿"，与 `PlanResult.slot(day, None)` 一致。
    """
    rec = _record(done_days=[3])
    assert rec.is_done(3, "晚餐") is True
    assert rec.is_done(3, "早餐") is False, "老存档说的是一天一顿的那一顿，不是三顿都做过了"
    assert rec.is_done(4, "晚餐") is False


def test_多餐方案里老字段只落在最后一顿():
    """一天三顿时 `done_days=[3]` 只表示晚餐 —— 老数据不能"升级"成三顿都做过。"""
    rec = PlanRecord(id="p2", done_days=[3], result=_result([
        DayPlan(day=3, meal="早餐"), DayPlan(day=3, meal="午餐"), DayPlan(day=3, meal="晚餐")]))
    assert rec.is_done(3, "晚餐") is True
    assert rec.is_done(3, "早餐") is False and rec.is_done(3, "午餐") is False
    assert rec.done_meals_of(3, ["早餐", "午餐", "晚餐"]) == {"晚餐"}


def test_只做早餐时老字段落在早餐上():
    """`meal=None` 在"只做早餐"的方案里就是早餐（全项目同一条规则）。"""
    rec = PlanRecord(id="p3", done_days=[1],
                     result=_result([DayPlan(day=1, meal="早餐")]))
    assert rec.last_meal_of(1) == "早餐"
    assert rec.is_done(1, "早餐") is True


def test_新存档按顿记():
    rec = _record(done_slots=[slot_key(3, "午餐")])
    assert rec.is_done(3, "午餐") is True
    assert rec.is_done(3, "晚餐") is False, "做完了午饭不代表晚饭也做完了"
    assert rec.done_meals_of(3, ["早餐", "午餐", "晚餐"]) == {"午餐"}


def test_两种记法可以共存():
    rec = _record(done_days=[2], done_slots=[slot_key(3, "早餐")])
    assert rec.is_done(2, "晚餐") and rec.is_done(3, "早餐")
    assert not rec.is_done(3, "晚餐")


# ---------------------------------------------------------------- 早餐菜只在勾了早餐时才出现


def test_只做晚餐时早餐菜不进候选池():
    db = _load_json_db()
    breakfast = [r for r in db.recipes if r.category == "早餐"]
    assert len(breakfast) >= 8, "早餐池要有足够的菜（docs/12 阶段一之后是 26 道）"

    only_dinner = retrieve_candidates(db, _c(meals=["晚餐"]))
    assert not [r for r in only_dinner if r.category == "早餐"], \
        "只做晚餐的用户不该被排出一碗皮蛋瘦肉粥当晚饭"
    assert "breakfast" in recipe_conflicts(breakfast[0], _c(meals=["晚餐"]))


def test_勾了早餐时早餐菜就是候选():
    db = _load_json_db()
    with_breakfast = retrieve_candidates(db, _c(meals=["早餐"]))
    assert [r for r in with_breakfast if r.category == "早餐"], "勾了早餐却一道早餐都没有"


def test_早餐时长上限是硬约束而不是数据问题():
    """`docs/10`：早餐默认 **15 分钟**上限。

    菜谱里可以有"要煮 20 分钟的粥"—— 那是真实数据，不该为了迁就上限把它删掉或谎报时长；
    它只是在默认上限下**不进候选池**，用户把上限放宽就能看到（这条原来写成
    "每道早餐菜都必须 ≤15 分钟"，加粥之后就会误报成回归失败）。
    """
    db = _load_json_db()
    porridge = next(r for r in db.recipes if r.name == "红薯粥")
    assert porridge.time_min == 20

    tight = _c(meals=["早餐"], breakfast_max_time_min=15)
    assert "time" in recipe_conflicts(porridge, tight, meal="早餐")
    assert porridge not in retrieve_candidates(db, tight, "早餐")

    loose = _c(meals=["早餐"], breakfast_max_time_min=30)
    assert porridge in retrieve_candidates(db, loose, "早餐")


def test_八道早餐都进库了且id不重复():
    """不写"总数必须等于 38"：那种断言每加一道菜就要改一次，
    而它想守的其实是"这 8 道确实在库里、id 没撞"。"""
    db = _load_json_db()
    ids = [r.id for r in db.recipes]
    assert {"r31", "r32", "r33", "r34", "r35", "r36", "r37", "r38"} <= set(ids)
    assert len(ids) == len(set(ids)), "菜谱 id 不能重复"
    assert len(MEALS) == 3
