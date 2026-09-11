"""建库/升级入口：生产路径只走 Alembic（docs/09 P0-3）。

三种起始状态，自动判断，都可重复执行：

| 起始状态 | 动作 | 说明 |
|---|---|---|
| 空库 / 不存在的文件 | `upgrade head` | 正常首次建库 |
| 已有我们的表、但没有 `alembic_version` | `stamp head` | P0-2/P0-5 期间用 `create_all()` 建的库，补记版本号，**不动数据** |
| 已有 `alembic_version` | `upgrade head` | 常规升级（当前已在 head 则空操作） |

`create_all()` 从此只用于测试（tests/test_storage_schema.py），不再出现在运行时代码里。

CLI：python -m recipe_planner.storage.migrate
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from recipe_planner.infra import settings
from recipe_planner.storage.orm import Base

ROOT = Path(__file__).resolve().parent.parent.parent
INI = ROOT / "alembic.ini"
SCRIPT_LOCATION = ROOT / "alembic"
VERSION_TABLE = "alembic_version"


def _sync_url(url: str) -> str:
    """reflection 只用同步驱动。"""
    return (url.replace("sqlite+aiosqlite:///", "sqlite:///", 1)
               .replace("postgresql+asyncpg://", "postgresql://", 1))


def _alembic_config(url: str) -> Config:
    cfg = Config(str(INI))
    cfg.set_main_option("script_location", SCRIPT_LOCATION.as_posix())
    cfg.set_main_option("sqlalchemy.url", "")      # 由 env.py 统一解析
    cfg.attributes["url"] = url                    # 注入本次要操作的库
    return cfg


def _sync_engine(url: str):
    sync = _sync_url(url)
    kwargs: dict = {}
    if sync.startswith("sqlite"):
        # 库文件可能还不存在：SQLite 会在首次连接时创建
        db_path = sync.replace("sqlite:///", "", 1)
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"timeout": 5}
    return create_engine(sync, **kwargs)


def _inspect_state(url: str) -> tuple[bool, bool]:
    """返回 (有版本表, 有业务表)。"""
    engine = _sync_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    business = tables & set(Base.metadata.tables)
    return VERSION_TABLE in tables, bool(business)


def ensure_schema(url: str | None = None, verbose: bool = False) -> str:
    """把目标库升到 head，返回本次动作名（upgrade / stamp / noop）。"""
    target = url or settings.database_url()
    has_version, has_business = _inspect_state(target)
    cfg = _alembic_config(target)
    if has_version:
        # upgrade 到 head；已在 head 时 Alembic 自己就是空操作
        command.upgrade(cfg, "head")
        action = "upgrade"
    elif has_business:
        # create_all 建出来的老库：只补版本号
        command.stamp(cfg, "head")
        action = "stamp"
    else:
        command.upgrade(cfg, "head")
        action = "upgrade"
    if verbose:
        print(f"数据库就绪（{action}）：{target}")
    return action


def current_revision(url: str | None = None) -> str | None:
    """当前库记录的迁移版本（无版本表时返回 None）。"""
    from alembic.runtime.migration import MigrationContext

    engine = _sync_engine(url or settings.database_url())
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


if __name__ == "__main__":
    print(f"当前版本: {current_revision()}")
    print(f"本次动作: {ensure_schema(verbose=True)}")
    print(f"升级后版本: {current_revision()}")
    sys.exit(0)
