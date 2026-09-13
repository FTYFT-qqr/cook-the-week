"""中间件 3：`AuthMiddleware` —— 家庭级 API Key（docs/08 §5、docs/08 §13 决策 2）。

- `AUTH_MODE=off`（默认，本机）：直接放行，但仍把 household 放进 contextvar 供日志用；
- `AUTH_MODE=apikey`：要求 `X-API-Key`，用 `secrets.compare_digest` 常数时间比较；
- **apikey 模式但没配 `API_KEY` → 一律 503**（宁可不可用，也不能"以为开了认证其实没开"）；
- `/health`、`/ready`、`/docs`、`/openapi.json` 免认证（探针和文档要能打开）。

决策 2 的原话是"一旦局域网/公网可访问，必须切 apikey"——所以这里的默认值是"关"，
但**只要切到 apikey 就绝不放行没有 key 的请求**，不留"配错了等于没认证"的口子。
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from recipe_planner.infra import settings
from recipe_planner.infra.logging import log_event, reset_household, set_household

from ..errors import send_problem

logger = logging.getLogger("recipe_planner.auth")

EXEMPT_PATHS = {"/health", "/ready", "/docs", "/redoc", "/openapi.json",
                "/docs/oauth2-redirect"}
DEFAULT_HOUSEHOLD = "household_default"


def is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1").strip()
    return ""


class AuthMiddleware:
    def __init__(self, app: ASGIApp, mode: Optional[str] = None,
                 api_key: Optional[str] = None) -> None:
        self.app = app
        self.mode = (mode if mode is not None else settings.auth_mode()).lower()
        self.api_key = api_key if api_key is not None else settings.api_key()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if self.mode != "apikey" or is_exempt(path):
            await self.app(scope, receive, send)
            return

        if not self.api_key:
            log_event(logger, logging.ERROR, "AUTH_MODE=apikey 但没配 API_KEY，已拒绝请求",
                      path=path)
            await send_problem(
                send, "auth_not_configured",
                "这个部署开了 API Key 校验，但服务端没配好密钥，所以谁都进不来。"
                "请联系部署的人设置 API_KEY 后重启。", 503,
                {"next_steps": [{"op": "contact_owner", "label": "找部署的人配好 API_KEY"},
                                {"op": "check_ready", "label": "看看服务状态"}]})
            return

        provided = _header(scope, b"x-api-key")
        if not provided or not secrets.compare_digest(provided, self.api_key):
            log_event(logger, logging.WARNING, "API Key 校验失败",
                      path=path, had_key=bool(provided))
            await send_problem(
                send, "unauthorized",
                "这个部署需要 API Key 才能访问。请在请求头带上 X-API-Key。", 401,
                {"next_steps": [{"op": "add_api_key", "label": "在请求头加上 X-API-Key"},
                                {"op": "read_docs", "label": "看看接口文档"}]})
            return

        token = set_household(DEFAULT_HOUSEHOLD)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_household(token)
