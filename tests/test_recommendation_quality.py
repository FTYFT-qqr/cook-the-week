"""docs/14 R1：推荐理由与离线评估基线。"""
from __future__ import annotations

from recipe_planner.core import (make_reason, meal_set_score, meal_structure_issues,
                                 plan_deterministic, reason_is_consistent, recipe_score,
                                 repair_plan, retrieve_candidates, score_parts, validate_plan,
                                 _has_nonprotein_vegetable, _has_protein)
from recipe_planner.db import _load_json_db
from recipe_planner.models import ChosenDish, DayPlan, Recipe, RecipeDB, UserConstraints
from recipe_planner.reporting import nutrition_boundary_text


DB = _load_json_db()


def test_score_parts是recipe_score的唯一明细来源():
    recipe = DB.by_id("r02")
    assert recipe is not None
    constraints = UserConstraints(goal="减脂", taste_tags=["清淡"], dish_last_seen={"r02": 45})
    parts = score_parts(recipe, constraints)
    assert recipe_score(recipe, constraints) == sum(float(p["score"]) for p in parts)
    assert {p["kind"] for p in parts} >= {"goal_tag", "taste", "rotation"}


def test_理由只使用实际依据且删除泛化营养措辞():
    recipe = DB.by_id("r02")
    assert recipe is not None
    reason = make_reason(recipe, UserConstraints(goal="减脂"))
    assert "偏清淡/低热量方向" in reason
    assert "营养搭配均衡" not in reason
    assert "滋润暖胃" not in reason
    assert reason_is_consistent(recipe, UserConstraints(goal="减脂"), reason)


def test_高蛋白缺少足够数据时只说标签方向():
    recipe = Recipe(id="x", name="一盘菜", time_min=30, cost_yuan=20,
                    goal_tags=["高蛋白"], protein_g=None)
    reason = make_reason(recipe, UserConstraints(goal="高蛋白"))
    assert "高蛋白标签方向" in reason
    assert "蛋白质较高" not in reason


def test_营养目标显示方向性边界():
    assert "不等于营养计算" in nutrition_boundary_text("减脂")
    assert nutrition_boundary_text("省钱") == ""


def test_日常搭配在高蛋白目标下仍优先一荤一素():
    constraints = UserConstraints(goal="高蛋白", days=4, dishes_per_day=2,
                                  max_time_min=90)
    plans, warnings = plan_deterministic(retrieve_candidates(DB, constraints), DB, constraints)

    assert not [w for w in warnings if w.code == "structure_shortage"]
    assert all(not meal_structure_issues(
        [DB.by_id(d.recipe_id) for d in p.dishes if DB.by_id(d.recipe_id)],
        constraints, meal=p.meal) for p in plans)
    assert not [i for i in validate_plan(plans, DB, constraints) if i.code == "structure"]
    first = [DB.by_id(d.recipe_id) for d in plans[0].dishes]
    assert any(_has_protein(r) for r in first if r)
    assert any(_has_nonprotein_vegetable(r) for r in first if r)
    assert meal_set_score(first, constraints) > meal_set_score(
        [DB.by_id(plans[0].dishes[0].recipe_id), DB.by_id(plans[1].dishes[0].recipe_id)],
        constraints)


def test_结构候选不足时保留可见告警而不静默违约():
    protein = Recipe(id="p", name="清蒸鱼", time_min=20, cost_yuan=20,
                     ingredients=[{"name": "鱼", "amount": "1条", "category": "水产"}])
    db = RecipeDB(recipes=[protein])
    constraints = UserConstraints(days=1, dishes_per_day=2, max_time_min=30)
    plans, warnings = plan_deterministic([protein], db, constraints)

    assert any(w.code == "shortage" for w in warnings)
    assert "候选不足" in next(w.message for w in warnings if w.code == "shortage")


def test_结构修复只替换不达标餐次():
    constraints = UserConstraints(goal="高蛋白", days=2, dishes_per_day=2,
                                  max_time_min=90)
    plans = [
        # 两道蛋白菜，故意制造结构问题。
        DayPlan(day=1, dishes=[ChosenDish(recipe_id="r19"), ChosenDish(recipe_id="r23")]),
        # 这一天已经合格，修复时必须保持原样。
        DayPlan(day=2, dishes=[ChosenDish(recipe_id="r01"), ChosenDish(recipe_id="r14")]),
    ]
    before_good = plans[1].model_dump()
    issues = validate_plan(plans, DB, constraints)
    fixed, ok = repair_plan(plans, retrieve_candidates(DB, constraints), DB, constraints, issues)

    assert ok
    assert fixed[1].model_dump() == before_good
    assert not validate_plan(fixed, DB, constraints)
