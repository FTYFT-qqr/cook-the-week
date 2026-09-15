"""P0-3 验收：Alembic 迁移能用、能回滚，且与 ORM 定义完全一致。

三件事：
1. `upgrade head` 建出的 schema == `Base.metadata.create_all` 建出的 schema（逐表/逐列/索引/外键）；
2. `downgrade base` 把 17 张表删干净；
3. 基线里的部分唯一索引 `plan_one_active` 在库里真的拦得住第二条 active 方案。

注意：这些测试必须是**同步**函数——`migrate.ensure_schema()` 内部会 `asyncio.run()`，
在 pytest-asyncio（asyncio_mode=auto）包装出来的事件循环里是跑不了的。

运行：python -m pytest tests -q
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from recipe_planner.storage import migrate
from recipe_planner.storage.orm import Base

# 注意：不用 pytest 的 tmp_path（它落在系统 TEMP 上，本项目环境对该目录无写权限）
TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"
EXPECTED_TABLES = set(Base.metadata.tables)


def _db_path(tag: str) -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"mig_{tag}_{uuid4().hex[:8]}.db"
    return path


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _snapshot(path: Path) -> dict:
    """把一个 SQLite 文件的结构拍平成可比较的字典（忽略 alembic_version 表）。"""
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    insp = inspect(engine)
    out: dict = {}
    try:
        for table in sorted(insp.get_table_names()):
            if table == migrate.VERSION_TABLE:
                continue
            cols = {
                c["name"]: (
                    str(c["type"]).upper(),
                    bool(c["nullable"]),
                    (str(c.get("default") or "")).strip(),
                )
                for c in insp.get_columns(table)
            }
            pk = tuple(insp.get_pk_constraint(table).get("constrained_columns") or ())
            idx = sorted(
                (i["name"], tuple(i.get("column_names") or ()), bool(i["unique"]))
                for i in insp.get_indexes(table)
            )
            fks = sorted(
                (tuple(f.get("constrained_columns") or ()),
                 f.get("referred_table"),
                 tuple(f.get("referred_columns") or ()))
                for f in insp.get_foreign_keys(table)
            )
            uniq = sorted(tuple(u.get("column_names") or ())
                          for u in insp.get_unique_constraints(table))
            out[table] = {"cols": cols, "pk": pk, "idx": idx, "fk": fks, "uq": uniq}
    finally:
        engine.dispose()
    return out


def _create_all(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
    finally:
        engine.dispose()


def _tables_of(path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


# ------------------------------------------------------------------ 1. 升级

def test_upgrade_head_creates_all_tables():
    path = _db_path("up")
    assert migrate.current_revision(_url(path)) is None      # 还没有版本表

    action = migrate.ensure_schema(_url(path))

    assert action == "upgrade"
    tables = _tables_of(path)
    assert EXPECTED_TABLES <= tables, f"缺表: {EXPECTED_TABLES - tables}"
    assert migrate.VERSION_TABLE in tables
    assert migrate.current_revision(_url(path)) == "0004"


def test_ensure_schema_is_idempotent():
    path = _db_path("idem")
    migrate.ensure_schema(_url(path))
    # 第二次：已有版本表 → 走 upgrade（已在 head，Alembic 空操作）
    assert migrate.ensure_schema(_url(path)) == "upgrade"
    assert migrate.current_revision(_url(path)) == "0004"
    assert EXPECTED_TABLES <= _tables_of(path)


def test_ensure_schema_stamps_legacy_create_all_db():
    """P0-2/P0-5 期间用 create_all 建的库：补版本号，不重建、不动数据。"""
    path = _db_path("legacy")
    _create_all(path)
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("insert into household (id, name) values ('h1', '我家')"))
    engine.dispose()

    action = migrate.ensure_schema(_url(path))

    assert action == "stamp"
    assert migrate.current_revision(_url(path)) == "0004"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as conn:
        assert conn.execute(text("select name from household where id='h1'")).scalar() == "我家"
    engine.dispose()


# ------------------------------------------------- 2. 与 ORM 定义逐字段一致

def test_migration_matches_orm_metadata():
    via_alembic = _db_path("alembic")
    via_create_all = _db_path("create_all")

    migrate.ensure_schema(_url(via_alembic))
    _create_all(via_create_all)

    got = _snapshot(via_alembic)
    want = _snapshot(via_create_all)

    assert set(got) == set(want) == EXPECTED_TABLES
    for table in sorted(want):
        assert got[table] == want[table], f"表 {table} 结构不一致"


# ------------------------------------------------------------- 3. 回滚

def test_downgrade_base_removes_everything():
    path = _db_path("down")
    migrate.ensure_schema(_url(path))
    assert EXPECTED_TABLES <= _tables_of(path)

    from alembic import command

    command.downgrade(migrate._alembic_config(_url(path)), "base")

    left = _tables_of(path)
    assert not (EXPECTED_TABLES & left), f"回滚后仍残留: {EXPECTED_TABLES & left}"
    assert migrate.current_revision(_url(path)) is None


# --------------------------------------------- 4. 部分唯一索引真的生效

def test_partial_unique_index_blocks_second_active_plan():
    path = _db_path("partial")
    migrate.ensure_schema(_url(path))
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    insert = ("insert into plan (id, household_id, week_start, status, constraints, "
              "profile_sig, change_note) values (:id, 'h1', '2026-09-07', :status, '{}', 'sig', '')")
    try:
        with engine.begin() as conn:
            conn.execute(text("insert into household (id, name) values ('h1', '我家')"))
            conn.execute(text("insert into recipe (id, name, category, description, difficulty, "
                              "time_min, cost_yuan, spice_level, taste_tags, goal_tags, allergens) "
                              "values ('r1', '番茄炒蛋', '热菜', '', '简单', 15, 8, '不辣', "
                              "'[]', '[]', '[]')"))
            conn.execute(text(insert), {"id": "p1", "status": "active"})

            with pytest.raises(IntegrityError):        # 第二份 active：被部分唯一索引拒绝
                conn.execute(text(insert), {"id": "p2", "status": "active"})

        with engine.begin() as conn:                    # 归档后可以再开一份 active
            conn.execute(text("update plan set status='archived' where id='p1'"))
            conn.execute(text(insert), {"id": "p2", "status": "active"})
    finally:
        engine.dispose()
