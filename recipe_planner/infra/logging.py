"""结构化 JSON 日志 + `request_id` 贯穿（docs/08 §5 中间件 1/2、§8）。

- 每条日志一行 JSON，`request_id` 由 contextvar 自动带上，不用每个调用点手动传；
- stdout 之外，本地开发同时写 `logs/app.log`（按天轮转，保留 14 天）；
- `setup_logging()` 幂等，测试里可反复调用。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "logs"
RETENTION_DAYS = 14

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_household: ContextVar[str] = ContextVar("household", default="")


def request_id() -> str:
    return _request_id.get()


def set_request_id(value: str):
    """返回 token，交给 reset_request_id() 还原（中间件必须在 finally 里还原）。"""
    return _request_id.set(value or "")


def reset_request_id(token) -> None:
    _request_id.reset(token)


def household() -> str:
    return _household.get()


def set_household(value: str):
    return _household.set(value or "")


def reset_household(token) -> None:
    _household.reset(token)


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象；自定义字段放在 record.fields 里。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        payload.setdefault("request_id", request_id() or "-")
        if _household.get():
            payload.setdefault("household", _household.get())
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """带结构化字段的一条日志（request_id / household 由 formatter 补）。"""
    logger.log(level, message, extra={"fields": fields})


def setup_logging(level: int | str | None = None, to_file: bool = True) -> logging.Logger:
    """配置根日志：stdout + 可选文件轮转。重复调用只生效一次。"""
    root = logging.getLogger()
    lvl = level if isinstance(level, int) else getattr(logging, str(level or "INFO").upper(),
                                                      logging.INFO)
    root.setLevel(lvl)
    formatter = JsonFormatter()

    if not any(getattr(h, "_rp_json", False) for h in root.handlers):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream._rp_json = True            # type: ignore[attr-defined]
        root.addHandler(stream)

    if to_file and not any(getattr(h, "_rp_file", False) for h in root.handlers):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.TimedRotatingFileHandler(
                LOG_DIR / "app.log", when="midnight", backupCount=RETENTION_DAYS,
                encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler._rp_file = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
        except OSError:                   # 只读环境/无权限：日志必须有，文件可以没有
            pass

    # 访问日志只保留 recipe_planner.access 这一条结构化记录。httpx/httpcore
    # 和 uvicorn.access 的 INFO 请求日志会把同一个请求再记一遍，造成重复与噪声。
    for noisy in ("httpx", "httpcore", "watchfiles", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.setLevel(logging.WARNING)
    uvicorn_access.disabled = True
    uvicorn_access.propagate = False
    return root
