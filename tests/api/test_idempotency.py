"""P1-4 验收（3/3）：幂等中间件（docs/08 §5 中间件 5）。

用 `POST /api/v1/_test/count`（测试专用计数器）证明"到底执行了几次" ——
这比看响应体更硬：回放的话计数器不会涨。
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, FastAPI
from sqlalchemy import select

from recipe_planner.api.errors import install_error_handlers
from recipe_planner.api.main import create_app
from recipe_planner.api.middleware.idempotency import (IdempotencyMiddleware,
                                                       MemoryIdempotencyStore, store_key)
from recipe_planner.storage.engine import get_sessionmaker
from recipe_planner.storage.orm import IdempotencyKey

PROBLEM = "application/problem+json"


async def _client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _counter_app(store=None):
    """一个只有计数器/报错路由的 app：把幂等中间件单独拎出来测，不受业务逻辑干扰。"""
    app = FastAPI()
    install_error_handlers(app)
    state = {"n": 0}
    router = APIRouter()

    @router.post("/api/v1/_test/count")
    async def count(payload: dict | None = None) -> dict:
        state["n"] += 1
        return {"n": state["n"], "echo": (payload or {}).get("x")}

    @router.get("/api/v1/_test/count")
    async def count_get() -> dict:
        state["n"] += 1
        return {"n": state["n"]}

    @router.post("/api/v1/_test/boom")
    async def boom() -> dict:
        state["n"] += 1
        raise ValueError("这条路径专门用来失败")

    app.include_router(router)
    app.add_middleware(IdempotencyMiddleware,
                       store=store if store is not None else MemoryIdempotencyStore())
    return app, state


# ---------------------------------------------------------------- 基本回放

async def test_same_key_replays_instead_of_executing(api):
    app, state = _counter_app()
    async with await _client(app) as c:
        r1 = await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-1"},
                          json={"x": 1})
        r2 = await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-1"},
                          json={"x": 1})
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() == {"n": 1, "echo": 1}
    assert state["n"] == 1, "第二次不该真的执行"
    assert "idempotent-replay" not in r1.headers
    assert r2.headers["idempotent-replay"] == "true"


async def test_without_key_executes_every_time(api):
    app, state = _counter_app()
    async with await _client(app) as c:
        r1 = await c.post("/api/v1/_test/count", json={})
        r2 = await c.post("/api/v1/_test/count", json={})
    assert (r1.json()["n"], r2.json()["n"]) == (1, 2)
    assert state["n"] == 2


async def test_get_with_key_is_ignored(api):
    """幂等只对 POST 生效：GET 天然幂等，带 key 也不该回放。"""
    app, state = _counter_app()
    async with await _client(app) as c:
        r1 = await c.get("/api/v1/_test/count", headers={"Idempotency-Key": "k-get"})
        r2 = await c.get("/api/v1/_test/count", headers={"Idempotency-Key": "k-get"})
    assert (r1.json()["n"], r2.json()["n"]) == (1, 2)
    assert "idempotent-replay" not in r2.headers


# ---------------------------------------------------------------- 冲突与并发

async def test_same_key_different_body_is_rejected(api):
    app, state = _counter_app()
    async with await _client(app) as c:
        await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-2"}, json={"x": 1})
        r = await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-2"}, json={"x": 2})
    assert r.status_code == 409
    assert r.headers["content-type"].startswith(PROBLEM)
    body = r.json()
    assert body["code"] == "idempotency_key_reused"
    assert "换个新的 key" in body["message"]
    assert body["details"]["next_steps"]
    assert state["n"] == 1, "冲突的请求不能被执行"


async def test_in_flight_key_returns_409(api):
    """并发重试：同一个 key 还在处理中 → 409 + Retry-After，而不是重复干活。"""
    store = MemoryIdempotencyStore()
    app, state = _counter_app(store=store)
    key = store_key("/api/v1/_test/count", "k-3")
    await store.acquire(key, "/api/v1/_test/count", "whatever")     # 假装还在处理

    async with await _client(app) as c:
        r = await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-3"}, json={})
    assert r.status_code == 409
    assert r.headers["retry-after"] == "3"
    body = r.json()
    assert body["code"] == "in_progress"
    assert "还在处理中" in body["message"]
    assert body["details"]["next_steps"]
    assert state["n"] == 0, "进行中的请求不能再执行一遍"


# ---------------------------------------------------------------- 失败与过期

async def test_failed_request_releases_the_key(api):
    """失败不缓存：否则用户会永远卡在同一个错误上。"""
    app, state = _counter_app()
    async with await _client(app) as c:
        r1 = await c.post("/api/v1/_test/boom", headers={"Idempotency-Key": "k-4"})
        assert r1.status_code == 500
        # key 已释放 → 用同一个 key 再发，会真的执行（还是 500，但状态不是"回放"）
        r2 = await c.post("/api/v1/_test/boom", headers={"Idempotency-Key": "k-4"})
    assert r2.status_code == 500
    assert "idempotent-replay" not in r2.headers
    assert state["n"] == 2


async def test_store_entries_expire(api):
    now = {"t": 0.0}
    store = MemoryIdempotencyStore(ttl_seconds=60, now=lambda: now["t"])
    app, state = _counter_app(store=store)
    async with await _client(app) as c:
        await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-5"}, json={})
        now["t"] = 30.0
        r2 = await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-5"}, json={})
        assert r2.json()["n"] == 1 and r2.headers.get("idempotent-replay") == "true"

        now["t"] = 61.0                                   # 超过 24h 的模拟：过期后重新执行
        r3 = await c.post("/api/v1/_test/count", headers={"Idempotency-Key": "k-5"}, json={})
    assert r3.json()["n"] == 2
    assert "idempotent-replay" not in r3.headers


# ---------------------------------------------------------------- 端点隔离

async def test_same_key_on_two_endpoints_does_not_collide():
    """`idempotency_key` 表主键只有 key 一列，所以存进库的 key 必须带上端点。"""
    a = store_key("/api/v1/plans/p1/save-money", "same-key")
    b = store_key("/api/v1/plans/p1/rate", "same-key")
    assert a != b
    assert store_key("/api/v1/plans/p1/rate", "same-key") == b      # 稳定
    assert len(a) <= 64


# ---------------------------------------------------------------- 数据库落库

async def test_db_store_persists_the_key(api, monkeypatch):
    """db 模式下幂等记录要真的进 `idempotency_key` 表（重启进程也认账）。"""
    monkeypatch.setenv("STORAGE", "db")
    _, _, record = api
    async with await _client(create_app()) as c:
        r1 = await c.post(f"/api/v1/plans/{record.id}/save-money",
                          headers={"Idempotency-Key": "real-1"})
        assert r1.status_code == 200, r1.text
        r2 = await c.post(f"/api/v1/plans/{record.id}/save-money",
                          headers={"Idempotency-Key": "real-1"})
    assert r2.status_code == 200
    assert r2.headers.get("idempotent-replay") == "true"
    assert r2.json() == r1.json()

    key = store_key(f"/api/v1/plans/{record.id}/save-money", "real-1")
    async with get_sessionmaker()() as session:
        row = (await session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key))).scalar_one_or_none()
    assert row is not None
    assert row.status_code == 200
    assert row.endpoint == f"/api/v1/plans/{record.id}/save-money"


async def test_real_mutation_runs_once_under_replay(api, monkeypatch):
    """真接口上的回放：省钱的换法只发生一次（第二次是回放，菜单不再变）。"""
    monkeypatch.setenv("STORAGE", "db")
    _, _, record = api
    async with await _client(create_app()) as c:
        r1 = await c.post(f"/api/v1/plans/{record.id}/save-money",
                          headers={"Idempotency-Key": "money-1"})
        dishes_after_first = (await c.get(f"/api/v1/plans/{record.id}")).json()["days"]
        r2 = await c.post(f"/api/v1/plans/{record.id}/save-money",
                          headers={"Idempotency-Key": "money-1"})
        dishes_after_replay = (await c.get(f"/api/v1/plans/{record.id}")).json()["days"]

    assert r1.json() == r2.json()
    assert dishes_after_first == dishes_after_replay, "回放不该再改一次菜单"
