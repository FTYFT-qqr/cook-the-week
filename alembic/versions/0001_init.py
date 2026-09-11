"""基线：16 张表（= docs/08 §3.2 的 DDL，与 storage/orm.py 一一对应）

Revision ID: 0001
Revises:
Create Date: 2026-09-11 19:32:14

本文件由 `alembic revision --autogenerate` 生成后人工核对，未做结构改动。
校验方式（tests/test_migrations.py）：
1. `upgrade head` 建出的 schema 与 `Base.metadata.create_all` 建出的 schema 逐表逐列相等；
2. `downgrade base` 能把 16 张表全部删干净；
3. 基线里的部分唯一索引 `plan_one_active` 在库里真的拦得住第二条 active 方案。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('app_setting',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('household',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('idempotency_key',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('endpoint', sa.String(length=120), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('response', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('llm_call_log',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('purpose', sa.String(length=24), nullable=False),
    sa.Column('prompt_hash', sa.String(length=64), nullable=False),
    sa.Column('tokens_in', sa.Integer(), nullable=True),
    sa.Column('tokens_out', sa.Integer(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('recipe',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('category', sa.String(length=24), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('difficulty', sa.String(length=16), nullable=False),
    sa.Column('time_min', sa.Integer(), nullable=False),
    sa.Column('cost_yuan', sa.Numeric(precision=8, scale=2), nullable=False),
    sa.Column('calories', sa.Integer(), nullable=True),
    sa.Column('protein_g', sa.Numeric(precision=6, scale=1), nullable=True),
    sa.Column('spice_level', sa.String(length=8), nullable=False),
    sa.Column('taste_tags', sa.JSON(), nullable=False),
    sa.Column('goal_tags', sa.JSON(), nullable=False),
    sa.Column('allergens', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('time_min between 1 and 300', name='recipe_time_ck'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('app_user',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('household_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ingredient',
    sa.Column('recipe_id', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('amount', sa.String(length=40), nullable=False),
    sa.Column('category', sa.String(length=24), nullable=False),
    sa.Column('grams', sa.Numeric(precision=8, scale=1), nullable=True),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipe.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('recipe_id', 'seq')
    )
    op.create_table('plan',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('household_id', sa.String(length=64), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('constraints', sa.JSON(), nullable=False),
    sa.Column('profile_sig', sa.String(length=128), nullable=False),
    sa.Column('change_note', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status in ('active','archived')", name='plan_status_ck'),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('plan_current_idx', 'plan', ['household_id', 'status', 'week_start'], unique=False)
    op.create_index('plan_one_active', 'plan', ['household_id'], unique=True, sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))
    op.create_table('preference',
    sa.Column('household_id', sa.String(length=64), nullable=False),
    sa.Column('recipe_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("kind in ('like','dislike')", name='preference_kind_ck'),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipe.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('household_id', 'recipe_id')
    )
    op.create_table('rating',
    sa.Column('household_id', sa.String(length=64), nullable=False),
    sa.Column('recipe_id', sa.String(length=64), nullable=False),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('score in (0,1,2)', name='rating_score_ck'),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipe.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('household_id', 'recipe_id')
    )
    op.create_table('action_log',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('plan_id', sa.String(length=64), nullable=True),
    sa.Column('household_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('action_plan_idx', 'action_log', ['plan_id', 'created_at'], unique=False)
    op.create_table('job',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('plan_id', sa.String(length=64), nullable=True),
    sa.Column('household_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('stage', sa.String(length=64), nullable=False),
    sa.Column('progress', sa.Numeric(precision=3, scale=2), nullable=False),
    sa.Column('request', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status in ('queued','running','succeeded','failed','cancelled')", name='job_status_ck'),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('job_active_idx', 'job', ['household_id', 'status', 'created_at'], unique=False)
    op.create_table('plan_day',
    sa.Column('plan_id', sa.String(length=64), nullable=False),
    sa.Column('day_no', sa.Integer(), nullable=False),
    sa.Column('day_date', sa.Date(), nullable=False),
    sa.Column('skipped', sa.Boolean(), nullable=False),
    sa.Column('people_override', sa.Integer(), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('day_no between 1 and 7', name='plan_day_no_ck'),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('plan_id', 'day_no')
    )
    op.create_table('shopping_check',
    sa.Column('plan_id', sa.String(length=64), nullable=False),
    sa.Column('item_name', sa.String(length=80), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('plan_id', 'item_name')
    )
    op.create_table('shopping_item',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('plan_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('category', sa.String(length=24), nullable=False),
    sa.Column('amount_text', sa.String(length=60), nullable=False),
    sa.Column('grams', sa.Numeric(precision=8, scale=1), nullable=True),
    sa.Column('needed', sa.Boolean(), nullable=False),
    sa.Column('batch', sa.Integer(), nullable=True),
    sa.Column('optional', sa.Boolean(), nullable=False),
    sa.Column('used_for', sa.JSON(), nullable=False),
    sa.CheckConstraint('batch is null or batch in (1,2)', name='shopping_batch_ck'),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('shopping_plan_idx', 'shopping_item', ['plan_id', 'category'], unique=False)
    op.create_table('plan_dish',
    sa.Column('plan_id', sa.String(length=64), nullable=False),
    sa.Column('day_no', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('recipe_id', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('locked', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['plan_id', 'day_no'], ['plan_day.plan_id', 'plan_day.day_no'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipe.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('plan_id', 'day_no', 'seq')
    )


def downgrade() -> None:
    # 逆序删除：先删子表，再删被引用的父表
    op.drop_table('plan_dish')
    op.drop_index('shopping_plan_idx', table_name='shopping_item')
    op.drop_table('shopping_item')
    op.drop_table('shopping_check')
    op.drop_table('plan_day')
    op.drop_index('job_active_idx', table_name='job')
    op.drop_table('job')
    op.drop_index('action_plan_idx', table_name='action_log')
    op.drop_table('action_log')
    op.drop_table('rating')
    op.drop_table('preference')
    op.drop_index('plan_one_active', table_name='plan', sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))
    op.drop_index('plan_current_idx', table_name='plan')
    op.drop_table('plan')
    op.drop_table('ingredient')
    op.drop_table('app_user')
    op.drop_table('recipe')
    op.drop_table('llm_call_log')
    op.drop_table('idempotency_key')
    op.drop_table('household')
    op.drop_table('app_setting')
    # ### end Alembic commands ###
