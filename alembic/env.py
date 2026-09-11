"""Alembic 运行环境（async，docs/09 P0-3）。

- 表结构来源：`recipe_planner.storage.orm.Base.metadata`（与运行时同一份定义，不会漂移）；
- 连接串来源：`-x url=...` > 环境变量 `DATABASE_URL` > `settings.database_url()`；
- SQLite 下复用 `attach_sqlite_pragmas()`，保证迁移过程与运行时看到同样的外键/超时行为。
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from recipe_planner.infra import settings
from recipe_planner.storage.engine import attach_sqlite_pragmas
from recipe_planner.storage.orm import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _normalize(url: str) -> str:
    """同步驱动兜底：迁移内部一律走 async 驱动。"""
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _database_url() -> str:
    """优先级：代码注入 config.attributes['url'] > 命令行 -x url=... > settings（DATABASE_URL）"""
    injected = (config.attributes or {}).get("url")
    if injected:
        return _normalize(str(injected))
    try:
        override = context.get_x_argument(as_dictionary=True).get("url")
    except Exception:  # 没传 -x
        override = None
    return _normalize((override or "").strip() or settings.database_url())


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL（`alembic upgrade head --sql`）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.",
                                      poolclass=pool.NullPool)
    if engine.url.get_backend_name() == "sqlite":
        attach_sqlite_pragmas(engine)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_migrations_online())
