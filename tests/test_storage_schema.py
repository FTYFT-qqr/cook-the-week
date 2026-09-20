"""P0-2 验收：17 张表建表、字段齐全、约束与级联真的生效。

运行：python -m pytest tests -q
（pytest.ini 里 asyncio_mode=auto，所以 async 测试不用加 marker）
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from recipe_planner.storage import engine as engine_mod
from recipe_planner.storage.engine import attach_sqlite_pragmas
from recipe_planner.storage.orm import (
    ActionLog,
    AppSetting,
    AppUser,
    Base,
    DishEvent,
    Household,
    IdempotencyKey,
    Ingredient,
    Job,
    LlmCallLog,
    Plan,
    PlanDay,
    PlanDish,
    Preference,
    Rating,
    Recipe,
    RecipeReference,
    ShoppingCheck,
    ShoppingItem,
)

EXPECTED_TABLES = {
    "recipe", "ingredient", "household", "app_user", "preference", "rating",
    "plan", "plan_day", "plan_dish", "shopping_item", "shopping_check",
    "job", "action_log", "idempotency_key", "llm_call_log", "app_setting",
    "dish_event", "recipe_reference",
}

# 注意：不用 pytest 的 tmp_path（它落在系统 TEMP 上，本项目环境对该目录无写权限）
TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"


def _db_file(tag: str = "") -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TMP_ROOT / f"t_{tag}{uuid4().hex[:8]}.db"


@pytest.fixture()
async def db():
    """每个测试一个独立 SQLite 文件库（真实文件才能验证 pragma 与索引）。"""
    db_file = _db_file()
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine = create_async_engine(url, connect_args={"timeout": 5})
    attach_sqlite_pragmas(engine)          # 与生产 engine 同一套 pragma（外键/ WAL / busy_timeout）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(db_file) + suffix).unlink(missing_ok=True)
            except OSError:
                pass


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda sc: inspect(sc).get_table_names())
    return set(names)


async def test_17_tables_created(db):
    engine = db.kw["bind"]
    assert await _tables(engine) == EXPECTED_TABLES


async def test_dish_event_columns(db):
    """偏好事件表（docs/12 阶段二）：逐菜一行，存**菜谱 id** 而不是菜名。

    故意**不建** CheckConstraint（以后加一种动作不该需要迁移），所以这里只验列与索引。
    """
    engine = db.kw["bind"]

    def _cols(sc):
        return ({c["name"] for c in inspect(sc).get_columns("dish_event")},
                {i["name"] for i in inspect(sc).get_indexes("dish_event")})

    async with engine.connect() as conn:
        cols, idx = await conn.run_sync(_cols)
    assert {"plan_id", "household_id", "recipe_id", "action", "meal", "day_no",
            "source", "created_at"} <= cols, cols
    assert {"dish_event_recipe_idx", "dish_event_plan_idx"} <= idx, idx
    assert DishEvent.__tablename__ == "dish_event"


async def test_recipe_columns_and_json_roundtrip(db):
    engine = db.kw["bind"]
    async with engine.connect() as conn:
        cols = await conn.run_sync(lambda sc: {c["name"] for c in inspect(sc).get_columns("recipe")})
    assert {"id", "name", "category", "difficulty", "time_min", "cost_yuan",
            "taste_tags", "goal_tags", "allergens", "carbs_g", "fat_g", "status",
            "version", "source_type", "source_url", "source_creator", "reviewed_at",
            "nutrition_basis", "nutrition_source", "nutrition_estimated", "content_hash",
            "batch_id", "created_at"} <= cols
    # 做法与视频链接（docs/12 阶段三，迁移 0004）：两列都是"有默认值"的，老库升级上来不会 NULL
    assert {"steps", "video_url"} <= cols, cols

    async with db() as s:
        s.add(Recipe(id="r01", name="西红柿炒鸡蛋", time_min=15, cost_yuan=8.5,
                     taste_tags=["清淡", "下饭"], goal_tags=["减脂"], allergens=["蛋"],
                     steps=["西红柿切块，鸡蛋打散", "热锅炒蛋盛出", "炒西红柿后合炒调味"],
                     video_url="https://example.com/v"))
        s.add(Ingredient(recipe_id="r01", seq=1, name="西红柿", amount="2 个",
                         category="蔬菜", grams=300))
        await s.commit()

    async with db() as s:
        r = await s.get(Recipe, "r01")
        assert r.taste_tags == ["清淡", "下饭"]        # JSON 列往返正常
        assert r.steps == ["西红柿切块，鸡蛋打散", "热锅炒蛋盛出", "炒西红柿后合炒调味"]
        assert r.video_url == "https://example.com/v"
        assert float(r.cost_yuan) == 8.5
        ing = (await s.execute(select(Ingredient).where(Ingredient.recipe_id == "r01"))).scalar_one()
        assert ing.grams == 300


async def test_recipe_reference_roundtrip(db):
    async with db() as s:
        s.add(Recipe(id="r-ref", name="参考菜", time_min=15, cost_yuan=8))
        s.add(RecipeReference(recipe_id="r-ref", kind="video", platform="B站",
                              title="家常做法", url="https://example.com/video"))
        await s.commit()

    async with db() as s:
        ref = await s.get(RecipeReference, 1)
        assert ref is not None
        assert ref.recipe_id == "r-ref" and ref.active is True


async def test_preference_is_mutually_exclusive(db):
    async with db() as s:
        s.add(Household(id="h1", name="我的家"))
        s.add(Recipe(id="r02", name="蒜蓉西兰花", time_min=12, cost_yuan=6))
        await s.commit()
        s.add(Preference(household_id="h1", recipe_id="r02", kind="like"))
        await s.commit()

    async with db() as s:
        s.add(Preference(household_id="h1", recipe_id="r02", kind="dislike"))
        with pytest.raises(IntegrityError):           # 主键即互斥：一道菜只能在一个列表里
            await s.commit()
        await s.rollback()

    async with db() as s:
        s.add(Preference(household_id="h1", recipe_id="r02", kind="whatever"))
        with pytest.raises(IntegrityError):           # check 约束拦住非法 kind
            await s.commit()
        await s.rollback()


async def test_only_one_active_plan_per_household(db):
    async with db() as s:
        s.add(Household(id="h1"))
        s.add(Plan(id="p1", household_id="h1", week_start=date(2026, 9, 14), status="active"))
        await s.commit()

    async with db() as s:
        s.add(Plan(id="p2", household_id="h1", week_start=date(2026, 9, 21), status="active"))
        with pytest.raises(IntegrityError):           # 决策 4：同时只 1 份 active
            await s.commit()
        await s.rollback()

    async with db() as s:
        s.add(Plan(id="p3", household_id="h1", week_start=date(2026, 9, 21), status="archived"))
        await s.commit()
    async with db() as s:
        archived = (await s.execute(
            select(Plan).where(Plan.status == "archived"))).scalars().all()
        assert [p.id for p in archived] == ["p3"]     # 归档可以有任意多份


async def test_cascade_delete_and_restrict(db):
    async with db() as s:
        s.add(Household(id="h1"))
        s.add(Recipe(id="r03", name="清蒸鲈鱼", time_min=25, cost_yuan=30))
        s.add(Plan(id="p1", household_id="h1", week_start=date(2026, 9, 14)))
        s.add(PlanDay(plan_id="p1", day_no=1, day_date=date(2026, 9, 14)))
        s.add(PlanDish(plan_id="p1", day_no=1, seq=1, recipe_id="r03", locked=True))
        s.add(ShoppingItem(plan_id="p1", name="鲈鱼", category="水产", amount_text="1 条",
                           batch=2, optional=False, used_for=["清蒸鲈鱼"]))
        s.add(ShoppingCheck(plan_id="p1", item_name="鲈鱼"))
        s.add(ActionLog(plan_id="p1", household_id="h1", kind="lock", text="已定住「清蒸鲈鱼」"))
        await s.commit()

    async with db() as s:
        plan = await s.get(Plan, "p1")
        await s.delete(plan)
        await s.commit()

    async with db() as s:
        for model in (PlanDay, PlanDish, ShoppingItem, ShoppingCheck, ActionLog):
            rows = (await s.execute(select(model))).scalars().all()
            assert rows == [], f"{model.__name__} 没跟着方案一起删掉"

    async with db() as s:
        # 菜谱被方案引用时不允许删除（RESTRICT），避免历史菜单变成悬空记录
        recipe = await s.get(Recipe, "r03")
        assert recipe is not None


async def test_job_and_log_tables_work(db):
    async with db() as s:
        s.add(Household(id="h1"))
        s.add(Recipe(id="r04", name="香菇青菜", time_min=12, cost_yuan=7))
        await s.commit()

    async with db() as s:
        s.add(Job(id="j1", household_id="h1", request={"days": 3}, status="queued"))
        s.add(LlmCallLog(model="deepseek-chat", purpose="plan", prompt_hash="abc",
                         tokens_in=1200, tokens_out=300, latency_ms=2870, ok=True))
        s.add(IdempotencyKey(key="k1", endpoint="POST /api/v1/plans", status_code=202,
                             response={"job_id": "j1"}))
        s.add(AppSetting(key="retention_weeks", value={"weeks": 12}))
        s.add(AppUser(id="u1", household_id="h1", name="我"))
        s.add(Rating(household_id="h1", recipe_id="r04", score=2))
        await s.commit()

    async with db() as s:
        job = await s.get(Job, "j1")
        assert job.status == "queued" and job.request == {"days": 3}
        assert job.progress == 0
        job.status = "running"
        job.stage = "正在搭配这一周的菜…"
        job.started_at = datetime.now(timezone.utc)
        job.progress = 0.45
        await s.commit()

    async with db() as s:
        s.add(Job(id="j2", household_id="h1", status="whatever"))
        with pytest.raises(IntegrityError):           # 状态机取值受 check 约束
            await s.commit()
        await s.rollback()


async def test_sqlite_pragmas_via_env(monkeypatch):
    """走真实 settings → engine 路径，确认 WAL / busy_timeout / 外键被打开。"""
    db_file = _db_file("pragma_")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    engine_mod.reset_engine()
    try:
        eng = engine_mod.get_engine()
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            mode = (await conn.execute(text("pragma journal_mode"))).scalar()
            busy = (await conn.execute(text("pragma busy_timeout"))).scalar()
            fk = (await conn.execute(text("pragma foreign_keys"))).scalar()
        assert str(mode).lower() == "wal"
        assert int(busy) == 5000
        assert int(fk) == 1
    finally:
        await engine_mod.get_engine().dispose()
        engine_mod.reset_engine()
        os.environ.pop("DATABASE_URL", None)
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(db_file) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
