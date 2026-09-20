"""FastAPI 应用工厂（docs/08 §5/§6，docs/09 P1-1…P1-4）。

中间件挂载顺序：Starlette 里**后 add 的在外层**，所以按 §5 的执行顺序倒着 add：

    执行：RequestID(1) → AccessLog(2) → Auth(3) → RateLimit(4) → Idempotency(5) → Session(6) → 路由
    add ：Session(6) → Idempotency(5) → RateLimit(4) → Auth(3) → AccessLog(2) → RequestID(1)

顺序不能随意换，几处依赖关系：
- Auth 必须在 RateLimit 外层：限流要按 API Key 分桶（没认证就按 IP 兜底）；
- Idempotency 必须在 Session 外层：它自己的记录要用**独立**事务提交，
  不能被业务事务的回滚带走；
- AccessLog 要在最外层附近：被认证/限流拦掉的请求也要出现在访问日志里。

错误处理（7）用异常处理器实现，见 `errors.install_error_handlers`。
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI

from recipe_planner.infra.logging import setup_logging
from recipe_planner.storage import async_adapters as data

from .errors import install_error_handlers
from .middleware import (AccessLogMiddleware, AuthMiddleware, IdempotencyMiddleware,
                         RateLimitMiddleware, RequestIDMiddleware, SessionMiddleware)
from .routes import health, jobs, plan_mutations, plans, profile, recipes

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
    app.add_middleware(IdempotencyMiddleware)    # 5：Idempotency-Key
    app.add_middleware(RateLimitMiddleware)      # 4：令牌桶
    app.add_middleware(AuthMiddleware)           # 3：API Key（AUTH_MODE=off 时放行）
    app.add_middleware(AccessLogMiddleware)      # 2
    app.add_middleware(RequestIDMiddleware)      # 1（最外层）

    app.include_router(health.router)            # /health、/ready（不带前缀）

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(recipes.router)
    api.include_router(plans.router)
    # 写入路由放在只读之后：/plans/current 等固定路径必须先匹配
    api.include_router(plan_mutations.router)
    api.include_router(jobs.router)              # POST /plans、/jobs/{id}
    api.include_router(profile.router)
    app.include_router(api)

    @app.on_event("startup")
    async def _reap_orphan_jobs() -> None:
        # 进程内 worker 无法跨重启续跑；先把上一进程遗留的 queued/running
        # 任务落成可解释的 failed，避免用户看到永久转圈（B-03）。
        await data.job_repo().reap_orphans()

    return app


app = create_app()
