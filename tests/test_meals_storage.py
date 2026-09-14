"""多餐方案的数据库往返（docs/10 第④步）。

这一步之前，一天多顿**存不进数据库**：`plan_day` 的主键是 `(plan_id, day_no)`，
第二个餐次直接撞唯一约束。0002 迁移把餐次落在真正需要它的地方
（`plan_dish.meal` + `plan_day` 的两个"哪几顿"短字符串），这里逐项验证：
存进去、按 (天, 餐) 原样读回来、"做过了"能按顿区分、老数据（没有 meal 列的行）仍然是晚餐。
"""
from __future__ import annotations

import importlib.util
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


# ---------------------------------------------------------------- 「做完了」按顿（docs/11 §4.1 P0-5）

MEALS3 = ["早餐", "午餐", "晚餐"]


def _with_db(fn):
    """跑一段需要临时库的断言（三处口径相同，抽出来避免各写一遍）。"""
    url = _fresh_url()
    migrate.ensure_schema(url)
    os.environ["DATABASE_URL"] = url
    engine.reset_engine()
    try:
        _seed_recipes()
        fn()
    finally:
        engine.reset_engine()
        os.environ.pop("DATABASE_URL", None)


def test_不给meal时只标当天最后一顿而不是三顿():
    """**这就是 docs/11 §4.1 P0-5 的核心断言。**

    以前"不给 meal"= 记"这一整天"（`done_at`），而 `is_done` 对任意餐次都返回 True ——
    用户勾一次"今晚做完了"，界面上早/午/晚三顿全变已做过。
    现在两边都是"当天最后一顿"。
    """
    def body():
        rec = db_store.save_plan(_result(1), start_date="2026-09-14")
        db_store.set_done(rec.id, 1, True)                 # 不给 meal
        back = db_store.get_record(rec.id)
        assert back.is_done(1, "晚餐") is True
        assert back.is_done(1, "早餐") is False, "「今晚做完了」不该把早餐也标成做过"
        assert back.is_done(1, "午餐") is False, "「今晚做完了」不该把午餐也标成做过"
        assert back.done_meals_of(1, MEALS3) == {"晚餐"}
        assert back.done_slots == [slot_key(1, "晚餐")], back.done_slots

    _with_db(body)


def test_取消做完了也只取消那一顿():
    def body():
        rec = db_store.save_plan(_result(1), start_date="2026-09-14")
        db_store.set_done(rec.id, 1, True)                  # 晚餐
        db_store.set_done(rec.id, 1, True, meal="早餐")
        db_store.set_done(rec.id, 1, False)                 # 取消"当天最后一顿"
        back = db_store.get_record(rec.id)
        assert back.is_done(1, "晚餐") is False
        assert back.is_done(1, "早餐") is True, "取消晚餐不该把早餐一起取消"
        assert back.done_days == [], "最后一顿取消了，老字段也要跟着清掉"

    _with_db(body)


def test_老字段done_at与新的按顿标记不再互相掩盖():
    """老数据的 `done_at`（＝当天最后一顿做过）和 `done_meals` 并存时两边都要读得到。

    以前 `_plan_to_record` 的条件是"done_at 非空**且** done_meals 为空"才算老字段 ——
    于是"老标记 + 新标记"同时存在时，老标记**被静默吞掉**。
    """
    def body():
        from datetime import datetime, timezone

        from recipe_planner.storage import orm
        from recipe_planner.storage.engine import session_scope

        rec = db_store.save_plan(_result(1), start_date="2026-09-14")
        db_store.set_done(rec.id, 1, True, meal="午餐")       # 先按顿标

        async def _legacy_mark():                             # 再补一个"老式"整天标记
            async with session_scope() as s:
                row = await s.get(orm.PlanDay, (rec.id, 1))
                row.done_at = datetime.now(timezone.utc)

        sync_bridge.run(_legacy_mark())
        back = db_store.get_record(rec.id)
        assert back.is_done(1, "午餐") is True
        assert back.done_days == [1], "老字段不能被 done_meals 吞掉"
        assert back.is_done(1, "晚餐") is True, "老字段说的是当天最后一顿"
        assert back.is_done(1, "早餐") is False

    _with_db(body)


def test_两个后端的做完了语义一致():
    """JSON 与 DB 对同一串操作必须给出同一个答案（docs/11 的 R5/P1：多后端分叉）。

    以前：JSON 把 `meal=None` 记成"一整天"、DB 记成"当天"并顺手清空 `done_meals` ——
    切一次 `STORAGE`，同一份"我这一周"就换了一副样子。
    """
    def snapshot(rec) -> set:
        return {(p.day, p.meal) for p in rec.result.days if rec.is_done(p.day, p.meal)}

    def body():
        # DB 侧
        db_rec = db_store.save_plan(_result(1), start_date="2026-09-14")
        db_store.set_done(db_rec.id, 1, True)                  # 不给 meal
        db_store.set_done(db_rec.id, 1, True, meal="早餐")
        db_done = snapshot(db_store.get_record(db_rec.id))

        # JSON 侧：同一串操作、同一份方案。用**按 STORAGE=json 重新加载的那个模块**
        # （顶层 `store` 在 db 模式下已被 db_store 覆盖，直接调它测的是 DB）
        plan_file = TMP_ROOT / f"p05_{uuid4().hex[:8]}.json"
        old_file = os.environ.get("RECIPE_PLAN_FILE")
        old_storage = os.environ.get("STORAGE")
        os.environ["RECIPE_PLAN_FILE"] = str(plan_file)
        os.environ["STORAGE"] = "json"
        try:
            spec = importlib.util.spec_from_file_location(
                f"json_store_p05_{uuid4().hex[:6]}",
                Path(__file__).resolve().parent.parent / "recipe_planner" / "store.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            json_rec = mod.save_plan(_result(1), start_date="2026-09-14")
            mod.set_done(json_rec.id, 1, True)
            mod.set_done(json_rec.id, 1, True, meal="早餐")
            json_done = snapshot(mod.get_record(json_rec.id))
        finally:
            if old_file is None:
                os.environ.pop("RECIPE_PLAN_FILE", None)
            else:
                os.environ["RECIPE_PLAN_FILE"] = old_file
            if old_storage is None:
                os.environ.pop("STORAGE", None)
            else:
                os.environ["STORAGE"] = old_storage
            plan_file.unlink(missing_ok=True)

        assert json_done == db_done == {(1, "晚餐"), (1, "早餐")}, (json_done, db_done)

    _with_db(body)
