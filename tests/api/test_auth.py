"""P1-4 验收（1/3）：认证中间件（docs/08 §5 中间件 3、§13 决策 2）。

要点：本机默认不校验；一旦切到 `apikey`，**没配密钥就一律 503**（宁可不可用，
也不能出现"以为开了认证其实没开"）。
"""
from __future__ import annotations

import httpx

from recipe_planner.api.main import create_app

PROBLEM = "application/problem+json"


async def _client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_off_by_default_lets_everything_through(api, monkeypatch, client):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert (await client.get("/api/v1/recipes")).status_code == 200
    assert (await client.get("/api/v1/profile")).status_code == 200


async def test_apikey_mode_requires_the_header(api, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "apikey")
    monkeypatch.setenv("API_KEY", "s3cret-key")
    async with await _client(create_app()) as c:
        # 没有 key
        r = await c.get("/api/v1/recipes")
        assert r.status_code == 401
        assert r.headers["content-type"].startswith(PROBLEM)
        body = r.json()
        assert body["code"] == "unauthorized"
        assert "X-API-Key" in body["message"]
        assert body["details"]["next_steps"]
        assert "s3cret-key" not in r.text, "不能把期望的密钥回显出去"
        assert body["request_id"] == r.headers["x-request-id"]

        # key 不对（只差一个字符也要拒）
        assert (await c.get("/api/v1/recipes", headers={"X-API-Key": "s3cret-ke"})).status_code == 401
        assert (await c.get("/api/v1/recipes", headers={"X-API-Key": ""})).status_code == 401

        # 对了就放行
        ok = await c.get("/api/v1/recipes", headers={"X-API-Key": "s3cret-key"})
        assert ok.status_code == 200
        assert ok.json()["total"] == 5


async def test_probes_and_docs_stay_open(api, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "apikey")
    monkeypatch.setenv("API_KEY", "s3cret-key")
    async with await _client(create_app()) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/ready")).status_code == 200
        assert (await c.get("/openapi.json")).status_code == 200
        assert (await c.get("/docs")).status_code == 200


async def test_apikey_mode_without_configured_key_fails_closed(api, monkeypatch):
    """配错就必须挡住，而不是"没配=不校验"。"""
    monkeypatch.setenv("AUTH_MODE", "apikey")
    monkeypatch.delenv("API_KEY", raising=False)
    async with await _client(create_app()) as c:
        r = await c.get("/api/v1/recipes")
        assert r.status_code == 503
        body = r.json()
        assert body["code"] == "auth_not_configured"
        assert "没配好密钥" in body["message"]
        assert body["details"]["next_steps"]

        # 探针仍然能回答，但 /ready 里要如实报告 auth 没配好
        assert (await c.get("/health")).status_code == 200
        ready = await c.get("/ready")
        assert ready.status_code == 503
        auth_check = next(x for x in ready.json()["checks"] if x["name"] == "auth")
        assert auth_check["ok"] is False and "API_KEY" in auth_check["detail"]


async def test_ready_reports_auth_mode(api, client):
    """默认本机模式：/ready 里 auth 是 ok 的，并说明"不校验"。"""
    body = (await client.get("/ready")).json()
    auth_check = next(x for x in body["checks"] if x["name"] == "auth")
    assert auth_check["ok"] is True
    assert "不校验" in auth_check["detail"]


async def test_write_routes_are_protected_too(api, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "apikey")
    monkeypatch.setenv("API_KEY", "s3cret-key")
    _, _, record = api
    async with await _client(create_app()) as c:
        r = await c.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "skip"})
        assert r.status_code == 401
        # 带上 key 才改得动
        r2 = await c.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "skip"},
                           headers={"X-API-Key": "s3cret-key"})
        assert r2.status_code == 200
