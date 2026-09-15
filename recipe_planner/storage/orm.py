"""SQLAlchemy 声明式模型（16 张表）。

对应 docs/08-后端架构设计.md §3.2 的 DDL。两处刻意的跨库折中（见 08 §3.4）：
- 数组字段（taste_tags / goal_tags / allergens / used_for）统一用 JSON 类型：Postgres 下等价可用，
  SQLite 下原生支持；数据量小（菜谱 30–150 条），过滤在应用层做，不需要 GIN/数组操作符。
- 主键 id 用 text（可读、便于迁移与对账），自增序号用 BigInteger + Identity。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from recipe_planner.models import MEAL


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[str]:
    return mapped_column(String(64), primary_key=True)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
                         nullable=False)


# ---------------------------------------------------------------- 菜谱知识库

class Recipe(Base):
    __tablename__ = "recipe"

    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(24), default="热菜", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    difficulty: Mapped[str] = mapped_column(String(16), default="简单", nullable=False)
    time_min: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_yuan: Mapped[float] = mapped_column(Numeric(8, 2), default=0, nullable=False)
    calories: Mapped[int | None] = mapped_column(Integer)
    protein_g: Mapped[float | None] = mapped_column(Numeric(6, 1))
    spice_level: Mapped[str] = mapped_column(String(8), default="不辣", nullable=False)
    taste_tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    goal_tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    allergens: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 家常做法 + 参考视频链接（docs/12 阶段三，迁移 0004）：与 taste_tags 同样是 JSON 列，
    # 所以"数组字段统一用 JSON"这条约定不用破。
    # **必须带 `server_default`**（不只是 Python 侧的 `default`）：迁移是给**已有 100 行**的
    # 表加两列 NOT NULL，没有库级默认值 SQLite 直接拒绝（"Cannot add a NOT NULL column with
    # default value NULL"），而且 `tests/test_migrations.py` 会逐列比对"迁移建的库"与
    # `create_all` 建的库 —— 两边写一样才是真的等价（踩坑 #46）。
    steps: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"),
                                        nullable=False)
    video_url: Mapped[str] = mapped_column(String(300), default="", server_default="",
                                           nullable=False)
    updated_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint("time_min between 1 and 300", name="recipe_time_ck"),
    )

    # 关系（让 SQLAlchemy 知道插入/删除顺序；DB 层另有 ON DELETE CASCADE 兜底）
    ingredients: Mapped[list["Ingredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan",
        order_by="Ingredient.seq", lazy="selectin")


class Ingredient(Base):
    __tablename__ = "ingredient"

    recipe_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("recipe.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(24), default="其他", nullable=False)
    grams: Mapped[float | None] = mapped_column(Numeric(8, 1))

    recipe: Mapped["Recipe"] = relationship(back_populates="ingredients")


# ---------------------------------------------------------------- 家庭与偏好

class Household(Base):
    __tablename__ = "household"

    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(64), default="我的家", nullable=False)
    created_at: Mapped[datetime] = _created()


class AppUser(Base):
    """家庭成员（P3 用；P0–P2 只有一条默认 household，不建 user）。"""

    __tablename__ = "app_user"

    id: Mapped[str] = _id()
    household_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("household.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    created_at: Mapped[datetime] = _created()


class Preference(Base):
    """喜欢 / 不喜欢（一道菜只能在一个列表里 → 主键即互斥约束）。"""

    __tablename__ = "preference"

    household_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("household.id", ondelete="CASCADE"), primary_key=True)
    recipe_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("recipe.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="口味档案", nullable=False)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint("kind in ('like','dislike')", name="preference_kind_ck"),
    )


class Rating(Base):
    """做完打分：2=好吃 / 1=一般 / 0=下次不做。"""

    __tablename__ = "rating"

    household_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("household.id", ondelete="CASCADE"), primary_key=True)
    recipe_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("recipe.id", ondelete="CASCADE"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint("score in (0,1,2)", name="rating_score_ck"),
    )


# ---------------------------------------------------------------- 方案

class Plan(Base):
    __tablename__ = "plan"

    id: Mapped[str] = _id()
    household_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("household.id", ondelete="CASCADE"), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="active", nullable=False)
    constraints: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    profile_sig: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    change_note: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint("status in ('active','archived')", name="plan_status_ck"),
        Index("plan_current_idx", "household_id", "status", "week_start"),
        # 决策 4：同一个家庭同时只允许 1 份 active（SQLite 与 Postgres 都支持部分唯一索引）
        Index("plan_one_active", "household_id", unique=True,
              sqlite_where=text("status = 'active'"),
              postgresql_where=text("status = 'active'")),
    )

    days: Mapped[list["PlanDay"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan",
        order_by="PlanDay.day_no", lazy="selectin")
    shopping: Mapped[list["ShoppingItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", lazy="selectin")
    checks: Mapped[list["ShoppingCheck"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", lazy="selectin")
    logs: Mapped[list["ActionLog"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", lazy="selectin")


class PlanDay(Base):
    __tablename__ = "plan_day"

    plan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plan.id", ondelete="CASCADE"), primary_key=True)
    day_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    day_date: Mapped[date] = mapped_column(Date, nullable=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    people_override: Mapped[int | None] = mapped_column(Integer)     # 「来客人了」
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # —— 餐次（docs/10）——
    # 一天多顿时，"不做饭/做过了"要能按顿说：存成逗号分隔的餐次名（如 "早餐,午餐"）。
    # 这样**不需要改主键**（`ALTER TABLE ADD COLUMN` 就够，不重建表 —— 对已有库最安全）。
    # 空串 = 沿用老字段（`skipped` / `done_at` 表示"这一天"）。
    skipped_meals: Mapped[str] = mapped_column(String(48), default="", server_default="",
                                              nullable=False)
    done_meals: Mapped[str] = mapped_column(String(48), default="", server_default="",
                                           nullable=False)

    __table_args__ = (
        CheckConstraint("day_no between 1 and 7", name="plan_day_no_ck"),
    )

    plan: Mapped["Plan"] = relationship(back_populates="days")
    dishes: Mapped[list["PlanDish"]] = relationship(
        back_populates="day", cascade="all, delete-orphan",
        order_by="PlanDish.seq", lazy="selectin")


class PlanDish(Base):
    __tablename__ = "plan_dish"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("recipe.id", ondelete="RESTRICT"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # 「定住」
    # 这一道属于哪一顿（docs/10）。老数据由 server_default 补成晚餐 —— 不必回填。
    meal: Mapped[str] = mapped_column(String(16), default=MEAL, server_default=MEAL,
                                      nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["plan_id", "day_no"],
                             ["plan_day.plan_id", "plan_day.day_no"],
                             ondelete="CASCADE"),
    )

    day: Mapped["PlanDay"] = relationship(back_populates="dishes")
    recipe: Mapped["Recipe"] = relationship(lazy="joined")


class ShoppingItem(Base):
    __tablename__ = "shopping_item"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plan.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_text: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    grams: Mapped[float | None] = mapped_column(Numeric(8, 1))
    needed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    batch: Mapped[int | None] = mapped_column(Integer)               # 1=周初买 2=周中买
    optional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_for: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (
        CheckConstraint("batch is null or batch in (1,2)", name="shopping_batch_ck"),
        Index("shopping_plan_idx", "plan_id", "category"),
    )

    plan: Mapped["Plan"] = relationship(back_populates="shopping")


class ShoppingCheck(Base):
    """买菜勾选（跨会话保留）。"""

    __tablename__ = "shopping_check"

    plan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plan.id", ondelete="CASCADE"), primary_key=True)
    item_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    checked_at: Mapped[datetime] = _created()

    plan: Mapped["Plan"] = relationship(back_populates="checks")


# ---------------------------------------------------------------- 任务与流水

class Job(Base):
    __tablename__ = "job"

    id: Mapped[str] = _id()
    plan_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("plan.id", ondelete="SET NULL"))
    household_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="plan_week", nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="queued", nullable=False)
    stage: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    progress: Mapped[float] = mapped_column(Numeric(3, 2), default=0, nullable=False)
    request: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelled')", name="job_status_ck"),
        Index("job_active_idx", "household_id", "status", "created_at"),
    )

    plan: Mapped["Plan"] = relationship(lazy="joined")


class ActionLog(Base):
    """改动流水：回执文案 + 撤销快照（把内存撤销栈持久化）。"""

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    plan_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("plan.id", ondelete="CASCADE"))
    household_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (Index("action_plan_idx", "plan_id", "created_at"),)

    plan: Mapped["Plan"] = relationship(back_populates="logs")


class DishEvent(Base):
    """**一道菜上发生的一次偏好事件**（docs/12 §4 阶段二 2.1）。

    ## 为什么不给 `action_log` 加列，而是新开一张表

    `action_log` 是"**一次动作一行**"（回执文案 + 撤销快照），而**一次动作可以涉及多道菜**：
    整周重排 = 21 顿、做完打分 = 一顿的 3 道、换一道 = 1 道。
    逐菜的时间衰减需要的是"**每道菜一行**"，塞进 `action_log` 就得在一条 JSON 里放数组，
    查询与统计反而更麻烦。所以：`action_log` 继续管"这次动作说了什么"，本表管"哪道菜上发生了什么"。

    ## 为什么存 `recipe_id` 而不是菜名

    档案（`preference` / `profile.py`）按**菜名**存，是因为它面向用户展示；
    而排序比的是 `r.id`。事件是给排序与统计用的，所以存 id —— 菜名改了不会让历史断掉。

    ## 与方案的关系

    `plan_id` 可空（有的反馈不带方案，例如在档案页直接标"喜欢"）；
    非空时 `ON DELETE CASCADE`（方案删了，它的事件也没意义了）。
    """
    __tablename__ = "dish_event"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    plan_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("plan.id", ondelete="CASCADE"))
    household_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipe_id: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    meal: Mapped[str] = mapped_column(String(16), default="", server_default="", nullable=False)
    day_no: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(24), default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        Index("dish_event_recipe_idx", "recipe_id", "created_at"),
        Index("dish_event_plan_idx", "plan_id", "created_at"),
        # 动作词表：**故意不做 CheckConstraint**（不像 preference.kind）——
        # 事件是新东西，以后加一种动作（例如"收藏视频"）不该需要一次迁移。
        # 合法性由 `recipe_planner/events.py` 的 ACTIONS 常量与测试守。
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_key"

    key: Mapped[str] = _id()
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()


class LlmCallLog(Base):
    __tablename__ = "llm_call_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), default="plan", nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = _id()
    value: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = _created()


# 供测试与 Alembic 使用的元数据
__all__ = [
    "Base", "Recipe", "Ingredient", "Household", "AppUser", "Preference", "Rating",
    "Plan", "PlanDay", "PlanDish", "ShoppingItem", "ShoppingCheck",
    "Job", "ActionLog", "IdempotencyKey", "LlmCallLog", "AppSetting",
]
