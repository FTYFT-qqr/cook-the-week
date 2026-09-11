"""中间件 1：`RequestIDMiddleware`（docs/08 §5）。"""
from __future__ import annotations

import re
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recipe_planner.infra.logging import reset_request_id, set_request_id

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def clean_request_id(raw: str) -> str:
    """外部传进来的 ID 只留安全字符并限长（防止把头注入进日志或响应头）。"""
    return _SAFE_ID.sub("", raw)[:64]


class RequestIDMiddleware:
    """读 `X-Request-ID`，没有就生成 UUID4；写进 contextvar 与响应头。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for key, value in scope.get("headers") or []:
            if key == b"x-request-id":
                incoming = clean_request_id(value.decode("latin-1"))
                break
        rid = incoming or uuid4().hex
        token = set_request_id(rid)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            reset_request_id(token)
