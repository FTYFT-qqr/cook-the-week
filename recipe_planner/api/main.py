"""FastAPI 应用工厂（docs/08 §5/§6，docs/09 P1-1/P1-2）。

中间件挂载顺序：Starlette 里**后 add 的在外层**，所以按 §5 的执行顺序倒着 add：

    执行：RequestID(1) → AccessLog(2) → Auth(3) → RateLimit(4) → Idempotency(5) → Session(6) → 路由
    add ：Session(6) → [P1-4 在这里插 5/4/3] → AccessLog(2) → RequestID(1)

错误处理（7）用异常处理器实现，见 `errors.install_error_handlers`。
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI

from recipe_planner.infra.logging import setup_logging

from .errors import install_error_handlers
from .middleware import AccessLogMiddleware, RequestIDMiddleware, SessionMiddleware
from .routes import health, plan_mutations, plans, profile, recipes

API_PREFIX = "/api/v1"
VERSION = "0.2.0"


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Recipe Planner API",
        version=VERSION,
        description="一周晚餐规划：菜谱 / 方案 / 今晚 / 口味档案。错误统一 problem+json。",
        openapi_tags=[{"name": "infra", "description": "存活与就绪"},
                      {"name": "recipes", "description": "菜谱库"},
                      {"name": "plans", "description": "方案与今晚"},
                      {"name": "profile", "description": "口味档案"}])

    install_error_handlers(app)

    app.add_middleware(SessionMiddleware)        # 6：请求级事务
    # P1-4 将在此处依次 add：Idempotency(5) → RateLimit(4) → Auth(3)
    app.add_middleware(AccessLogMiddleware)      # 2
    app.add_middleware(RequestIDMiddleware)      # 1（最外层）

    app.include_router(health.router)            # /health、/ready（不带前缀）

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(recipes.router)
    api.include_router(plans.router)
    # 写入路由放在只读之后：/plans/current 等固定路径必须先匹配
    api.include_router(plan_mutations.router)
    api.include_router(profile.router)
    app.include_router(api)
    return app


app = create_app()
