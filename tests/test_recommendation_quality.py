"""docs/14 R1：推荐理由与离线评估基线。"""
from __future__ import annotations

from recipe_planner.core import (make_reason, reason_is_consistent, recipe_score,
                                 score_parts)
from recipe_planner.db import _load_json_db
from recipe_planner.models import Recipe, UserConstraints
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
