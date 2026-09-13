"""中间件 5：`IdempotencyMiddleware` —— `Idempotency-Key`（docs/08 §5 中间件 5）。

规则（对 `POST` 且带 `Idempotency-Key` 的请求生效）：

| 情况 | 行为 |
|---|---|
| 第一次见这个 key | 正常执行，**成功后**把响应存下来（默认留 24h） |
| 见过、且当时成功了 | 直接返回原响应，加 `Idempotent-Replay: true`，**不再执行** |
| 正在进行中（并发重试） | 409 `in_progress` + `Retry-After`，让客户端等一会儿 |
| 同 key 但请求体不一样 | 409 `idempotency_key_reused`（这是客户端用错了，不能默默按老结果回） |
| 执行结果是 4xx/5xx | **释放 key**，客户端换个姿势重试不受影响 |
| `text/event-stream` 响应 | 不缓存（SSE 是流，不能拿旧流回放） |

三个实现细节值得记住：
1. **key 里必须带端点**：`idempotency_key` 表的主键只有 `key` 一列，
   所以存进去的是 `sha256(端点 + 客户端 key)` 的前 40 位 —— 否则同一个 key 用在两个接口上会互相回放。
2. **只缓存 2xx**：把失败也缓存起来，用户会永远卡在同一个错误上。
3. **这里用的会话是独立的**：本中间件在 `SessionMiddleware` 外层，`session_scope()` 会自己开一个短事务，
   这样"已经收到了请求"这件事不会被业务事务的回滚带走。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable, Optional, Protocol

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recipe_planner.infra import settings
from recipe_planner.infra.logging import log_event

from ..errors import send_problem

logger = logging.getLogger("recipe_planner.idempotency")

TTL_SECONDS = 24 * 3600
MAX_BODY_BYTES = 256 * 1024


def store_key(endpoint: str, raw_key: str) -> str:
    """端点内唯一的存储键（见模块文档第 1 点）。"""
    digest = hashlib.sha256(f"{endpoint}|{raw_key}".encode("utf-8")).hexdigest()
    return digest[:40]


def payload_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()[:16]


class IdempotencyStore(Protocol):
    async def lookup(self, key: str) -> Optional[dict]: ...
    async def acquire(self, key: str, endpoint: str, body_hash: str) -> str: ...
    async def complete(self, key: str, endpoint: str, status: int, body: Any,
                       body_hash: str) -> None: ...
    async def release(self, key: str) -> None: ...


class MemoryIdempotencyStore:
    """单进程内存实现（`STORAGE=json` 或测试用）。"""

    def __init__(self, ttl_seconds: int = TTL_SECONDS,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self._now = now
        self._rows: dict[str, dict] = {}

    def _fresh(self, row: Optional[dict]) -> Optional[dict]:
        if row is None:
            return None
        if self._now() - row["at"] > self.ttl:
            return None
        return row

    async def lookup(self, key: str) -> Optional[dict]:
        row = self._fresh(self._rows.get(key))
        if row is None:
            self._rows.pop(key, None)
        return row

    async def acquire(self, key: str, endpoint: str, body_hash: str) -> str:
        row = await self.lookup(key)
        if row is None:
            self._rows[key] = {"endpoint": endpoint, "status": None, "body": None,
                               "body_hash": body_hash, "at": self._now()}
            return "acquired"
        return "inflight" if row["status"] is None else "done"

    async def complete(self, key: str, endpoint: str, status: int, body: Any,
                       body_hash: str) -> None:
        self._rows[key] = {"endpoint": endpoint, "status": status, "body": body,
                           "body_hash": body_hash, "at": self._now()}

    async def release(self, key: str) -> None:
        self._rows.pop(key, None)


class DbIdempotencyStore:
    """数据库实现：用 `idempotency_key` 表（docs/08 §3.2）。status_code 为空 = 进行中。"""

    def __init__(self, ttl_seconds: int = TTL_SECONDS) -> None:
        self.ttl = ttl_seconds

    async def lookup(self, key: str) -> Optional[dict]:
        from recipe_planner.storage.engine import session_scope
        from recipe_planner.storage.orm import IdempotencyKey

        async with session_scope() as session:
            row = await session.get(IdempotencyKey, key)
            if row is None:
                return None
            created = row.created_at.timestamp() if row.created_at else 0.0
            if time.time() - created > self.ttl:
                await session.delete(row)
                return None
            payload = dict(row.response or {})
            return {"endpoint": row.endpoint, "status": row.status_code,
                    "body": payload.get("body"), "body_hash": payload.get("body_hash", ""),
                    "at": created}

    async def acquire(self, key: str, endpoint: str, body_hash: str) -> str:
        from sqlalchemy.exc import IntegrityError

        from recipe_planner.storage.engine import session_scope
        from recipe_planner.storage.orm import IdempotencyKey

        existing = await self.lookup(key)
        if existing is not None:
            return "inflight" if existing["status"] is None else "done"
        try:
            async with session_scope() as session:
                session.add(IdempotencyKey(key=key, endpoint=endpoint, status_code=None,
                                           response={"body_hash": body_hash}))
        except IntegrityError:
            # 并发抢同一个 key：另一个请求刚插进去 → 当作"进行中"
            return "inflight"
        return "acquired"

    async def complete(self, key: str, endpoint: str, status: int, body: Any,
                       body_hash: str) -> None:
        from recipe_planner.storage.engine import session_scope
        from recipe_planner.storage.orm import IdempotencyKey

        async with session_scope() as session:
            row = await session.get(IdempotencyKey, key)
            payload = {"body": body, "body_hash": body_hash}
            if row is None:
                session.add(IdempotencyKey(key=key, endpoint=endpoint, status_code=status,
                                           response=payload))
            else:
                row.status_code = status
                row.response = payload

    async def release(self, key: str) -> None:
        from recipe_planner.storage.engine import session_scope
        from recipe_planner.storage.orm import IdempotencyKey

        async with session_scope() as session:
            row = await session.get(IdempotencyKey, key)
            if row is not None:
                await session.delete(row)


def default_store() -> IdempotencyStore:
    ttl = settings.idempotency_ttl_hours() * 3600
    if settings.storage_kind() == "db":
        return DbIdempotencyStore(ttl)
    return MemoryIdempotencyStore(ttl)


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp, store: Optional[IdempotencyStore] = None) -> None:
        self.app = app
        self.store = store or default_store()
        self.ttl = settings.idempotency_ttl_hours() * 3600

    # ---------------------------------------------------------- 请求体

    @staticmethod
    async def _read_body(receive: Receive) -> tuple[bytes, Receive]:
        """把请求体读完并**原样回放**给下游（ASGI 的 receive 只能消费一次）。"""
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body") or b"")
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return body, replay

    # ---------------------------------------------------------- 主流程

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() != "POST":
            await self.app(scope, receive, send)
            return
        raw_key = ""
        for key, value in scope.get("headers") or []:
            if key == b"idempotency-key":
                raw_key = value.decode("latin-1").strip()
                break
        if not raw_key:
            await self.app(scope, receive, send)
            return

        endpoint = scope.get("path", "")
        body, replay = await self._read_body(receive)
        key = store_key(endpoint, raw_key)
        body_hash = payload_hash(body)

        existing = await self.store.lookup(key)
        if existing is not None and existing.get("status") is not None:
            if existing.get("body_hash") and existing["body_hash"] != body_hash:
                await send_problem(
                    send, "idempotency_key_reused",
                    "这个 Idempotency-Key 刚才用在另一次请求上了。换个新的 key 再发一次。", 409,
                    {"next_steps": [{"op": "new_key", "label": "换一个 Idempotency-Key 重发"}]})
                return
            log_event(logger, logging.INFO, "命中幂等键，直接回放上次的响应",
                      path=endpoint, status=int(existing["status"]))
            await self._replay(send, int(existing["status"]), existing.get("body"))
            return

        state = await self.store.acquire(key, endpoint, body_hash)
        if state == "inflight":
            log_event(logger, logging.WARNING, "同一个幂等键的请求还在处理中", path=endpoint)
            await send_problem(
                send, "in_progress",
                "同一件事我还在处理中，别急着重发。等几秒再看结果就行。", 409,
                {"retry_after_sec": 3,
                 "next_steps": [{"op": "wait", "label": "等 3 秒再看结果"},
                                {"op": "check_plan", "label": "看看方案是不是已经出来了"}]},
                headers=[(b"retry-after", b"3")])
            return

        captured: dict = {"status": 0, "chunks": [], "stream": False}

        async def send_capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = int(message["status"])
                for name, value in message.get("headers") or []:
                    if name == b"content-type" and b"event-stream" in value.lower():
                        captured["stream"] = True
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, replay, send_capture)
        except Exception:
            await self.store.release(key)          # 崩了就放掉，别把 key 卡死
            raise

        status = captured["status"]
        raw_body = b"".join(captured["chunks"])
        if captured["stream"] or len(raw_body) > MAX_BODY_BYTES:
            await self.store.release(key)          # 流式/超大响应不缓存
            return
        if 200 <= status < 300:
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
            except (UnicodeDecodeError, ValueError):
                payload = None
            if payload is None:
                await self.store.release(key)
                return
            await self.store.complete(key, endpoint, status, payload, body_hash)
        else:
            # 失败不缓存：否则用户会永远卡在同一个错误上
            await self.store.release(key)

    @staticmethod
    async def _replay(send: Send, status: int, body: Any) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode("latin-1")),
                                (b"idempotent-replay", b"true")]})
        await send({"type": "http.response.body", "body": payload})
