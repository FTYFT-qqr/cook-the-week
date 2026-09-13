"""运行时设置：环境变量集中一处，便于测试覆盖与回退开关。

开关（docs/08-后端架构设计.md、docs/09-后端实现计划.md）：
- STORAGE=db|json   数据从数据库读还是从 JSON 文件读（P0 的安全阀）
- USE_API=0|1       界面直连领域层还是走 HTTP API（P1 的安全阀）
- AUTH_MODE=off|apikey   本机关闭认证；一旦局域网/公网可访问必须开
- DATABASE_URL      默认 SQLite：data/app.db（P2 换 Postgres 只改这一行）
- REDIS_URL         不配置则自动用内存实现（缓存/队列降级）
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def storage_kind() -> str:
    kind = _env("STORAGE", "db").lower()
    return kind if kind in {"json", "db"} else "db"


def use_api() -> bool:
    return _env("USE_API", "0") == "1"


def auth_mode() -> str:
    mode = _env("AUTH_MODE", "off").lower()
    return mode if mode in {"off", "apikey"} else "off"


def api_key() -> str:
    return _env("API_KEY")


def database_url() -> str:
    url = _env("DATABASE_URL")
    if url:
        return url
    return f"sqlite+aiosqlite:///{(DATA_DIR / 'app.db').as_posix()}"


def redis_url() -> str:
    return _env("REDIS_URL")


def plan_retention_weeks() -> int:
    """方案保留周数（08 §13 决策 3：默认 12 周，更旧的归档不删除）。"""
    try:
        return max(1, int(_env("PLAN_RETENTION_WEEKS", "12")))
    except ValueError:
        return 12


def llm_model() -> str:
    return _env("LLM_MODEL", "deepseek-chat")


def llm_api_key() -> str:
    """DeepSeek 密钥是否配置（没配就会走确定性兜底，功能不受影响）。"""
    return _env("DEEPSEEK_API_KEY")


def llm_base_url() -> str:
    return _env("OPENAI_BASE_URL", "https://api.deepseek.com")


# ---------------------------------------------------------------- 限流与幂等（P1-4）


def _int_env(name: str, default: int, low: int = 1) -> int:
    try:
        return max(low, int(_env(name, str(default))))
    except ValueError:
        return default


def rate_limit_enabled() -> bool:
    """`RATE_LIMIT=off` 可整体关掉（压测/演示用）。"""
    return _env("RATE_LIMIT", "on").lower() not in {"off", "0", "false", "no"}


def rate_limit_per_min() -> int:
    """普通接口：每分钟多少次。"""
    return _int_env("RATE_LIMIT_PER_MIN", 60)


def rate_limit_plan_per_min() -> int:
    """排菜（要花钱调模型）单独一个桶，默认 6/分钟（docs/08 §5 中间件 4）。"""
    return _int_env("RATE_LIMIT_PLAN_PER_MIN", 6)


def idempotency_ttl_hours() -> int:
    """同一个 `Idempotency-Key` 的响应保留多久（默认 24h，docs/08 §5 中间件 5）。"""
    return _int_env("IDEMPOTENCY_TTL_HOURS", 24)


def job_timeout_sec() -> int:
    """一次排菜任务最多跑多久（docs/08 §6：默认 120s，超时记为 failed(timeout)）。"""
    return _int_env("JOB_TIMEOUT_SEC", 120, low=5)
