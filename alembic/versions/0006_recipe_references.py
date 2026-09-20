"""菜谱多参考链接（C-02 D5）。"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipe_reference",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  autoincrement=True, nullable=False),
        sa.Column("recipe_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), server_default="search", nullable=False),
        sa.Column("platform", sa.String(32), server_default="", nullable=False),
        sa.Column("title", sa.String(200), server_default="", nullable=False),
        sa.Column("creator", sa.String(120), server_default="", nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipe.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("recipe_reference_idx", "recipe_reference",
                    ["recipe_id", "active", "sort_order"], unique=False)
    # 兼容迁移旧的单一 video_url；空链接不生成伪引用。
    op.execute("""
        INSERT INTO recipe_reference
            (recipe_id, kind, platform, title, creator, url, active, sort_order)
        SELECT id, 'video', '', '旧 video_url', '', video_url, 1, 0
        FROM recipe
        WHERE trim(video_url) <> ''
    """)


def downgrade() -> None:
    op.drop_index("recipe_reference_idx", table_name="recipe_reference")
    op.drop_table("recipe_reference")
