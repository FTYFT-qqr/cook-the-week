"""中间件 6：`SessionMiddleware` —— 每请求一个 AsyncSession（Unit of Work）。

正常提交、异常回滚。仓储方法里的 `session_scope()` 会复用这里的会话（见 `engine.request_scope`），
所以一次请求里的多次仓储调用共享同一个事务。

**为什么还要看响应码**：Starlette 的异常处理器（我们注册的 problem+json 那一套）跑在本中间件的
**内层**，路由抛出的领域异常会被就地转成 4xx/5xx 响应，本中间件根本看不到异常。如果只看异常，
"抛错"的请求照样会被提交。所以规则是：**响应码 >= 400 一律回滚**（这次请求不算数），
只有 2xx/3xx 才提交。

注意：提交发生在响应发出**之后**，因此写接口自己要在返回前 `await session.commit()`
（P1-3 起遵守），否则客户端可能先看到 200、实际却没落库。
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recipe_planner.storage.engine import get_sessionmaker, request_scope


class SessionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session = get_sessionmaker()()
        status = 0

        async def send_tracking(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            with request_scope(session):
                try:
                    await self.app(scope, receive, send_tracking)
                except Exception:
                    await session.rollback()
                    raise
                if status >= 400:
                    await session.rollback()
                else:
                    await session.commit()
        finally:
            await session.close()
