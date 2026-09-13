"""P1-4 验收（2/3）：限流中间件（docs/08 §5 中间件 4）。

- 普通接口 60/分钟；`POST /api/v1/plans` 单独 6/分钟（第 7 次必须 429）；
- 429 要带 `Retry-After`，并且仍然是 problem+json（人话 + 可点击下一步）；
- 探针（/health、/ready）不限流。
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter

from recipe_planner.api.main import create_app
from recipe_planner.api.middleware.ratelimit import TokenBucket, route_class

PROBLEM = "application/problem+json"


async def _client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _app_with_plan_route():
    """用**真实的** `POST /api/v1/plans`（P1-5 已经做出来了）验证"贵接口单独一个桶"。"""
    return create_app()


# ---------------------------------------------------------------- 令牌桶本身

def test_token_bucket_refills_over_time():
    now = {"t": 1000.0}
    bucket = TokenBucket(capacity=6, per_minute=6, now=lambda: now["t"])
    for _ in range(6):
        allowed, _wait = bucket.take()
        assert allowed
    allowed, wait = bucket.take()
    assert not allowed and 9 < wait <= 10, wait          # 6/分钟 = 10 秒一个

    now["t"] += 10.0                                     # 过 10 秒刚好回一个
    assert bucket.take()[0] is True
    assert bucket.take()[0] is False

    now["t"] += 3600.0                                   # 放很久：回满但不超容量
    assert bucket.capacity == 6
    for _ in range(6):
        assert bucket.take()[0] is True
    assert bucket.take()[0] is False


def test_route_classification():
    assert route_class("POST", "/api/v1/plans") == "expensive"
    assert route_class("GET", "/api/v1/plans") == "normal"
    assert route_class("POST", "/api/v1/plans/abc/save-money") == "normal"


# ---------------------------------------------------------------- 贵接口

async def test_seventh_plan_request_is_429(api, fast_runner, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT", "on")
    monkeypatch.delenv("RATE_LIMIT_PLAN_PER_MIN", raising=False)   # 用默认 6
    async with await _client(_app_with_plan_route()) as c:
        for i in range(6):
            r = await c.post("/api/v1/plans", json={})
            assert r.status_code == 202, f"第 {i + 1} 次不该被拦：{r.text}"

        r7 = await c.post("/api/v1/plans", json={})
        assert r7.status_code == 429
        assert r7.headers["content-type"].startswith(PROBLEM)
        retry_after = int(r7.headers["retry-after"])
        assert retry_after >= 1
        body = r7.json()
        assert body["code"] == "rate_limited"
        assert str(retry_after) in body["message"]           # 人话里说清等多久
        assert body["details"]["bucket"] == "expensive"
        assert body["details"]["limit_per_min"] == 6
        assert body["details"]["retry_after_sec"] == retry_after
        assert body["details"]["next_steps"]


async def test_normal_bucket_is_separate_from_expensive(api, fast_runner, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT", "on")
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "5")
    async with await _client(_app_with_plan_route()) as c:
        for _ in range(5):
            assert (await c.get("/api/v1/recipes")).status_code == 200
        blocked = await c.get("/api/v1/recipes")
        assert blocked.status_code == 429
        assert blocked.json()["details"]["bucket"] == "normal"
        # 普通桶满了，不影响贵接口的桶
        assert (await c.post("/api/v1/plans", json={})).status_code == 202


async def test_probes_are_not_rate_limited(api, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "3")
    async with await _client(_app_with_plan_route()) as c:
        for _ in range(30):
            assert (await c.get("/health")).status_code == 200
            assert (await c.get("/ready")).status_code == 200


async def test_identities_get_their_own_buckets(api, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT", "on")
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "2")
    async with await _client(_app_with_plan_route()) as c:
        a = {"X-API-Key": "family-a"}
        b = {"X-API-Key": "family-b"}
        assert (await c.get("/api/v1/recipes", headers=a)).status_code == 200
        assert (await c.get("/api/v1/recipes", headers=a)).status_code == 200
        assert (await c.get("/api/v1/recipes", headers=a)).status_code == 429
        # 另一家不受影响
        assert (await c.get("/api/v1/recipes", headers=b)).status_code == 200


async def test_rate_limit_can_be_switched_off(api, fast_runner, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT", "off")
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "1")
    async with await _client(_app_with_plan_route()) as c:
        for _ in range(12):
            assert (await c.post("/api/v1/plans", json={})).status_code == 202
