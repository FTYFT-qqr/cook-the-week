"""餐次：`plan_dish.meal` + `plan_day` 的"哪几顿"两列（docs/10 第④步）

Revision ID: 0002
Revises: 0001

**只做 `ADD COLUMN`，不重建表。** 原设计是把 `plan_day` 的主键改成
`(plan_id, day_no, meal)`，但那要重建 `plan_day` 和 `plan_dish` 两张表 ——
对一个已经在用的库来说风险大得多。改成的做法是：餐次这个维度**只加在真正需要它的地方**
（每道菜属于哪一顿），"哪几顿做过了 / 哪几顿不做饭"用 day 行上的两个短字符串记。

- `plan_dish.meal`：server_default `'晚餐'` → 老数据自动当成晚餐，**不需要回填脚本**；
- `plan_day.done_meals` / `skipped_meals`：逗号分隔的餐次名（如 `'早餐,午餐'`），空串 =
  沿用老字段（`done_at` / `skipped` 表示"这一天"）。

`tests/test_migrations.py` 会逐表逐列比对"迁移建出来的 schema"与
`Base.metadata.create_all` 建出来的 schema，所以这里与 `storage/orm.py` 必须一字不差。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

MEAL = "晚餐"


def upgrade() -> None:
    op.add_column('plan_dish', sa.Column('meal', sa.String(length=16),
                                         server_default=MEAL, nullable=False))
    op.add_column('plan_day', sa.Column('skipped_meals', sa.String(length=48),
                                        server_default='', nullable=False))
    op.add_column('plan_day', sa.Column('done_meals', sa.String(length=48),
                                        server_default='', nullable=False))


def downgrade() -> None:
    # SQLite 删列要走 batch（它会重建表）—— 只在下行时才有这个代价
    with op.batch_alter_table('plan_day') as batch:
        batch.drop_column('done_meals')
        batch.drop_column('skipped_meals')
    with op.batch_alter_table('plan_dish') as batch:
        batch.drop_column('meal')
