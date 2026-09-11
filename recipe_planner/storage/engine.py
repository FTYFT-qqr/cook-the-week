"""async engine / session 工厂。

- SQLite：打开 WAL + busy_timeout + 外键约束（家庭场景写并发≈1，这样足够）；
- Postgres：只需改 `DATABASE_URL`，代码与表结构不动（docs/08 §3.4）；
- 生产建表走 Alembic；`create_all()` 只给测试与首次本地初始化用。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from recipe_planner.infra.settings import database_url

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def attach_sqlite_pragmas(engine: AsyncEngine) -> None:
    """SQLite 必须显式打开外键（默认关闭 → ON DELETE CASCADE 不生效）+ WAL + busy_timeout。"""
    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - 由 SQLite 驱动触发
        cur = dbapi_conn.cursor()
        cur.execute("pragma journal_mode=WAL")
        cur.execute("pragma busy_timeout=5000")
        cur.execute("pragma foreign_keys=ON")
        cur.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = database_url()
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 5}
        _engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            attach_sqlite_pragmas(_engine)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), class_=AsyncSession,
                                           expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope():
    """一个请求/一个用例一个事务：正常提交，异常回滚。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(engine: AsyncEngine | None = None) -> None:
    """仅测试/本地首次初始化使用；生产用 `alembic upgrade head`。"""
    from recipe_planner.storage.orm import Base

    target = engine or get_engine()
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine | None = None) -> None:
    from recipe_planner.storage.orm import Base

    target = engine or get_engine()
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def reset_engine() -> None:
    """测试里切换 DATABASE_URL 后调用，丢弃缓存的 engine。"""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None
