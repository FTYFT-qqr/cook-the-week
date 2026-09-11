"""存活与就绪（docs/08 §6）。

- `/health`：进程活着就 200，不碰任何外部依赖（容器探针/负载均衡用）；
- `/ready`：能不能干活——DB 连得上才算就绪；Redis/LLM 没配置**不算**失败（会自动降级到
  内存实现与确定性排菜），但会在 `checks` 里如实说出来，方便排障。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from recipe_planner.infra import settings

from ..schemas import CheckOut, HealthOut, ReadyOut

router = APIRouter()

VERSION = "0.2.0"


@router.get("/health", response_model=HealthOut, tags=["infra"])
async def health() -> HealthOut:
    return HealthOut(status="ok", version=VERSION)


async def _check_db() -> CheckOut:
    if settings.storage_kind() != "db":
        return CheckOut(name="db", ok=True, detail="未使用数据库（STORAGE=json）")
    from recipe_planner.storage.engine import session_scope

    try:
        async with session_scope() as session:
            await session.execute(text("select 1"))
        return CheckOut(name="db", ok=True, detail="连接正常")
    except Exception as exc:                     # 连不上就不算就绪
        return CheckOut(name="db", ok=False, detail=f"连不上数据库：{type(exc).__name__}")


def _check_redis() -> CheckOut:
    if not settings.redis_url():
        return CheckOut(name="redis", ok=True, detail="没配置 REDIS_URL，用内存实现（P2 才需要）")
    return CheckOut(name="redis", ok=True, detail="已配置（P2 接入）")


def _check_llm() -> CheckOut:
    configured = bool(settings.llm_api_key())
    return CheckOut(
        name="llm", ok=True,
        detail=(f"已配置 {settings.llm_model()}" if configured
                else "没配密钥，排菜会走确定性兜底（功能不受影响）"))


@router.get("/ready", response_model=ReadyOut, tags=["infra"],
            responses={503: {"description": "有依赖不可用"}})
async def ready():
    checks = [await _check_db(), _check_redis(), _check_llm()]
    payload = ReadyOut(status="ready" if all(c.ok for c in checks) else "degraded", checks=checks)
    if payload.status != "ready":
        return JSONResponse(status_code=503, content=payload.model_dump())
    return payload
