"""菜谱的做法步骤与参考视频链接（docs/12 §4 阶段三 3.1）

Revision ID: 0004
Revises: 0003

**给已有表加两列，都是"有默认值、可空得起来"的**：
`steps`（JSON 数组，默认 `[]`）、`video_url`（字符串，默认 `''`）——
所以老库升级上来之后，每一道菜只是"还没写做法"，不会出现 NULL 或读不出来的值，
界面上也就自然不显示"怎么做"入口（而不是显示一个空面板）。

**为什么放在 `recipe` 上而不是新开表**：做法是**菜谱的内容**，与 `ingredients` 同级 ——
一道菜的做法不随方案、不随用户变化。放 JSON 列也和 `taste_tags` / `goal_tags` / `allergens`
一致（`orm.py` 开头那条"数组字段统一用 JSON 类型"的约定不用破）。

`tests/test_migrations.py` 会逐表逐列比对"迁移建出来的 schema"与
`Base.metadata.create_all` 建出来的 schema，所以这里与 `storage/orm.py` 必须一字不差。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `server_default` 让**已有的 100 行**在升级时立刻拿到合法值（不是 NULL）：
    # SQLite 的 ADD COLUMN 要求默认值是常量，JSON 列在这里写成 '[]' 文本。
    op.add_column("recipe", sa.Column("steps", sa.JSON(), server_default=sa.text("'[]'"),
                                      nullable=False))
    op.add_column("recipe", sa.Column("video_url", sa.String(length=300),
                                      server_default="", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("recipe") as batch:
        batch.drop_column("video_url")
        batch.drop_column("steps")
