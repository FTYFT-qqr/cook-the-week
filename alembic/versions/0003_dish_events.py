"""偏好事件表（docs/12 §4 阶段二 2.1）

Revision ID: 0003
Revises: 0002

**纯新增一张表，不动任何已有表**：这是能做到"随时可撤"的前提
（`downgrade()` 直接 drop 掉它，老库的数据一行都不受影响）。

为什么需要它：`action_log` 是"一次动作一行"，而一次动作可以涉及多道菜
（整周重排 21 顿、做完打分一顿 3 道）—— 逐菜的时间衰减要的是"每道菜一行"。

`tests/test_migrations.py` 会逐表逐列比对"迁移建出来的 schema"与
`Base.metadata.create_all` 建出来的 schema，所以这里与 `storage/orm.py` 必须一字不差。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dish_event",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("household_id", sa.String(length=64), nullable=False),
        sa.Column("recipe_id", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("meal", sa.String(length=16), server_default="", nullable=False),
        sa.Column("day_no", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=24), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plan.id"], ondelete="CASCADE"),
    )
    op.create_index("dish_event_recipe_idx", "dish_event", ["recipe_id", "created_at"])
    op.create_index("dish_event_plan_idx", "dish_event", ["plan_id", "created_at"])


def downgrade() -> None:
    op.drop_index("dish_event_plan_idx", table_name="dish_event")
    op.drop_index("dish_event_recipe_idx", table_name="dish_event")
    op.drop_table("dish_event")
