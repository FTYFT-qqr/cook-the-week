"""接口 DTO（docs/08 §6）。

字段按**前端要什么**定，不按数据库有什么定：例如今晚页要"约 45 分钟 · 预计 ¥62 · 18:30 能吃上"，
就直接给 `minutes / cost / eat_eta`，不让前端自己算（否则同一条规则会在两处实现、两处出错）。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------- 通用

class NextStep(BaseModel):
    """可点击的下一步（05 §5.1：改不了就要给下一步，而不是一句"失败了"）。

    允许带额外字段（例如打分那步要带 `score`），所以 `extra="allow"`。
    """

    model_config = ConfigDict(extra="allow")

    op: str
    label: str


# ----------------------------------------------------------------- 健康检查

class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    app: str = "recipe-planner"
    version: str = ""


class CheckOut(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class ReadyOut(BaseModel):
    status: Literal["ready", "degraded"]
    checks: list[CheckOut]


# ----------------------------------------------------------------- 菜谱

class IngredientOut(BaseModel):
    name: str
    amount: str = ""
    category: str = "其他"


class RecipeOut(BaseModel):
    id: str
    name: str
    category: str
    description: str = ""
    difficulty: str = "简单"
    time_min: int
    cost_yuan: float
    calories: Optional[int] = None
    protein_g: Optional[float] = None
    spice_level: str = "不辣"
    taste_tags: list[str] = []
    goal_tags: list[str] = []
    allergens: list[str] = []
    ingredients: list[IngredientOut] = []
    liked: bool = False
    disliked: bool = False


class RecipeListOut(BaseModel):
    items: list[RecipeOut]
    total: int
    next_cursor: Optional[int] = None


# ----------------------------------------------------------------- 方案

class DishOut(BaseModel):
    recipe_id: str
    name: str = ""
    category: str = ""
    difficulty: str = ""
    time_min: int = 0
    reason: str = ""
    locked: bool = False


class DayOut(BaseModel):
    day: int
    weekday: str = ""
    date_label: str = ""
    skipped: bool = False
    people: Optional[int] = None
    minutes: int = 0
    cost: float = 0.0
    dishes: list[DishOut] = []
    done: bool = False
    cook_order: list[str] = []
    has_parallel: bool = False


class ShoppingItemOut(BaseModel):
    name: str
    category: str = ""
    amount: str = ""
    needed: bool = True
    checked: bool = False
    batch: Optional[int] = None
    optional: bool = False
    for_recipes: list[str] = []


class IssueOut(BaseModel):
    level: str
    code: str
    message: str
    day: Optional[int] = None
    recipe_id: Optional[str] = None


class SummaryOut(BaseModel):
    days: int
    dishes: int
    total_cost: float
    budget_total: Optional[float] = None
    over_budget: float = 0.0
    liked_hit: int = 0
    goal: str = "随便"
    goal_hit: int = 0
    hardest_day: int = 0
    hardest_minutes: int = 0
    structure: str = ""
    warnings: list[IssueOut] = []


class ConstraintsOut(BaseModel):
    people: int
    days: int
    dishes_per_day: int
    allergens: list[str] = []
    spice_level: str = "不辣"
    taste_tags: list[str] = []
    goal: str = "随便"
    max_time_min: int = 45
    skill: str = "随便"
    cook_start: str = ""
    budget_per_person_day: Optional[float] = None
    pantry_items: list[str] = []


class PlanListItem(BaseModel):
    id: str
    label: str = ""
    created_at: str = ""
    start_date: str = ""
    change_note: str = ""
    days: int = 0
    dishes: int = 0
    total_cost: float = 0.0
    done_days: list[int] = []
    is_current: bool = False


class PlanListOut(BaseModel):
    items: list[PlanListItem]
    total: int
    current_id: Optional[str] = None


class PlanDetailOut(BaseModel):
    id: str
    label: str = ""
    created_at: str = ""
    start_date: str = ""
    change_note: str = ""
    is_current: bool = False
    constraints: ConstraintsOut
    days: list[DayOut]
    shopping: list[ShoppingItemOut]
    summary: SummaryOut
    checked_items: list[str] = []
    done_days: list[int] = []
    issues: list[IssueOut] = []


# ----------------------------------------------------------------- 今晚

class TonightOut(BaseModel):
    """今晚页首页数据（docs/08 §6）：state ∈ no_plan / week_over / skipped / done / planned。"""

    state: Literal["no_plan", "week_over", "skipped", "done", "planned"]
    plan_id: Optional[str] = None
    kicker: str = ""
    headline: str = ""
    meta: str = ""
    reason: str = ""
    day: int = 0
    weekday: str = ""
    date_label: str = ""
    week_label: str = ""
    dishes: list[DishOut] = []
    minutes: int = 0
    cost: float = 0.0
    people: Optional[int] = None
    eat_eta: str = ""
    hint: str = ""
    next_steps: list[NextStep] = []


# ----------------------------------------------------------------- 档案

class ProfileOut(BaseModel):
    liked_dishes: list[str] = []
    disliked_dishes: list[str] = []
    # 形状与 profile.rate() 一致：{菜名: {"score": 0|1|2, "date": "MM/DD"}}
    ratings: dict[str, Any] = {}
    # {name, since, source}（since=什么时候记的，source=在哪记的）
    history: list[dict[str, Any]] = []
    signature: str = ""
