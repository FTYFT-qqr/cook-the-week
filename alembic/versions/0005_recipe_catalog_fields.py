"""菜谱主源元数据与营养字段（C-02 D1）。

现有菜谱统一回填为已发布的 version=1；后续草稿、审核和发布都由数据库状态控制，
JSON 只作为 seed / snapshot / fixture，不再直接决定运行时候选池。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 不允许对已有表 ADD COLUMN 使用 CURRENT_TIMESTAMP 这类非字面量默认值。
    # created_at 因此先可空加入，再在同一迁移中回填；新 ORM 行由 Python 默认值填写。
    op.add_column("recipe", sa.Column("carbs_g", sa.Numeric(6, 1), nullable=True))
    op.add_column("recipe", sa.Column("fat_g", sa.Numeric(6, 1), nullable=True))
    op.add_column("recipe", sa.Column("status", sa.String(12), server_default="published",
                                      nullable=False))
    op.add_column("recipe", sa.Column("version", sa.Integer(), server_default="1",
                                      nullable=False))
    op.add_column("recipe", sa.Column("source_type", sa.String(24), server_default="family",
                                      nullable=False))
    op.add_column("recipe", sa.Column("source_url", sa.String(500), server_default="",
                                      nullable=False))
    op.add_column("recipe", sa.Column("source_creator", sa.String(120), server_default="",
                                      nullable=False))
    op.add_column("recipe", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("recipe", sa.Column("nutrition_basis", sa.String(20), server_default="unknown",
                                      nullable=False))
    op.add_column("recipe", sa.Column("nutrition_source", sa.String(200), server_default="",
                                      nullable=False))
    op.add_column("recipe", sa.Column("nutrition_estimated", sa.Boolean(),
                                      server_default=sa.text("0"), nullable=False))
    op.add_column("recipe", sa.Column("content_hash", sa.String(64), server_default="",
                                      nullable=False))
    op.add_column("recipe", sa.Column("batch_id", sa.String(64), server_default="",
                                      nullable=False))
    op.add_column("recipe", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE recipe SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("recipe") as batch:
        batch.drop_column("created_at")
        batch.drop_column("batch_id")
        batch.drop_column("content_hash")
        batch.drop_column("nutrition_estimated")
        batch.drop_column("nutrition_source")
        batch.drop_column("nutrition_basis")
        batch.drop_column("reviewed_at")
        batch.drop_column("source_creator")
        batch.drop_column("source_url")
        batch.drop_column("source_type")
        batch.drop_column("version")
        batch.drop_column("status")
        batch.drop_column("fat_g")
        batch.drop_column("carbs_g")
