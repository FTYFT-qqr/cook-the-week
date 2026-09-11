"""接口层测试的公共夹具（docs/08 §11：pytest + httpx.AsyncClient）。

- 每个测试一份**临时 SQLite 库**（不碰 `data/app.db`），用 `create_all` 建表
  （"迁移 == ORM" 已由 `tests/test_migrations.py` 单独证明，这里不重复验）；
- 种几道菜 + 一份 3 天方案 + 一份档案；
- `raise_app_exceptions=False`：这样才能拿到我们自己的 500 problem+json 响应体。
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from recipe_planner.api.main import create_app
from recipe_planner.models import (ChosenDish, DayPlan, PlanResult, Recipe, RecipeDB,
                                   ShoppingItem, UserConstraints)
from recipe_planner.storage.engine import get_engine, reset_engine
from recipe_planner.storage.orm import Base
from recipe_planner.storage.repositories import PlanRepo, ProfileRepo, RecipeRepo

TMP_ROOT = Path(__file__).resolve().parent.parent.parent / ".tmp" / "pytest"

# 5 道菜，够排 3 天 × 2 道还留一道备用
RECIPES = [
    Recipe(id="r1", name="番茄炒蛋", category="热菜", difficulty="简单", time_min=15,
           cost_yuan=8.0, taste_tags=["酸甜", "下饭"], goal_tags=["省钱"],
           ingredients=[{"name": "番茄", "amount": "2个", "category": "蔬菜"},
                        {"name": "鸡蛋", "amount": "3个", "category": "肉蛋"}]),
    Recipe(id="r2", name="清炒时蔬", category="热菜", difficulty="简单", time_min=10,
           cost_yuan=6.0, taste_tags=["清淡"], goal_tags=["减脂", "省钱"],
           ingredients=[{"name": "青菜", "amount": "1把", "category": "蔬菜"}]),
    Recipe(id="r3", name="红烧排骨", category="热菜", difficulty="中等", time_min=55,
           cost_yuan=38.0, taste_tags=["咸鲜"], goal_tags=["高蛋白"],
           ingredients=[{"name": "排骨", "amount": "600克", "category": "肉蛋"}]),
    Recipe(id="r4", name="紫菜蛋花汤", category="汤", difficulty="简单", time_min=12,
           cost_yuan=5.0, taste_tags=["清淡"], goal_tags=["清淡"],
           ingredients=[{"name": "紫菜", "amount": "1小把", "category": "干货"}]),
    Recipe(id="r5", name="凉拌黄瓜", category="凉菜", difficulty="简单", time_min=8,
           cost_yuan=4.0, taste_tags=["清爽"], goal_tags=["减脂"],
           ingredients=[{"name": "黄瓜", "amount": "2根", "category": "蔬菜"}]),
]

PROFILE = {
    "customer_name": "默认客户",
    "liked_dishes": ["番茄炒蛋"],
    "disliked_dishes": ["红烧排骨"],
    # ratings 的形状由 profile.rate() 决定：{菜名: {"score": 0|1|2, "date": "MM/DD"}}
    "ratings": {"清炒时蔬": {"score": 2, "date": "09/10"}},
    "history": {"番茄炒蛋": {"since": "09/10", "source": "菜单页"}},
}

START = "2026-09-14"          # 周一


def build_result(days: int = 3, people: int = 2, dishes_per_day: int = 2,
                 **constraint_kw) -> PlanResult:
    db = RecipeDB(recipes=RECIPES)
    constraints = UserConstraints(people=people, days=days, dishes_per_day=dishes_per_day,
                                  cook_start=constraint_kw.pop("cook_start", "18:30"),
                                  **constraint_kw)
    day_plans = []
    for day in range(1, days + 1):
        picks = [RECIPES[(day - 1 + i) % len(RECIPES)] for i in range(dishes_per_day)]
        day_plans.append(DayPlan(day=day, dishes=[ChosenDish(recipe_id=r.id, reason="快手又下饭")
                                                  for r in picks]))
    return PlanResult(
        constraints=constraints, candidate_count=len(db.recipes), days=day_plans,
        shopping=[ShoppingItem(name="番茄", category="蔬菜", amount="4个",
                               for_recipes=["番茄炒蛋"]),
                  ShoppingItem(name="青菜", category="蔬菜", amount="2把",
                               for_recipes=["清炒时蔬"]),
                  ShoppingItem(name="排骨", category="肉蛋", amount="600克",
                               for_recipes=["红烧排骨"])],
        estimated_cost_yuan=80.0)


def _db_url() -> str:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = (TMP_ROOT / f"api_{uuid4().hex[:8]}.db").as_posix()
    return f"sqlite+aiosqlite:///{path}"


@pytest_asyncio.fixture()
async def api(monkeypatch):
    """(client, app) —— 指向临时库、已种数据的接口客户端。"""
    monkeypatch.setenv("STORAGE", "db")
    monkeypatch.setenv("DATABASE_URL", _db_url())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    reset_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await RecipeRepo.upsert_many(RECIPES)
    await ProfileRepo.save_profile(PROFILE)
    record = await PlanRepo.save_plan(build_result(), START, "第一次排的")
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app, record
    await engine.dispose()
    reset_engine()


@pytest_asyncio.fixture()
async def client(api):
    return api[0]


@pytest.fixture()
def result_factory():
    """造一份排菜结果（需要不同形态的测试用，例如"全是便宜菜"）。"""
    return build_result


@pytest.fixture()
def start_date():
    return START
