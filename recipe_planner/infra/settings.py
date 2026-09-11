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
