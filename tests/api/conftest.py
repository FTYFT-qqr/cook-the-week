"""接口层测试的公共夹具（docs/08 §11：pytest + httpx.AsyncClient）。

- 每个测试一份**临时 SQLite 库**（不碰 `data/app.db`），用 `create_all` 建表
  （"迁移 == ORM" 已由 `tests/test_migrations.py` 单独证明，这里不重复验）；
- 种几道菜 + 一份 3 天方案 + 一份档案；
- `raise_app_exceptions=False`：这样才能拿到我们自己的 500 problem+json 响应体。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from recipe_planner.api.main import create_app
from recipe_planner.api.worker import InProcessRunner, set_runner
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

# 种子周的周一傍晚：**接口层的"现在"在测试里被钉死在这一刻**（docs/11 §3.3 P1-6 日期炸弹）。
PINNED_NOW = datetime(2026, 9, 14, 18, 0)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """把接口层的「现在」钉死在种子周里。

    不钉的话，「今晚」的状态判定会拿**真实今天**去比一个写死的种子周：
    过了 2026-09-21，同一批断言会集体从「计划中」翻成「这周已结束」——
    测试随日历变红变绿，是最坏的一种"绿"。

    注入点只有一处：`recipe_planner/api/clock.py`（接口层不许直接写 `datetime.now()`）。
    """
    from recipe_planner.api import clock

    monkeypatch.setattr(clock, "now", lambda: PINNED_NOW)
    monkeypatch.setattr(clock, "today", lambda: PINNED_NOW.date())
    return PINNED_NOW


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


# ---------------------------------------------------------------- 任务（P1-5）

class FakeGraph:
    """假流水线：逐节点吐 update，可以精确控制"跑多久 / 在哪失败 / 有没有结果"。

    沿用 `scripts/self_check.py` 里 `_SlowGraph` 的手法 —— 不碰真实 LLM，
    但走的是和真图一模一样的 `stream(init, stream_mode="updates")` 协议。
    """

    NODES = ["retrieve", "plan", "validate", "shopping", "answer"]

    def __init__(self, *, delay: float = 0.0, raise_at: str | None = None,
                 empty: bool = False, nodes: list[str] | None = None) -> None:
        self.delay = delay
        self.raise_at = raise_at
        self.empty = empty
        self.nodes = nodes or list(self.NODES)

    def stream(self, init: dict, stream_mode: str = "updates"):
        from recipe_planner.models import ChosenDish, DayPlan, PlanResult

        for node in self.nodes:
            if self.delay:
                time.sleep(self.delay)
            if node == self.raise_at:
                raise RuntimeError("假图故意失败")
            if node == "answer":
                result = None
                if not self.empty:
                    c = init["constraints"]
                    result = PlanResult(
                        constraints=c, candidate_count=5,
                        days=[DayPlan(day=d, dishes=[ChosenDish(recipe_id="r1", reason="假图"),
                                                     ChosenDish(recipe_id="r2", reason="假图")])
                              for d in range(1, c.days + 1)],
                        shopping=[], llm_used=False)
                yield {"answer": {"result": result}}
            else:
                yield {node: {}}


def fake_graph_factory(**kwargs):
    return lambda db: FakeGraph(**kwargs)


@pytest.fixture()
def runner_factory(api):
    """装一个可配置的执行器，用例结束还原（避免工作线程泄漏到别的用例）。"""
    installed = []

    def _install(**kwargs) -> InProcessRunner:
        kwargs.setdefault("graph_factory", fake_graph_factory())
        runner = InProcessRunner(**kwargs)
        installed.append(runner)
        set_runner(runner)
        return runner

    yield _install
    for runner in installed:
        runner.shutdown()
    set_runner(None)


@pytest.fixture()
def fast_runner(runner_factory):
    """瞬时完成的执行器：只关心"接口返回什么"的用例用它，别让真图跑起来。"""
    return runner_factory(graph_factory=fake_graph_factory())


async def wait_job(client, job_id: str, timeout: float = 15.0) -> dict:
    """轮询到终态（succeeded / failed / cancelled）。"""
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        body = (await client.get(f"/api/v1/jobs/{job_id}")).json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        # 轮询间隔别太密：限流中间件是 60 次/分钟，0.05 秒一次（20 次/秒）几下就把桶抽干，
        # 之后所有轮询都吃 429 —— 任务其实早就结束了，测试却以为它没结束。
        await asyncio.sleep(0.5)
    raise AssertionError(f"任务 {job_id} 在 {timeout}s 内没有结束，最后状态：{body}")


@pytest.fixture()
def graph_factory():
    """假流水线工厂（`tests/api` 不是包，所以统一用夹具传函数，不做相对导入）。"""
    return fake_graph_factory


@pytest.fixture()
def wait_for_job():
    return wait_job
