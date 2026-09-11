"""中间件集合（docs/08 §5）。

执行顺序（外 → 内）：`RequestID → AccessLog → Auth → RateLimit → Idempotency → Session → Error`。
Starlette 的 `add_middleware` 是**后加的在最外层**，挂载顺序见 `api/main.py`。

| # | 中间件 | 文件 | 状态 |
|---|---|---|---|
| 1 | RequestID | `request_id.py` | ✅ P1-1 |
| 2 | AccessLog | `access_log.py` | ✅ P1-1 |
| 3 | Auth | `auth.py` | P1-4 |
| 4 | RateLimit | `ratelimit.py` | P1-4 |
| 5 | Idempotency | `idempotency.py` | P1-4 |
| 6 | Session | `session.py` | ✅ P1-1 |
| 7 | Error | `../errors.py`（异常处理器形式） | ✅ P1-1 |
"""
from .access_log import AccessLogMiddleware
from .request_id import RequestIDMiddleware
from .session import SessionMiddleware

__all__ = ["AccessLogMiddleware", "RequestIDMiddleware", "SessionMiddleware"]
