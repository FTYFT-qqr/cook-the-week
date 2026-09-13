"""多餐方案的数据库往返（docs/10 第④步）。

这一步之前，一天多顿**存不进数据库**：`plan_day` 的主键是 `(plan_id, day_no)`，
第二个餐次直接撞唯一约束。0002 迁移把餐次落在真正需要它的地方
（`plan_dish.meal` + `plan_day` 的两个"哪几顿"短字符串），这里逐项验证：
存进去、按 (天, 餐) 原样读回来、"做过了"能按顿区分、老数据（没有 meal 列的行）仍然是晚餐。
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from recipe_planner.models import (ChosenDish, DayPlan, PlanResult, UserConstraints,
                                   slot_key)
from recipe_planner.storage import db_store, engine, migrate, sync_bridge

# 注意：**不要**在收尾里调 sync_bridge.shutdown() —— 它是进程级常驻循环，
# 停掉它会让后面跑的测试（尤其是任务/SSE 那条链路）拿到已经失效的循环。
# 收尾只需要 reset_engine() + 撤掉 DATABASE_URL。

TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"


def _fresh_url() -> str:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{(TMP_ROOT / f'meals_{uuid4().hex[:8]}.db').as_posix()}"


def _seed_recipes() -> None:
    """`plan_dish.recipe_id` 有外键指向 recipe —— 空库里必须先种菜谱（r01–r30）。"""
    from recipe_planner.models import Ingredient, Recipe
    from recipe_planner.storage.repositories import RecipeRepo

    recipes = [Recipe(id=f"r{i:02d}", name=f"测试菜{i}", category="热菜", time_min=15,
                      cost_yuan=10.0,
                      ingredients=[Ingredient(name="食材", amount="1份", category="蔬菜")])
               for i in range(1, 31)]
    sync_bridge.run(RecipeRepo.upsert_many(recipes))


def _result(days: int = 2) -> PlanResult:
    c = UserConstraints(people=2, days=days, meals=["早餐", "午餐", "晚餐"],
                        dishes_per_meal={"早餐": 1, "午餐": 2, "晚餐": 3})
    slots = []
    for day in range(1, days + 1):
        slots.append(DayPlan(day=day, meal="早餐", dishes=[ChosenDish(recipe_id=f"r{day}1")]))
        slots.append(DayPlan(day=day, meal="午餐",
                             dishes=[ChosenDish(recipe_id=f"r{day}2"), ChosenDish(recipe_id=f"r{day}3")]))
        slots.append(DayPlan(day=day, meal="晚餐",
                             dishes=[ChosenDish(recipe_id=f"r{day}4"),
                                     ChosenDish(recipe_id=f"r{day}5"),
                                     ChosenDish(recipe_id=f"r{day}6")]))
    return PlanResult(constraints=c, candidate_count=0, days=slots, final=True)


def test_多餐方案存进数据库再原样读回来():
    url = _fresh_url()
    migrate.ensure_schema(url)
    os.environ["DATABASE_URL"] = url
    engine.reset_engine()
    try:
        _seed_recipes()
        rec = db_store.save_plan(_result(2), start_date="2026-09-14", change_note="多餐测试")
        back = db_store.get_record(rec.id)
        assert back is not None, "存完应该能读回来"
        got = [(p.day, p.meal, len(p.dishes)) for p in back.result.days]
        assert got == [(1, "早餐", 1), (1, "午餐", 2), (1, "晚餐", 3),
                       (2, "早餐", 1), (2, "午餐", 2), (2, "晚餐", 3)], got
        # 菜也不能串餐
        assert back.result.slot(1, "午餐").recipe_ids() == ["r12", "r13"]
        assert back.result.slot(1, "晚餐").recipe_ids() == ["r14", "r15", "r16"]
        assert back.result.slot(2, "早餐").recipe_ids() == ["r21"]
        # 存档级信息
        assert back.label and back.start_date == "2026-09-14"
        assert back.result.constraints.active_meals() == ["早餐", "午餐", "晚餐"]
    finally:
        engine.reset_engine()
        os.environ.pop("DATABASE_URL", None)


def test_做过了能按顿区分():
    url = _fresh_url()
    migrate.ensure_schema(url)
    os.environ["DATABASE_URL"] = url
    engine.reset_engine()
    try:
        _seed_recipes()
        rec = db_store.save_plan(_result(1), start_date="2026-09-14")
        db_store.set_done(rec.id, 1, True, meal="午餐")
        back = db_store.get_record(rec.id)
        assert back.is_done(1, "午餐") is True
        assert back.is_done(1, "晚餐") is False, "做完了午饭不代表晚饭也做完了"
        assert back.is_done(1, "早餐") is False
        db_store.set_done(rec.id, 1, False, meal="午餐")
        assert db_store.get_record(rec.id).is_done(1, "午餐") is False
    finally:
        engine.reset_engine()
        os.environ.pop("DATABASE_URL", None)


def test_不做饭能按顿记():
    url = _fresh_url()
    migrate.ensure_schema(url)
    os.environ["DATABASE_URL"] = url
    engine.reset_engine()
    try:
        _seed_recipes()
        rec = db_store.save_plan(_result(1), start_date="2026-09-14")
        # 直接把午餐标成不做饭（界面走的是 core.skip_day，这里只验存储往返）
        from recipe_planner.core import skip_day
        from recipe_planner.storage.repositories import PlanRepo

        result = db_store.get_record(rec.id).result
        result.days = skip_day(result.days, 1, meal="午餐")
        db_store.update_result(rec.id, result)
        back = db_store.get_record(rec.id)
        assert back.result.slot(1, "午餐").skipped is True
        assert back.result.slot(1, "晚餐").skipped is False
        assert back.result.slot(1, "早餐").skipped is False
        assert sync_bridge.run(PlanRepo.load_records()) is not None  # 仓储列表也正常
    finally:
        engine.reset_engine()
        os.environ.pop("DATABASE_URL", None)


def test_老数据没有meal列时仍然是晚餐():
    """0002 用 server_default 补了 '晚餐'，所以"迁移前存的方案"读出来还是晚餐。"""
    url = _fresh_url()
    migrate.ensure_schema(url)
    os.environ["DATABASE_URL"] = url
    engine.reset_engine()
    try:
        c = UserConstraints(people=2, days=2, dishes_per_day=2)     # 老形态：没有 meals 字段
        result = PlanResult(constraints=c, candidate_count=0, final=True, days=[
            DayPlan(day=1, dishes=[ChosenDish(recipe_id="r01"), ChosenDish(recipe_id="r02")]),
            DayPlan(day=2, dishes=[ChosenDish(recipe_id="r03")]),
        ])
        _seed_recipes()
        rec = db_store.save_plan(result, start_date="2026-09-14")
        back = db_store.get_record(rec.id)
        assert [(p.day, p.meal) for p in back.result.days] == [(1, "晚餐"), (2, "晚餐")]
        assert back.result.constraints.active_meals() == ["晚餐"]
        assert slot_key(1, "晚餐") not in back.done_slots
    finally:
        engine.reset_engine()
        os.environ.pop("DATABASE_URL", None)
