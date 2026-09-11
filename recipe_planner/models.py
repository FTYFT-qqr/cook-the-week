"""Recipe-Planner 数据模型（pydantic v2）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------- 常量 ----------

DISPLAY_CATEGORIES = ["蔬菜", "肉蛋", "水产", "豆制品", "菌菇", "主食", "调料", "干货"]

ALLERGENS = ["花生", "海鲜", "蛋", "奶", "坚果", "大豆", "麸质", "芝麻"]
TASTE_TAGS = ["清淡", "咸鲜", "酸甜", "香辣", "微辣", "下饭", "清爽"]
GOALS = ["随便", "减脂", "控糖", "清淡", "高蛋白", "省钱"]
SPICE_LEVELS = ["不辣", "微辣", "辣"]
MEAL = "晚餐"
MEALS_PER_DAY = 1
DISABLED_ALLERGENS = Literal[tuple(ALLERGENS)]  # type: ignore[misc]


# ---------- 数据模型 ----------

class Ingredient(BaseModel):
    name: str
    amount: str = Field(description="数量描述，如 '300克'、'2个'")
    category: str = "其他"  # 上面 DISPLAY_CATEGORIES 之一或其他
    grams: Optional[float] = None  # 可换算的克数，用于预算/扣减估算；None=不可换算


class Recipe(BaseModel):
    id: str
    name: str
    category: str = "热菜"  # 热菜/凉菜/汤/主食
    description: str = ""
    difficulty: str = "简单"  # 简单/中等/较难
    time_min: int = Field(ge=1, le=300)  # 从准备到出锅
    cost_yuan: float = Field(ge=0, description="一份(约2人)估算成本")
    calories: Optional[int] = None  # kcal / 份
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    taste_tags: list[str] = []  # TASTE_TAGS 子集
    spice_level: str = "不辣"  # SPICE_LEVELS
    goal_tags: list[str] = []  # 减脂/控糖/清淡/高蛋白/省钱 子集
    allergens: list[str] = []  # ALLERGENS 子集
    ingredients: list[Ingredient] = []

    @field_validator("ingredients")
    @classmethod
    def _non_empty(cls, v: list[Ingredient]) -> list[Ingredient]:
        return v or []

    def all_allergen_names(self) -> set[str]:
        """过敏原 + 食材名兜底（含关键词匹配）。"""
        s = set(self.allergens)
        for ing in self.ingredients:
            for al in ALLERGENS:
                if al in ing.name:
                    s.add(al)
        return s


class RecipeDB(BaseModel):
    recipes: list[Recipe] = []

    def by_id(self, rid: str) -> Optional[Recipe]:
        for r in self.recipes:
            if r.id == rid:
                return r
        return None


# ---------- 用户约束 ----------

class UserConstraints(BaseModel):
    people: int = Field(default=2, ge=1, le=20)
    days: int = Field(default=3, ge=1, le=7)
    dishes_per_day: int = Field(default=2, ge=1, le=4)  # 每顿几道菜
    allergens: list[str] = []
    spice_level: str = "不辣"  # 期望辣度：不辣/微辣/辣（高于此排除）
    taste_tags: list[str] = []  # 期望口味偏好（尽量满足，软约束）
    goal: str = "随便"  # GOALS
    max_time_min: int = Field(default=45, ge=1, le=180)
    skill: str = "随便"  # 随便/新手/老手（D4：新手排除「较难」的菜）
    cook_start: str = ""  # 我几点开始做饭（"18:30"），用来倒推「几点能吃上」（B2）
    budget_per_person_day: Optional[float] = Field(default=None, ge=0)
    pantry_items: list[str] = []  # 家里已有食材（名称关键词）
    liked_dishes: list[str] = []  # 客户喜欢的菜品 id（软性，尽量安排）
    disliked_dishes: list[str] = []  # 客户不喜欢的菜品 id（硬性，绝不安排）
    customer_name: str = ""  # 客户标识（预留多客户画像）
    must_include_recipes: list[str] = []  # 调试/演示用：必须包含的菜（id）

    @field_validator("taste_tags")
    @classmethod
    def _dedup(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))


# ---------- 中间状态 / 输出 ----------

class ChosenDish(BaseModel):
    recipe_id: str
    reason: str = ""


class DayPlan(BaseModel):
    day: int
    meal: str = MEAL
    dishes: list[ChosenDish] = []
    skipped: bool = False  # 客户说「这天不做饭」（E-03）：不排菜、不计花费、不进清单
    people: Optional[int] = None  # 这一天的临时人数（「来客人了」只改今晚份量，05 M1）

    def recipe_ids(self) -> list[str]:
        return [d.recipe_id for d in self.dishes]

    def recipe_ids(self) -> list[str]:
        return [d.recipe_id for d in self.dishes]


class ValidationIssue(BaseModel):
    level: Literal["error", "warning"]
    code: str  # allergen / duplicate / over_budget / over_time / goal / ...
    message: str
    day: Optional[int] = None
    recipe_id: Optional[str] = None


class ShoppingItem(BaseModel):
    name: str
    category: str
    amount: str
    needed: bool = True  # False = 库存已覆盖
    for_recipes: list[str] = []


class PlanResult(BaseModel):
    constraints: UserConstraints
    candidate_count: int
    days: list[DayPlan]
    issues: list[ValidationIssue] = []
    repairs_used: int = 0
    shopping: list[ShoppingItem] = []
    llm_used: bool = False
    llm_error: Optional[str] = None
    latency_sec: float = 0.0
    estimated_cost_yuan: float = 0.0
    final: bool = True
    trace: list[str] = []

    def to_rich_dict(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json())


class PlanRecord(BaseModel):
    """一份方案存档（内存/JSON/DB 三种实现共用同一个结构）。"""

    id: str
    created_at: str = ""      # "2026-08-09 15:20"
    start_date: str = ""      # ISO 日期，这一周的第一天（周一）
    label: str = ""           # "8/12–8/18"
    change_note: str = ""     # 最近一次改动的说明
    done_days: list[int] = []        # 已经做过饭的日子（M1 状态③）
    checked_items: list[str] = []    # 买菜清单的勾选（M4：关掉浏览器再打开还在）
    result: PlanResult

    def constraints(self) -> UserConstraints:
        return self.result.constraints
