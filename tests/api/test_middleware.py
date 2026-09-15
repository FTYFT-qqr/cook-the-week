"""P1-1 验收：中间件 1（请求 ID）、2（访问日志）、6（请求级事务）、7（统一错误）。

运行：python -m pytest tests -q
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi import APIRouter

from recipe_planner.api.main import create_app
from recipe_planner.api.middleware.request_id import clean_request_id
from recipe_planner.api.errors import ApiError, ConflictError
from recipe_planner.storage.engine import get_sessionmaker, request_scope, session_scope
from recipe_planner.storage.orm import AppSetting

PROBLEM = "application/problem+json"


# ---------------------------------------------------------------- 中间件 1

async def test_request_id_is_generated_and_echoed(client):
    r = await client.get("/health")
    assert r.status_code == 200
    rid = r.headers.get("x-request-id", "")
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid), rid


async def test_request_id_from_client_is_reused_and_sanitized(client):
    r = await client.get("/health", headers={"X-Request-ID": "abc-123_XY"})
    assert r.headers["x-request-id"] == "abc-123_XY"

    # 危险字符（CR/LF、空格、中文）会被剔掉，避免头注入
    r2 = await client.get("/health", headers={"X-Request-ID": "bad id\r\nInjected: 1"})
    assert "\r" not in r2.headers["x-request-id"]
    assert "\n" not in r2.headers["x-request-id"]
    assert r2.headers["x-request-id"] == "badidInjected1"


def test_clean_request_id_limits_length():
    assert clean_request_id("a" * 200) == "a" * 64
    assert clean_request_id("....") == "...."


# ---------------------------------------------------------------- 中间件 2

class _JsonCollector(logging.Handler):
    """在**请求进行中**就格式化记录，这样 contextvar 里的 request_id 还没被还原。"""

    def __init__(self):
        super().__init__()
        self.payloads: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        from recipe_planner.infra.logging import JsonFormatter

        try:
            self.payloads.append(json.loads(JsonFormatter().format(record)))
        except Exception:                     # 格式化失败不能污染被测代码
            pass


async def test_access_log_is_json_with_request_id(client):
    collector = _JsonCollector()
    access_logger = logging.getLogger("recipe_planner.access")
    access_logger.addHandler(collector)
    try:
        r = await client.get("/health")
    finally:
        access_logger.removeHandler(collector)
    rid = r.headers["x-request-id"]

    mine = [p for p in collector.payloads if p.get("msg") == "请求完成"]
    assert mine, "没有产生访问日志"
    payload = mine[-1]
    assert payload["method"] == "GET"
    assert payload["path"] == "/health"
    assert payload["status"] == 200
    assert payload["bytes"] > 0
    assert payload["latency_ms"] >= 0
    assert payload["level"] == "INFO"
    # 关键：日志里的 request_id 必须和响应头是同一个（一次请求可追）
    assert payload["request_id"] == rid


def test_log_formatter_falls_back_when_no_request():
    """请求之外打的日志不该崩，request_id 显示为 -。"""
    from recipe_planner.infra.logging import JsonFormatter

    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    assert json.loads(JsonFormatter().format(rec))["request_id"] == "-"


# ---------------------------------------------------------------- 中间件 7

async def test_404_is_problem_json_with_human_message_and_next_steps(client):
    r = await client.get("/api/v1/plans/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM)
    body = r.json()
    assert body["code"] == "plan_not_found"
    assert body["status"] == 404
    assert body["request_id"] == r.headers["x-request-id"]
    assert body["details"]["next_steps"][0]["label"] == "看看还有哪些方案"
    # 人话：不出现异常类名、堆栈、英文 key
    assert "Traceback" not in body["message"] and "Error" not in body["message"]
    assert body["message"].endswith("。")


async def test_unknown_path_is_problem_json(client):
    r = await client.get("/api/v1/不存在的地址")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM)
    assert r.json()["code"] == "not_found"


async def test_validation_error_lists_fields(client):
    r = await client.get("/api/v1/recipes", params={"limit": 0})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_request"
    assert any("limit" in f for f in body["details"]["fields"]), body["details"]["fields"]
    assert body["details"]["next_steps"][0]["label"] == "按提示改一下再提交"
    assert "limit" in body["message"]


# ---------------------------------------------------------------- 中间件 6

def _app_with_test_routes() -> object:
    """在工厂产出的 app 上挂几条只有测试才有的路由（走同一套中间件）。"""
    app = create_app()

    async def boom_unhandled():
        raise ValueError("内部细节不应该出现在响应里")

    async def write_then_conflict(api_flag: bool = False):
        async with session_scope() as session:
            session.add(AppSetting(key="k_conflict", value={"v": 1}))
        raise ConflictError("这一步现在做不了。", next_steps=[{"op": "retry", "label": "再试一次"}])

    async def write_then_ok():
        async with session_scope() as session:
            session.add(AppSetting(key="k_ok", value={"v": 1}))
        return {"ok": True}

    async def who_is_my_session():
        """两次仓储调用是否共享同一个会话（同一请求一个事务）。"""
        from recipe_planner.infra.logging import request_id
        from recipe_planner.storage import engine as engine_mod

        async with session_scope() as s1:
            async with session_scope() as s2:
                return {"same": s1 is s2, "request_id": request_id()}

    async def write_then_read_in_same_request():
        """同一请求里刚写入、还没提交的数据，能不能被后续仓储调用看到。"""
        async with session_scope() as session:
            session.add(AppSetting(key="k_visible", value={"v": 7}))
        from recipe_planner.storage.orm import AppSetting as M
        from sqlalchemy import select

        async with session_scope() as session:
            row = (await session.execute(select(M).where(M.key == "k_visible"))).scalar_one_or_none()
        return {"visible": row is not None}

    router = APIRouter()
    router.add_api_route("/_test/boom", boom_unhandled, methods=["GET"])
    router.add_api_route("/_test/conflict", write_then_conflict, methods=["GET"])
    router.add_api_route("/_test/ok", write_then_ok, methods=["GET"])
    router.add_api_route("/_test/session", who_is_my_session, methods=["GET"])
    router.add_api_route("/_test/visible", write_then_read_in_same_request, methods=["GET"])
    app.include_router(router)
    return app


async def _client_for(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_unhandled_exception_is_problem_json_without_stacktrace(api):
    _, _, _ = api
    app = _app_with_test_routes()
    async with await _client_for(app) as c:
        r = await c.get("/_test/boom")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith(PROBLEM)
    body = r.json()
    assert body["code"] == "internal_error"
    assert "内部细节" not in json.dumps(body, ensure_ascii=False)
    assert "ValueError" not in json.dumps(body, ensure_ascii=False)
    assert body["details"]["next_steps"]


async def test_error_response_rolls_back_the_transaction(api):
    _, _, _ = api
    app = _app_with_test_routes()
    async with await _client_for(app) as c:
        r = await c.get("/_test/conflict")
        assert r.status_code == 409
        assert r.json()["code"] == "conflict"
    # 出错的那次请求写的行必须被回滚掉
    from sqlalchemy import select

    from recipe_planner.storage.orm import AppSetting as M

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(M).where(M.key == "k_conflict"))).scalars().all()
    assert rows == [], "409 响应之后写入没有被回滚"


async def test_success_response_commits(api):
    _, _, _ = api
    app = _app_with_test_routes()
    async with await _client_for(app) as c:
        r = await c.get("/_test/ok")
        assert r.status_code == 200
    from sqlalchemy import select

    from recipe_planner.storage.orm import AppSetting as M

    async with get_sessionmaker()() as session:
        row = (await session.execute(select(M).where(M.key == "k_ok"))).scalar_one_or_none()
    assert row is not None, "200 响应的写入没有提交"


async def test_one_session_per_request(api):
    _, _, _ = api
    app = _app_with_test_routes()
    async with await _client_for(app) as c:
        r = await c.get("/_test/session")
    body = r.json()
    assert body["same"] is True, "同一请求内的两次 session_scope 不是同一个会话"
    assert body["request_id"] == r.headers["x-request-id"]


async def test_writes_visible_inside_same_request(api):
    _, _, _ = api
    app = _app_with_test_routes()
    async with await _client_for(app) as c:
        r = await c.get("/_test/visible")
    assert r.json()["visible"] is True


async def test_request_scope_resets_after_request(api):
    """请求结束后不能把会话留在 contextvar 里（否则后面的读会跑到已关闭的会话上）。"""
    _, _, _ = api
    from recipe_planner.storage import engine as engine_mod

    assert engine_mod._request_session.get() is None
    app = _app_with_test_routes()
    async with await _client_for(app) as c:
        await c.get("/_test/ok")
    assert engine_mod._request_session.get() is None


def test_request_scope_is_reentrant_safe():
    """request_scope 是同步上下文管理器：进出必须成对，异常也要还原。"""
    from recipe_planner.storage import engine as engine_mod

    sentinel = object()
    try:
        with request_scope(sentinel):     # type: ignore[arg-type]
            assert engine_mod._request_session.get() is sentinel
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert engine_mod._request_session.get() is None
