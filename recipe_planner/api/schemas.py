"""接口 DTO（docs/08 §6）。

字段按**前端要什么**定，不按数据库有什么定：例如今晚页要"约 45 分钟 · 预计 ¥62 · 18:30 能吃上"，
就直接给 `minutes / cost / eat_eta`，不让前端自己算（否则同一条规则会在两处实现、两处出错）。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from recipe_planner.models import ALLERGENS


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


# ----------------------------------------------------------------- 任务（P1-5）

class PlanCreateIn(BaseModel):
    """排一周的需求（= `UserConstraints` + 起始周与备注）。"""

    people: int = Field(default=2, ge=1, le=20)
    days: int = Field(default=3, ge=1, le=7)
    dishes_per_day: int = Field(default=2, ge=1, le=4)
    allergens: list[str] = Field(default_factory=list,
                                 description="只能填这几个：" + "、".join(ALLERGENS))
    spice_level: str = "不辣"
    taste_tags: list[str] = Field(default_factory=list)
    goal: str = "随便"
    max_time_min: int = Field(default=45, ge=1, le=180)
    skill: str = "随便"
    cook_start: str = ""
    budget_per_person_day: Optional[float] = Field(default=None, ge=0)
    pantry_items: list[str] = Field(default_factory=list)
    start_date: Optional[str] = Field(default=None, description="这一周的第一天（默认下周一）")
    change_note: str = ""


class JobAcceptedOut(BaseModel):
    """202 的响应：立刻给 job_id，别让客户端干等。"""

    job_id: str
    status: Literal["queued"] = "queued"
    queue_position: int = 1
    message: str = ""
    poll_path: str = ""
    timeout_sec: int = 120
    next_steps: list[NextStep] = []


class JobOut(BaseModel):
    """任务状态（轮询用）。失败时 `message` 是人话，`next_steps` 可直接点。"""

    id: str
    kind: str = "plan_week"
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    stage: str = ""
    progress: float = 0.0
    plan_id: Optional[str] = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    error_code: Optional[str] = None
    message: str = ""
    next_steps: list[NextStep] = []
    request: dict[str, Any] = {}


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


# ----------------------------------------------------------------- 写入

class MutationOut(BaseModel):
    """写入类接口的统一回执（docs/08 §6：`{data, action_log_id, undo_hint}`）。

    - `message` 是人话回执，句式「已<做了什么>，<哪里没动>」（05 §5.3）；
    - `undo_hint` 是**精确的**逆操作（method/path/body），不是"再点一次"；
    - `next_steps` 给"换不动"时的放宽项（05 §5.1：失败必须给可点击的补救项）。
    """

    kind: str
    message: str
    data: dict[str, Any] = {}
    action_log_id: Optional[int] = None
    undo_hint: Optional[dict[str, Any]] = None
    next_steps: list[NextStep] = []


class DayPatchIn(BaseModel):
    """`PATCH /plans/{id}/days/{day}` 的请求体（一次只做一件事）。"""

    op: Literal["skip", "restore", "people", "faster", "swap", "replace_day", "done"]
    people: Optional[int] = None          # op=people：这一天**总共**几人（绝对值）
    recipe_id: Optional[str] = None       # op=swap：要换掉的那道
    recipe_ids: Optional[list[str]] = None  # op=replace_day：这一天最终要有哪些菜（空数组=不做饭）
    done: bool = True                     # op=done


class FeedbackIn(BaseModel):
    op: Literal["like", "dislike", "lock", "unlock"]


class RateIn(BaseModel):
    day: int = Field(ge=1, le=7)
    score: int = Field(ge=0, le=2, description="2=好吃 / 1=一般 / 0=下次不做")


class ChecksIn(BaseModel):
    names: list[str] = Field(default_factory=list, description="已买到的食材名（全量覆盖，幂等）")


class ProfilePatchIn(BaseModel):
    op: Literal["like", "dislike", "remove"]
    names: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- 档案

class ProfileOut(BaseModel):
    liked_dishes: list[str] = []
    disliked_dishes: list[str] = []
    # 形状与 profile.rate() 一致：{菜名: {"score": 0|1|2, "date": "MM/DD"}}
    ratings: dict[str, Any] = {}
    # {name, since, source}（since=什么时候记的，source=在哪记的）
    history: list[dict[str, Any]] = []
    signature: str = ""
