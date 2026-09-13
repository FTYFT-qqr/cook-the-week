"""多餐排菜（docs/10 第②步）的测试。

这一步的出口标准是两条：
1. **默认路径零变化** —— 只勾晚餐时排出来的菜、金额、文案与 docs/10 之前完全一致
   （由整个 tests 套件 + smoke_app 165 + self_check 163/161 守着）；
2. 勾了三餐时真的排得出三餐，且每顿都守自己的规矩（早餐池、早餐时长、当天预算）。
"""
from __future__ import annotations

from recipe_planner.core import (plan_deterministic, pool_for_meal, retrieve_candidates,
                                 validate_plan)
from recipe_planner.db import _load_json_db
from recipe_planner.llm_planner import _parse_plans, build_prompt
from recipe_planner.models import ChosenDish, DayPlan, UserConstraints

DB = _load_json_db()


def _three_meals(**kw) -> UserConstraints:
    base = dict(people=2, days=3, meals=["早餐", "午餐", "晚餐"],
                dishes_per_meal={"早餐": 1, "午餐": 2, "晚餐": 3})
    base.update(kw)
    return UserConstraints(**base)


def _plan(c: UserConstraints):
    cands = retrieve_candidates(DB, c)
    plans, warns = plan_deterministic(cands, DB, c)
    return plans, warns, validate_plan(plans, DB, c)


# ---------------------------------------------------------------- 排得出三餐


def test_勾三餐_每天就是三餐():
    plans, _warns, _issues = _plan(_three_meals())
    assert len(plans) == 9, "3 天 × 3 顿"
    for day in (1, 2, 3):
        slots = [p for p in plans if p.day == day]
        assert [p.meal for p in slots] == ["早餐", "午餐", "晚餐"], "顺序必须是早→午→晚"
        assert [len(p.dishes) for p in slots] == [1, 2, 3], "每餐道数要各按各的来"


def test_只勾晚餐时每餐2道_与以前完全一致():
    """默认路径的守门测试：结果形状与 docs/10 之前一模一样。"""
    c = UserConstraints(people=2, days=3, dishes_per_day=2)
    plans, _warns, _issues = _plan(c)
    assert len(plans) == 3
    assert all(p.meal == "晚餐" for p in plans)
    assert [len(p.dishes) for p in plans] == [2, 2, 2]


def test_只做早餐也是一种完整用法():
    c = UserConstraints(people=2, days=4, meals=["早餐"],
                        dishes_per_meal={"早餐": 2})
    plans, _warns, _issues = _plan(c)
    assert len(plans) == 4
    assert all(p.meal == "早餐" and len(p.dishes) == 2 for p in plans)
    for p in plans:
        for dish in p.dishes:
            assert DB.by_id(dish.recipe_id).category in ("早餐", "主食") or any(
                "蛋" in i.name for i in DB.by_id(dish.recipe_id).ingredients)


# ---------------------------------------------------------------- 每顿守自己的规矩


def test_早餐只吃早餐池里的东西():
    plans, _warns, _issues = _plan(_three_meals())
    for p in plans:
        for dish in p.dishes:
            r = DB.by_id(dish.recipe_id)
            if p.meal == "早餐":
                assert r.time_min <= 15, f"{r.name} 早餐做不完"
            else:
                assert r.category != "早餐", f"{r.name} 是早餐菜，不该出现在{p.meal}"


def test_一周内不重复同一道菜():
    plans, _warns, _issues = _plan(_three_meals())
    ids = [d.recipe_id for p in plans for d in p.dishes]
    assert len(ids) == len(set(ids)), "同一道菜在一周里出现了两次"


def test_三餐排出来没有硬错误():
    _plans, _warns, issues = _plan(_three_meals())
    hard = [i for i in issues if i.level == "error"]
    assert not hard, [i.message for i in hard]


def test_多顿时的提示语带餐次():
    """校验文案要说清是**哪一顿**超时，否则"第2天超时了"根本没法修。"""
    c = _three_meals()
    plans = [DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r07")])]  # 土豆炖牛肉 90 分钟
    issues = validate_plan(plans, DB, c)
    over = [i for i in issues if i.code == "over_time"]
    assert over and "第1天早餐" in over[0].message, over[0].message if over else "没有超时提示"


def test_单顿时提示语不带餐次_保持原样():
    """单顿时文案必须与以前一字不差（self_check / smoke_app 有断言压在上面）。"""
    c = UserConstraints(people=2, days=1, dishes_per_day=1)
    plans = [DayPlan(day=1, dishes=[ChosenDish(recipe_id="r07")])]
    issues = validate_plan(plans, DB, c)
    over = [i for i in issues if i.code == "over_time"]
    assert over and over[0].message.startswith("第1天「"), over[0].message if over else "没有超时提示"


# ---------------------------------------------------------------- 预算按天、不按顿


def test_预算是一整天的而不是每顿一份():
    c = _three_meals(budget_per_person_day=45.0)
    plans, _warns, issues = _plan(c)
    limit = 45.0 * c.people
    for day in (1, 2, 3):
        total = 0.0
        for p in plans:
            if p.day != day:
                continue
            for dish in p.dishes:
                total += DB.by_id(dish.recipe_id).cost_yuan * c.people / 2.0
        assert total <= limit + 1e-6, f"第{day}天花了 {total:.1f}，超了当天预算 {limit:.1f}"
    assert not [i for i in issues if i.code == "over_budget"], \
        [i.message for i in issues if i.code == "over_budget"]


def test_预算很紧时会少排菜而不是超预算():
    """候选不够就少排并给 shortage 警告 —— 宁可少一道，也不能悄悄超预算。"""
    c = _three_meals(budget_per_person_day=12.0)
    plans, warns, issues = _plan(c)
    limit = 12.0 * c.people
    for day in (1, 2, 3):
        total = sum(DB.by_id(d.recipe_id).cost_yuan * c.people / 2.0
                    for p in plans if p.day == day for d in p.dishes)
        assert total <= limit + 1e-6, f"第{day}天 {total:.1f} 超了 {limit:.1f}"
    assert not [i for i in issues if i.code == "over_budget"]


# ---------------------------------------------------------------- 候选池切分


def test_候选池按餐切分_早餐菜不进晚餐池():
    c = _three_meals()
    cands = retrieve_candidates(DB, c)
    breakfast = pool_for_meal(cands, c, "早餐")
    dinner = pool_for_meal(cands, c, "晚餐")
    assert breakfast and dinner

    # 两个池子**允许有交集**：主食（炒饭/拌面）和含蛋的菜两顿饭都可能吃，
    # 这是 docs/10 §5 有意定的（早餐池 = 早餐菜 + 主食 + 蛋类）。
    # 真正要守住的是：分类为「早餐」的那 8 道（粥/面/饼）绝不能进晚餐池。
    assert not {r.id for r in breakfast if r.category == "早餐"} & {r.id for r in dinner}
    assert not any(r.category == "早餐" for r in dinner)
    assert all(r.time_min <= 15 for r in breakfast)
    assert any(r.category == "早餐" for r in breakfast)


# ---------------------------------------------------------------- 改动只动"那一顿"


def _ids(plans, day: int, meal: str) -> list[str]:
    from recipe_planner.core import slot_of
    slot = slot_of(plans, day, meal)
    return slot.recipe_ids() if slot else []


def test_换一道只动那一顿():
    """docs/10 最大的地雷：改用 `p.day == X` 找的话，会去改当天**第一顿**（早餐）。"""
    from recipe_planner.core import swap_dish

    c = _three_meals()
    plans, _w, _i = _plan(c)
    before_breakfast = _ids(plans, 1, "早餐")
    before_lunch = _ids(plans, 1, "午餐")
    target = _ids(plans, 1, "晚餐")[0]

    new_plans, rep = swap_dish(plans, 1, target, DB, c)
    assert rep is not None
    assert _ids(new_plans, 1, "早餐") == before_breakfast, "早餐被动了"
    assert _ids(new_plans, 1, "午餐") == before_lunch, "午餐被动了"
    assert target not in _ids(new_plans, 1, "晚餐")
    assert len(_ids(new_plans, 1, "晚餐")) == 3
    assert len(new_plans) == len(plans), "槽位数量不能变"


def test_不做饭只影响那一顿():
    from recipe_planner.core import skip_day, slot_of

    c = _three_meals()
    plans, _w, _i = _plan(c)
    new_plans = skip_day(plans, 1)
    assert slot_of(new_plans, 1, "晚餐").skipped is True
    assert slot_of(new_plans, 1, "早餐").skipped is False, "只不吃晚餐，早餐还得吃"
    assert slot_of(new_plans, 1, "午餐").skipped is False
    assert _ids(new_plans, 1, "午餐"), "午餐的菜不该被清掉"


def test_改回来只补那一顿_且不重复():
    from recipe_planner.core import restore_day, skip_day, slot_of

    c = _three_meals()
    plans, _w, _i = _plan(c)
    before_breakfast = _ids(plans, 1, "早餐")
    before_lunch = _ids(plans, 1, "午餐")

    back = restore_day(skip_day(plans, 1), 1, DB, c)
    assert slot_of(back, 1, "晚餐").skipped is False
    assert len(_ids(back, 1, "晚餐")) == 3
    assert _ids(back, 1, "早餐") == before_breakfast
    assert _ids(back, 1, "午餐") == before_lunch
    ids = [d.recipe_id for p in back for d in p.dishes]
    assert len(ids) == len(set(ids)), "补回来的菜不能和别的餐撞"


def test_回家晚了只换那一顿():
    """手工造一组"慢晚餐"：排在后面的土豆炖牛肉(90)+冬瓜排骨汤(60) 一定换得动。

    用排菜结果直接测是不可靠的 —— 排出来的晚餐本来就可能已经够快，
    那时 `fastest_day` 会**诚实返回 None**（界面说"今晚这几道已经是最快的组合了"），
    测试就会因为前提不成立而假失败。
    """
    from recipe_planner.core import fastest_day, slot_of

    c = _three_meals(max_time_min=90)
    plans = [
        DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r31")]),
        DayPlan(day=1, meal="午餐", dishes=[ChosenDish(recipe_id="r01"), ChosenDish(recipe_id="r04")]),
        DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r07"), ChosenDish(recipe_id="r15")]),
    ]
    before_breakfast = _ids(plans, 1, "早餐")
    before_lunch = _ids(plans, 1, "午餐")
    old_dinner = _ids(plans, 1, "晚餐")

    out = fastest_day(plans, 1, DB, c)
    assert out is not None, "晚餐是 90 分钟的菜，一定换得动"
    new_plans, picked = out
    assert _ids(new_plans, 1, "早餐") == before_breakfast
    assert _ids(new_plans, 1, "午餐") == before_lunch
    assert _ids(new_plans, 1, "晚餐") != old_dinner
    assert all(DB.by_id(r.id).category != "早餐" for r in picked), "晚餐不该被换成早餐菜"
    assert slot_of(new_plans, 1, "晚餐").meal == "晚餐"


def test_已经够快时诚实返回None():
    """换不更快就不换，并且要明说 —— 不能为了"有变化"硬换一道更慢的。"""
    from recipe_planner.core import fastest_day

    c = _three_meals(max_time_min=90)
    plans = [
        DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r31")]),
        DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r36"), ChosenDish(recipe_id="r35")]),
    ]
    assert fastest_day(plans, 1, DB, c) is None        # 8 分钟 + 10 分钟，已经没得更快了


# ---------------------------------------------------------------- LLM 提示词


def test_提示词按餐给道数():
    c = _three_meals()
    prompt = build_prompt(c, retrieve_candidates(DB, c))
    assert "全天" in prompt
    assert "早餐1道" in prompt and "午餐2道" in prompt and "晚餐3道" in prompt
    assert "meal" in prompt, "多顿时必须要求模型给出 meal 字段"
    assert "早餐不超过 15 分钟" in prompt


def test_单顿提示词与以前一字不差():
    """只做晚餐是最常见的用法；提示词一变 LLM 输出就会变，等于给没要求改的路径引入不确定性。"""
    c = UserConstraints(people=2, days=3, dishes_per_day=2)
    prompt = build_prompt(c, retrieve_candidates(DB, c))
    assert "每天1顿晚餐" in prompt
    assert "1. 每天恰好选 2 道菜。" in prompt
    assert "单道菜耗时不超过 45 分钟" in prompt
    assert "全天" not in prompt


def test_多顿时LLM结果要三餐齐全():
    c = _three_meals(days=1)
    good = {"days": [
        {"day": 1, "meal": "早餐", "dishes": [{"recipe_id": "r31", "reason": "快手"}]},
        {"day": 1, "meal": "午餐", "dishes": [{"recipe_id": "r01", "reason": "下饭"},
                                             {"recipe_id": "r04", "reason": "清淡"}]},
        {"day": 1, "meal": "晚餐", "dishes": [{"recipe_id": "r05", "reason": "省钱"},
                                             {"recipe_id": "r06", "reason": "清爽"},
                                             {"recipe_id": "r10", "reason": "清淡"}]},
    ]}
    plans = _parse_plans(good, c)
    assert plans is not None and len(plans) == 3
    assert [p.meal for p in plans] == ["早餐", "午餐", "晚餐"]

    missing_dinner = {"days": good["days"][:2]}
    assert _parse_plans(missing_dinner, c) is None, "缺一顿就整体不通过，别留半张全天菜单"

    wrong_count = {"days": [dict(d) for d in good["days"]]}
    wrong_count["days"][0] = {"day": 1, "meal": "早餐",
                             "dishes": [{"recipe_id": "r31", "reason": "a"},
                                        {"recipe_id": "r32", "reason": "b"}]}
    assert _parse_plans(wrong_count, c) is None, "早餐多排了一道也要判不通过"
