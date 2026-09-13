"""中间件 4：`RateLimitMiddleware` —— 令牌桶（docs/08 §5 中间件 4）。

- 普通接口 60/分钟，`POST /api/v1/plans`（要花钱调模型）单独一个桶 6/分钟；
- 超限 → 429 + `Retry-After`（秒），并且照样是 problem+json（人话 + 可点击下一步）；
- `/health`、`/ready` 不限流（探针不能被限流打挂）；
- 桶按"谁"分：带 API Key 就按 Key 分，否则按来源 IP —— 一家人共用一个桶是合理的，但也别互相踩；
- `RATE_LIMIT=off` 整体关闭。

P2 换 Redis 时，只需把这里的 `self._buckets` 换成 Redis 实现（接口一样：take → (是否放行, 等多久)）。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from recipe_planner.infra import settings
from recipe_planner.infra.logging import log_event

from ..errors import send_problem
from .auth import is_exempt

logger = logging.getLogger("recipe_planner.ratelimit")

# 贵的接口单独一个桶（docs/08 §5）：一个路由一个 (method, path) 精确匹配
EXPENSIVE_ROUTES = {("POST", "/api/v1/plans")}


class TokenBucket:
    """令牌桶：按时间匀速回填，最多攒 `capacity` 个（=允许的突发量）。"""

    def __init__(self, capacity: int, per_minute: int,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.capacity = float(capacity)
        self.rate = per_minute / 60.0          # 每秒回填多少
        self._now = now
        self.tokens = float(capacity)
        self.updated = now()

    def take(self, cost: float = 1.0) -> tuple[bool, float]:
        """返回 (是否放行, 还要等多少秒)。"""
        now = self._now()
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        return False, (cost - self.tokens) / self.rate if self.rate else 60.0

    def idle_and_full(self) -> bool:
        """闲置到满桶了（可以安全回收，避免桶字典无限长）。"""
        return self.tokens >= self.capacity


def route_class(method: str, path: str) -> str:
    return "expensive" if (method.upper(), path) in EXPENSIVE_ROUTES else "normal"


def identity_of(scope: Scope) -> str:
    """按 API Key 分桶；没有 Key 就按来源 IP。"""
    for key, value in scope.get("headers") or []:
        if key == b"x-api-key":
            return "key:" + value.decode("latin-1").strip()[:16]
    client = scope.get("client") or ("unknown", 0)
    return "ip:" + str(client[0])


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, normal_per_min: Optional[int] = None,
                 plan_per_min: Optional[int] = None, enabled: Optional[bool] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        self.app = app
        self.enabled = settings.rate_limit_enabled() if enabled is None else enabled
        self.limits = {
            "normal": normal_per_min or settings.rate_limit_per_min(),
            "expensive": plan_per_min or settings.rate_limit_plan_per_min(),
        }
        self._now = now or time.monotonic
        self._buckets: dict[tuple[str, str], TokenBucket] = {}

    def _bucket(self, ident: str, klass: str) -> TokenBucket:
        key = (ident, klass)
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) > 1000:            # 顺手回收闲置的桶
                self._buckets = {k: v for k, v in self._buckets.items()
                                 if not v.idle_and_full()}
            bucket = TokenBucket(self.limits[klass], self.limits[klass], self._now)
            self._buckets[key] = bucket
        return bucket

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if is_exempt(path):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        klass = route_class(method, path)
        ident = identity_of(scope)
        allowed, wait = self._bucket(ident, klass).take()
        if not allowed:
            retry_after = max(1, math.ceil(wait))
            log_event(logger, logging.WARNING, "请求被限流",
                      path=path, method=method, bucket=klass, identity=ident,
                      retry_after_sec=retry_after)
            await send_problem(
                send, "rate_limited",
                f"你点得有点快，缓 {retry_after} 秒再试。"
                + ("（排菜要花钱调模型，所以单独限得紧一些）" if klass == "expensive" else ""),
                429,
                {"bucket": klass, "limit_per_min": self.limits[klass],
                 "retry_after_sec": retry_after,
                 "next_steps": [{"op": "wait", "label": f"等 {retry_after} 秒再试"},
                                {"op": "view_existing", "label": "先看看已有的方案"}]},
                headers=[(b"retry-after", str(retry_after).encode("latin-1"))])
            return

        await self.app(scope, receive, send)
