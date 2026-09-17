"""离线评估推荐质量，不写入菜谱、方案或偏好数据。

运行：python scripts/evaluate_recommendations.py
输出：.tmp/recommendation-evaluation/report.json 与 report.md

输入固定为仓库内的 data/recipes.json，排菜只走确定性核心，避免网络模型让基线漂移。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recipe_planner import preference  # noqa: E402
from recipe_planner.core import (  # noqa: E402
    _has_protein,
    plan_deterministic,
    retrieve_candidates,
    validate_plan,
)
from recipe_planner.db import _load_json_db  # noqa: E402
from recipe_planner.models import UserConstraints  # noqa: E402


DEFAULT_OUTPUT_DIR = ROOT / ".tmp" / "recommendation-evaluation"
CROSS_WEEK_JACCARD_MAX = 0.60
GOAL_HIT_RATE_MIN = 0.50


def _scenarios() -> dict[str, UserConstraints]:
    """R1-3 的固定输入；不读取本机档案，保证不同机器结果可比较。"""
    common = dict(people=2, days=3, dishes_per_day=2, max_time_min=45,
                  spice_level="不辣")
    return {
        "default": UserConstraints(**common),
        "清淡": UserConstraints(**common, goal="清淡", taste_tags=["清淡"]),
        "减脂方向": UserConstraints(**common, goal="减脂", taste_tags=["清淡"]),
        "高蛋白方向": UserConstraints(**common, goal="高蛋白"),
        "省钱": UserConstraints(**common, goal="省钱", budget_per_person_day=25),
        "新手": UserConstraints(**common, skill="新手"),
        "严格过敏": UserConstraints(**common, allergens=["海鲜", "蛋"]),
        "近期吃过": UserConstraints(
            **common,
            dish_last_seen={"r01": 2, "r02": 3, "r03": 4, "r04": 5},
        ),
    }


def _plan_once(db, constraints: UserConstraints):
    candidates = retrieve_candidates(db, constraints)
    plans, warnings = plan_deterministic(candidates, db, constraints)
    issues = validate_plan(plans, db, constraints)
    return candidates, plans, warnings, issues


def _cost(plans, db, constraints: UserConstraints) -> float:
    return round(sum(
        recipe.cost_yuan * constraints.people / 2.0
        for plan in plans
        for dish in plan.dishes
        if (recipe := db.by_id(dish.recipe_id)) is not None
    ), 2)


def _structure(plans, db) -> dict[str, int | float | None]:
    eligible = [p for p in plans if not p.skipped and len(p.dishes) >= 2]
    achieved = 0
    for plan in eligible:
        recipes = [db.by_id(d.recipe_id) for d in plan.dishes]
        recipes = [r for r in recipes if r is not None]
        if any(_has_protein(r) for r in recipes) and any(not _has_protein(r) for r in recipes):
            achieved += 1
    return {
        "evaluable_slots": len(eligible),
        "achieved_slots": achieved,
        "rate": round(achieved / len(eligible), 4) if eligible else None,
    }


def _reason_metrics(plans, db, constraints: UserConstraints) -> dict[str, int | float]:
    """独立按菜谱事实核对理由，不调用理由生成器或一致性模板。"""
    total = 0
    consistent = 0
    for plan in plans:
        for dish in plan.dishes:
            recipe = db.by_id(dish.recipe_id)
            if recipe is None:
                continue
            total += 1
            if _reason_facts_are_consistent(recipe, constraints, dish.reason):
                consistent += 1
    return {
        "total": total,
        "consistent": consistent,
        "rate": round(consistent / total, 4) if total else 1.0,
    }


def _reason_facts_are_consistent(recipe, constraints: UserConstraints, reason: str) -> bool:
    """只看可从输入独立推出的事实，避免与 `make_reason()` 共享模板。"""
    if not reason.startswith(f"{recipe.name}："):
        return False
    tail = f"约 {recipe.time_min} 分钟，成本约 {recipe.cost_yuan} 元/份（{recipe.category}）。"
    if tail not in reason:
        return False
    if recipe.time_min <= 20 and "快手省时" not in reason:
        return False
    if recipe.time_min > 20 and "快手省时" in reason:
        return False
    if recipe.cost_yuan <= 12 and "成本友好" not in reason:
        return False
    if recipe.cost_yuan > 12 and "成本友好" in reason:
        return False

    expected_goal = {
        "减脂": "偏清淡/低热量方向",
        "控糖": "控糖饮食方向（非营养计算）",
        "高蛋白": "蛋白质较高" if (recipe.protein_g or 0) >= 20 else "高蛋白标签方向",
    }.get(constraints.goal)
    if constraints.goal != "随便" and constraints.goal in recipe.goal_tags:
        if expected_goal and expected_goal not in reason:
            return False
    elif expected_goal and expected_goal in reason:
        return False

    for taste in sorted(set(constraints.taste_tags) & set(recipe.taste_tags)):
        if f"符合「{taste}」口味" not in reason:
            return False

    weights = getattr(constraints, "dish_weights", None) or {}
    if weights:
        weight = float(weights.get(recipe.id, 0.0))
        if weight > 0 and "按你的偏好优先" not in reason:
            return False
        if weight < 0 and "按你的反馈暂时避开" not in reason:
            return False
    elif recipe.id in set(constraints.liked_dishes) and "你喜欢这道菜" not in reason:
        return False

    rotation = preference.rotation_adjustment(
        (getattr(constraints, "dish_last_seen", None) or {}).get(recipe.id)
    )
    if rotation < 0 and "最近吃过，先换换口味" not in reason:
        return False
    if rotation > 0 and "有一阵子没吃，帮你换换口味" not in reason:
        return False
    if rotation == 0 and ("最近吃过，先换换口味" in reason
                          or "有一阵子没吃，帮你换换口味" in reason):
        return False

    if "这一顿先安排蛋白来源" in reason and not _has_protein(recipe):
        return False
    if "这一顿补一道蔬菜" in reason and _has_protein(recipe):
        return False
    if "这一顿补一碗汤" in reason and recipe.category != "汤":
        return False
    return not any(text in reason for text in ("营养搭配均衡", "滋润暖胃"))


def _jaccard(plans_a, plans_b) -> float:
    a = {d.recipe_id for p in plans_a for d in p.dishes}
    b = {d.recipe_id for p in plans_b for d in p.dishes}
    union = a | b
    return round(len(a & b) / len(union), 4) if union else 1.0


def _next_week_constraints(constraints: UserConstraints, plans) -> UserConstraints:
    """把第一周实际排出的菜视作已吃过，模拟下一周的真实反馈。"""
    seen = dict(getattr(constraints, "dish_last_seen", None) or {})
    for plan in plans:
        for dish in plan.dishes:
            seen[dish.recipe_id] = 3
    return constraints.model_copy(update={"dish_last_seen": seen})


def evaluate(db=None) -> dict:
    db = db or _load_json_db()
    rows: dict[str, dict] = {}
    for name, constraints in _scenarios().items():
        candidates, plans, warnings, issues = _plan_once(db, constraints)
        next_constraints = _next_week_constraints(constraints, plans)
        candidates_2, plans_2, warnings_2, issues_2 = _plan_once(db, next_constraints)
        ids = [d.recipe_id for p in plans for d in p.dishes]
        slots = [p for p in plans if not p.skipped and p.dishes]
        goal_slots = [
            p for p in slots
            if constraints.goal == "随便"
            or any((recipe := db.by_id(d.recipe_id)) is not None
                   and constraints.goal in recipe.goal_tags for d in p.dishes)
        ]
        budget_total = (constraints.budget_per_person_day * constraints.people * constraints.days
                        if constraints.budget_per_person_day is not None else None)
        cost = _cost(plans, db, constraints)
        expected_slots = constraints.days * sum(
            constraints.dishes_for(meal) for meal in constraints.active_meals())
        shortage = len(ids) < expected_slots or len(
            [d.recipe_id for p in plans_2 for d in p.dishes]) < expected_slots
        budget_overage = round(max(0.0, cost - budget_total), 2) if budget_total is not None else 0.0
        rows[name] = {
            "candidate_count": len(candidates),
            "candidate_shortage": shortage,
            "hard_violations": sum(1 for i in issues + issues_2 if i.level == "error"),
            "warning_count": (len(warnings) + len(warnings_2)
                               + sum(1 for i in issues + issues_2 if i.level == "warning")),
            "weekly_duplicate_count": len(ids) - len(set(ids)),
            "structure": _structure(plans, db),
            "cross_week_jaccard": _jaccard(plans, plans_2),
            "goal_direction_hit_rate": (
                round(len(goal_slots) / len(slots), 4) if slots and constraints.goal != "随便" else None
            ),
            "estimated_cost_yuan": cost,
            "budget_total_yuan": round(budget_total, 2) if budget_total is not None else None,
            "budget_overage_yuan": budget_overage if budget_total is not None else None,
            "budget_overage_explained": budget_overage == 0.0 or any(
                i.code == "over_budget" for i in issues + issues_2),
            "explanation": _reason_metrics(plans, db, constraints),
        }

    all_rows = list(rows.values())
    return {
        "schema_version": 1,
        "input": {
            "recipe_source": "data/recipes.json",
            "recipe_count": len(db.recipes),
            "planner": "deterministic",
            "scenarios": list(_scenarios()),
        },
        "summary": {
            "hard_violations": sum(row["hard_violations"] for row in all_rows),
            "weekly_duplicate_count": sum(row["weekly_duplicate_count"] for row in all_rows),
            "explanation_consistency_rate": min(
                (row["explanation"]["rate"] for row in all_rows), default=1.0
            ),
            "cross_week_jaccard_max": max(
                (row["cross_week_jaccard"] for row in all_rows
                 if not row["candidate_shortage"]), default=0.0
            ),
        },
        "scenarios": rows,
    }


def _markdown(report: dict) -> str:
    lines = [
        "# 推荐质量离线评估",
        "",
        "> 固定输入：`data/recipes.json`；确定性排菜；不读取或修改本机档案。",
        "",
        "| 场景 | 候选 | 硬约束违规 | 周内重复 | 结构达标率 | 跨周 Jaccard | 理由一致率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in report["scenarios"].items():
        structure = row["structure"]["rate"]
        structure_text = "—" if structure is None else f"{structure:.0%}"
        lines.append(
            f"| {name} | {row['candidate_count']} | {row['hard_violations']} | "
            f"{row['weekly_duplicate_count']} | {structure_text} | "
            f"{row['cross_week_jaccard']:.0%} | {row['explanation']['rate']:.0%} |"
        )
    lines += [
        "",
        "## 汇总",
        "",
        f"- 硬约束违规：{report['summary']['hard_violations']}",
        f"- 周内重复总数：{report['summary']['weekly_duplicate_count']}",
        f"- 最低理由一致率：{report['summary']['explanation_consistency_rate']:.0%}",
        "",
        f"默认场景跨周重合率门槛：≤ {CROSS_WEEK_JACCARD_MAX:.0%}；候选不足的场景单独标记。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="报告输出目录（默认 .tmp/recommendation-evaluation）")
    args = parser.parse_args(argv)

    report = evaluate()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "report.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    print(f"Reports written to: {args.output_dir / 'report.json'} and {args.output_dir / 'report.md'}")
    failures = []
    if report["summary"]["hard_violations"] != 0:
        failures.append("hard_constraints")
    if report["summary"]["weekly_duplicate_count"] != 0:
        failures.append("weekly_duplicates")
    if report["summary"]["explanation_consistency_rate"] < 1.0:
        failures.append("independent_explanations")
    if report["summary"]["cross_week_jaccard_max"] > CROSS_WEEK_JACCARD_MAX:
        failures.append("cross_week_overlap")
    for name, row in report["scenarios"].items():
        if row["goal_direction_hit_rate"] is not None and row["goal_direction_hit_rate"] < GOAL_HIT_RATE_MIN:
            failures.append(f"goal:{name}")
        if not row["budget_overage_explained"]:
            failures.append(f"budget:{name}")
    if failures:
        print("质量门禁失败：" + ", ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
