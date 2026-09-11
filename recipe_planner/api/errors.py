"""统一错误：领域异常 → `application/problem+json`（docs/08 §5 中间件 7）。

体例（05 §5.1 要求失败必须给"人话 + 可点击的下一步"）：

```json
{"code": "plan_not_found", "message": "没找到这份方案，可能已经被删掉了。",
 "request_id": "…", "status": 404,
 "details": {"next_steps": [{"op": "list_plans", "label": "看看还有哪些方案"}]}}
```

- `message` 一律是人话（不出现堆栈、不出现英文异常名）；
- `details.next_steps[]` 直接给前端可点击的放宽/重试选项；
- 未捕获异常只记日志、不回显堆栈。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from recipe_planner.actions import ActionError
from recipe_planner.infra.logging import log_event, request_id

logger = logging.getLogger("recipe_planner.api")

PROBLEM_MEDIA_TYPE = "application/problem+json"


def step(op: str, label: str, **extra: Any) -> dict:
    return {"op": op, "label": label, **extra}


class ApiError(Exception):
    """所有对外错误的基类。子类只改类属性即可。"""

    code = "internal_error"
    status = 500
    message = "出了点问题，我这边没能完成这一步。"

    def __init__(self, message: Optional[str] = None, *, details: Optional[dict] = None,
                 next_steps: Optional[list[dict]] = None, status: Optional[int] = None,
                 code: Optional[str] = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details: dict = dict(details or {})
        if next_steps:
            self.details["next_steps"] = next_steps
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class NotFoundError(ApiError):
    code = "not_found"
    status = 404
    message = "没找到你要的东西。"


class PlanNotFoundError(NotFoundError):
    code = "plan_not_found"
    message = "没找到这份方案，可能已经被删掉了。"


class ConflictError(ApiError):
    code = "conflict"
    status = 409
    message = "现在这个状态下做不了这件事。"


class ConfirmRequiredError(ConflictError):
    code = "confirm_required"
    message = "这一步会动到已有数据，需要你确认一下。"


class InvalidRequestError(ApiError):
    code = "invalid_request"
    status = 422
    message = "有个地方填得不太对。"


class UnavailableError(ApiError):
    code = "unavailable"
    status = 503
    message = "这个功能暂时用不了，稍后再试一次。"


_HTTP_CODES = {
    400: ("bad_request", "这个请求我没看懂。"),
    401: ("unauthorized", "这个部署需要 API Key。"),
    403: ("forbidden", "你没有权限做这件事。"),
    404: ("not_found", "这个地址不存在。"),
    405: ("method_not_allowed", "这个地址不接受这种请求方式。"),
    409: ("conflict", "现在这个状态下做不了这件事。"),
    429: ("rate_limited", "你点得有点快，缓一下再试。"),
    500: ("internal_error", "出了点问题，我这边没能完成这一步。"),
}


def problem_body(code: str, message: str, status: int, details: Optional[dict] = None,
                 rid: Optional[str] = None) -> dict:
    return {
        "code": code,
        "message": message,
        "request_id": rid if rid is not None else (request_id() or "-"),
        "status": status,
        "details": details or {},
    }


def problem_response(code: str, message: str, status: int, details: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(status_code=status, media_type=PROBLEM_MEDIA_TYPE,
                        content=problem_body(code, message, status, details))


def install_error_handlers(app: FastAPI) -> None:
    """注册全部异常处理器（等价于"中间件 7"）。"""

    @app.exception_handler(ActionError)
    async def _action_error(request: Request, exc: ActionError) -> JSONResponse:
        """领域层的"这件事做不了"：已经是人话 + 可点击的下一步，直接透出。"""
        return problem_response(exc.code, exc.message, exc.status,
                                {"next_steps": exc.next_steps})

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        if exc.status >= 500:
            log_event(logger, logging.ERROR, "接口出错", code=exc.code, path=request.url.path)
        return problem_response(exc.code, exc.message, exc.status, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_CODES.get(exc.status_code, ("http_error", "这个请求没能完成。"))
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
        return problem_response(code, detail or message, exc.status_code,
                                {"next_steps": [step("open_docs", "看看接口文档")]})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(x) for x in err.get("loc", []) if x != "body")
                  for err in exc.errors()]
        return problem_response(
            "invalid_request",
            "有几个参数填得不太对：" + "、".join(f for f in fields if f) if fields
            else "有个地方填得不太对。",
            422,
            {"fields": fields,
             "next_steps": [step("fix_fields", "按提示改一下再提交")]})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 堆栈只进日志，不进响应体（08 §5 中间件 7）
        log_event(logger, logging.ERROR, "未捕获异常", path=request.url.path,
                  error=type(exc).__name__)
        logger.exception("未捕获异常: %s %s", request.method, request.url.path)
        return problem_response(
            "internal_error", "出了点问题，我这边没能完成这一步。", 500,
            {"next_steps": [step("retry", "再试一次"), step("go_home", "回到今晚")]})
