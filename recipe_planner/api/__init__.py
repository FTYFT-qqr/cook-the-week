"""服务化后端（docs/08-后端架构设计.md 方案 B）。

- `main.create_app()`：应用工厂（中间件 + 路由 + 统一错误）；
- `deps`：依赖注入（一次请求共享菜谱/方案/档案）；
- `schemas`：接口 DTO；
- `errors`：`problem+json` 与可点击的下一步；
- `middleware/`：请求 ID、访问日志、请求级事务（P1-4 再加认证/限流/幂等）。
"""
from .main import VERSION, create_app

__all__ = ["create_app", "VERSION"]
