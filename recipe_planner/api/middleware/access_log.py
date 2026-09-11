"""中间件 2：`AccessLogMiddleware` —— 结构化访问日志（docs/08 §5/§8）。

字段：`ts/level/request_id/method/path/status/latency_ms/household/bytes`；>2s 提升为 warn。
`request_id` 由日志 formatter 自动补，这里只管业务字段。
"""
from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recipe_planner.infra.logging import log_event

logger = logging.getLogger("recipe_planner.access")

SLOW_MS = 2000.0


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp, slow_ms: float = SLOW_MS) -> None:
        self.app = app
        self.slow_ms = slow_ms

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0
        size = 0

        async def send_counting(message: Message) -> None:
            nonlocal status, size
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body":
                size += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_counting)
        finally:
            latency = (time.perf_counter() - started) * 1000.0
            log_event(logger,
                      logging.WARNING if latency >= self.slow_ms else logging.INFO,
                      "请求完成",
                      method=scope.get("method", "-"),
                      path=scope.get("path", "-"),
                      status=status,
                      latency_ms=round(latency, 1),
                      bytes=size)
