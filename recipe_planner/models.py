"""Recipe-Planner 数据模型（pydantic v2）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from recipe_planner.allergens import hidden_allergens

# ---------- 常量 ----------

DISPLAY_CATEGORIES = ["蔬菜", "肉蛋", "水产", "豆制品", "菌菇", "主食", "调料", "干货"]

ALLERGENS = ["花生", "海鲜", "蛋", "奶", "坚果", "大豆", "麸质", "芝麻"]
TASTE_TAGS = ["清淡", "咸鲜", "酸甜", "香辣", "微辣", "下饭", "清爽"]
GOALS = ["随便", "减脂", "控糖", "清淡", "高蛋白", "省钱"]
SPICE_LEVELS = ["不辣", "微辣", "辣"]
MEAL = "晚餐"
STRATEGY_DAILY_BALANCE = "daily_balance"
# ---------- 餐次（docs/10）----------
# 用户可以选只做早餐、只做晚餐，或任意组合；**默认只勾晚餐**，所以"不关心早餐"的用户
# 与 P1 的行为完全一致（这是回归面为零的原因，不是顺带的好处）。
MEALS = ("早餐", "午餐", "晚餐")
MEAL_DISH_DEFAULTS = {"早餐": 1, "午餐": 2, "晚餐": 3}   # 每餐默认几道菜
BREAKFAST_MAX_TIME_DEFAULT = 15      # 早餐单菜时长上限的默认值（表单可调 5–30）
DISH_MIN, DISH_MAX = 1, 5            # 每餐道数范围（上限从 4 提到 5）
DISABLED_ALLERGENS = Literal[tuple(ALLERGENS)]  # type: ignore[misc]


# ---------- 数据模型 ----------

class Ingredient(BaseModel):
    name: str
    amount: str = Field(description="数量描述，如 '300克'、'2个'")
    category: str = "其他"  # 上面 DISPLAY_CATEGORIES 之一或其他
    grams: Optional[float] = None  # 可换算的克数，用于预算/扣减估算；None=不可换算


class RecipeReference(BaseModel):
    """菜谱外部参考链接（C-02：链接是内容元数据，不参与推荐规则）。"""

    id: Optional[int] = None
    kind: Literal["video", "article", "search"] = "search"
    platform: str = ""
    title: str = ""
    creator: str = ""
    url: str
    checked_at: Optional[datetime] = None
    active: bool = True
    sort_order: int = 0


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
    # 家常做法（docs/12 阶段三）：3–5 步，每步一句动作。**不是算法生成的**，
    # 是随菜谱一起维护的内容（与 `ingredients` 同级）；空列表 = 还没写，界面就不给入口。
    steps: list[str] = []
    # 参考视频链接：**只存链接、不抓取内容**（阶段三 3.1）。
    # 空字符串 = 还没有策展链接 → 界面给一个"去搜做法视频"的搜索入口，
    # 而不是编一个可能 404 的地址（详见 docs/12 v7）。
    video_url: str = ""
    ingredients: list[Ingredient] = []
    # C-02 菜谱内容生命周期元数据。默认值兼容现有 JSON seed；运行时真正
    # 决定推荐候选的是数据库中的 status。
    status: Literal["draft", "review", "published", "archived"] = "published"
    version: int = Field(default=1, ge=1)
    source_type: str = "family"
    source_url: str = ""
    source_creator: str = ""
    reviewed_at: Optional[datetime] = None
    nutrition_basis: str = "unknown"
    nutrition_source: str = ""
    nutrition_estimated: bool = False
    content_hash: str = ""
    created_at: Optional[datetime] = None
    batch_id: str = ""
    references: list[RecipeReference] = []

    @field_validator("ingredients")
    @classmethod
    def _non_empty(cls, v: list[Ingredient]) -> list[Ingredient]:
        return v or []

    def all_allergen_names(self) -> set[str]:
        """这道菜**实际**含哪些过敏原：显式标注 ∪ 食材名兜底 ∪ 隐性来源映射。

        docs/12 §3.5（阶段零）：原来只有前两项，而实测「海鲜/坚果/大豆/麸质/芝麻」
        这 5 个标签在 171 个食材名里出现 **0 次**（食材写的是"蚝油""生抽"，不会出现"海鲜"），
        于是它们完全依赖 `allergens` 字段被人工填对，而 19/38 道菜该字段为空 ——
        实测「蚝油生菜」对海鲜过敏用户不会被排除。第三项就是补这个洞（`allergens.py`）。

        全项目只有两个调用点：`core.recipe_conflicts`（检索过滤）与 `core.validate_plan`
        （硬校验），所以这一处修好，检索与校验两条路径同时生效。
        """
        s = set(self.allergens)
        for ing in self.ingredients:
            for al in ALLERGENS:
                if al in ing.name:
                    s.add(al)
        return s | hidden_allergens(self.name, [i.name for i in self.ingredients])


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
    dishes_per_day: int = Field(default=2, ge=1, le=5)  # 老字段：没指定每餐道数时用它
    allergens: list[str] = []
    spice_level: str = "不辣"  # 期望辣度：不辣/微辣/辣（高于此排除）
    taste_tags: list[str] = []  # 期望口味偏好（尽量满足，软约束）
    goal: str = "随便"  # GOALS
    # 组合策略：第一版只开放日常搭配，老方案缺字段时自然回落到该默认值。
    strategy: str = STRATEGY_DAILY_BALANCE
    max_time_min: int = Field(default=45, ge=1, le=180)
    skill: str = "随便"  # 随便/新手/老手（D4：新手排除「较难」的菜）
    cook_start: str = ""  # 我几点开始做饭（"18:30"），用来倒推「几点能吃上」（B2）
    budget_per_person_day: Optional[float] = Field(default=None, ge=0)
    pantry_items: list[str] = []  # 家里已有食材（名称关键词）
    liked_dishes: list[str] = []  # 客户喜欢的菜品 id（软性，尽量安排）
    disliked_dishes: list[str] = []  # 客户不喜欢的菜品 id（硬性，绝不安排）
    # 逐菜权重（docs/12 阶段二）：`{菜谱 id: 加分}`，由 `preference.dish_weights()` 算出来。
    # **空字典 = 还没有偏好信号** → `recipe_score` 回退到老行为（"喜欢就 +8"），所以
    # 老数据/老测试一行都不用改；有信号时才换成"会随时间衰减"的权重。
    dish_weights: dict[str, float] = {}
    # 上次吃是多少天前 `{菜谱 id: 天数}`（docs/12 阶段二 2.6）：只收"确实吃过"的菜
    # （点过「做完了」或打过分），用来"换着吃"—— **空字典 = 没有吃过记录 = 老行为**，
    # 所以它和 `dish_weights` 一样是零回归的换挡开关。
    dish_last_seen: dict[str, int] = {}
    # 临时避开由事件计算，默认 7 天后恢复；不写入永久喜欢/不喜欢档案。
    snoozed_dishes: list[str] = []
    customer_name: str = ""  # 客户标识（预留多客户画像）
    must_include_recipes: list[str] = []  # 调试/演示用：必须包含的菜（id）

    # —— 餐次（docs/10）。全部带默认值 → **老方案/老接口不需要任何迁移** ——
    meals: list[str] = [MEAL]                      # 这一周排哪几顿
    dishes_per_meal: dict[str, int] = {}           # 每餐几道菜；空则回落到 dishes_per_day
    breakfast_max_time_min: int = Field(default=BREAKFAST_MAX_TIME_DEFAULT, ge=5, le=30)

    @field_validator("taste_tags")
    @classmethod
    def _dedup(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    # —— 餐次的读取规则：**只写在这里**，别在别处再判断一遍 ——

    def active_meals(self) -> list[str]:
        """这一周要排哪几顿，按 早→午→晚 的固定顺序。

        认不出的餐次名直接丢掉（不报错）：老数据里没有这个字段，前端也可能传脏值，
        与其让整份方案排不出来，不如回落到"只做晚餐"这个最保守的默认。
        """
        picked = set(self.meals or [])
        ordered = [m for m in MEALS if m in picked]
        return ordered or [MEAL]

    def is_multi_meal(self) -> bool:
        return len(self.active_meals()) > 1

    def dishes_for(self, meal: str) -> int:
        """这一顿要几道菜：`dishes_per_meal` 优先，没写就用 `dishes_per_day`。

        两个字段并存是为了兼容已有的 3 份老方案（它们只有 `dishes_per_day`）——
        与其写一次猜测性的数据迁移，不如让读取时有一条明确的回落规则。
        """
        raw = self.dishes_per_meal.get(meal) or self.dishes_per_day
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = self.dishes_per_day
        return max(DISH_MIN, min(DISH_MAX, value))

    def time_cap_for(self, meal: str) -> int:
        """这一顿**单道菜**的时长上限：早餐有自己的上限，午/晚用 `max_time_min`。"""
        if meal == "早餐":
            return int(self.breakfast_max_time_min)
        return int(self.max_time_min)


# ---------- 中间状态 / 输出 ----------

class ChosenDish(BaseModel):
    recipe_id: str
    reason: str = ""


class DayPlan(BaseModel):
    day: int
    meal: str = MEAL
    dishes: list[ChosenDish] = []
    skipped: bool = False  # 客户说「这顿不做饭」（E-03 / docs/10）：不排菜、不计花费、不进清单
    people: Optional[int] = None  # 这一顿的临时人数（「来客人了」只改这一顿的份量，05 M1）

    def recipe_ids(self) -> list[str]:
        return [d.recipe_id for d in self.dishes]


def slot_key(day: int, meal: str) -> str:
    """"这一天这一顿"的字符串键（存 `PlanRecord.done_slots` 用）。

    用字符串而不是元组：JSON 里元组会退化成列表，两种后端读出来的形状就不一样了。
    """
    return f"{int(day)}|{meal}"


def parse_slot_key(key: str) -> Optional[tuple[int, str]]:
    """把 `slot_key` 拆回来；**认不出来就返回 None**（而不是硬给 (0, "") 这种假值）。

    键都是我们自己写进去的，认不出来只可能是数据坏了 —— 那就明说"坏了"，
    别造一个看起来正常的假结果让调用方接着往下走。
    """
    day, sep, meal = str(key).partition("|")
    if not sep or not meal:
        return None
    try:
        return int(day), meal
    except ValueError:
        return None


class ValidationIssue(BaseModel):
    level: Literal["error", "warning"]
    code: str  # allergen / duplicate / over_budget / over_time / goal / ...
    message: str
    day: Optional[int] = None
    meal: Optional[str] = None
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

    # —— 按"哪一天哪一顿"取菜（docs/10）——
    # 一天多顿之后，`next((p for p in result.days if p.day == X))` 这种写法会**静默拿到第一顿**
    # （早餐），是本轮最容易犯又最难看出来的错。所以取 DayPlan 一律走下面两个方法。

    def slots_for(self, day: int) -> list[DayPlan]:
        """这一天所有的餐，按 早→午→晚 的顺序（认不出的餐次排在最后）。"""
        order = {meal: i for i, meal in enumerate(MEALS)}
        return sorted((p for p in self.days if p.day == day),
                      key=lambda p: order.get(p.meal, len(MEALS)))

    def slot(self, day: int, meal: Optional[str] = None) -> Optional[DayPlan]:
        """指定某一顿；`meal=None` 取当天最后一顿（「今晚」页默认看的就是它）。"""
        slots = self.slots_for(day)
        if not slots:
            return None
        if meal is None:
            return slots[-1]
        return next((p for p in slots if p.meal == meal), None)


class PlanRecord(BaseModel):
    """一份方案存档（内存/JSON/DB 三种实现共用同一个结构）。"""

    id: str
    created_at: str = ""      # "2026-08-09 15:20"
    start_date: str = ""      # ISO 日期，这一周的第一天（周一）
    label: str = ""           # "8/12–8/18"
    change_note: str = ""     # 最近一次改动的说明
    done_days: list[int] = []        # **老字段**：当天**最后一顿**做完了（一天一顿时代＝晚餐）
    done_slots: list[str] = []       # "3|晚餐" 形式的"这一顿做过了"（docs/10 起只写这里）
    checked_items: list[str] = []    # 买菜清单的勾选（M4：关掉浏览器再打开还在）
    result: PlanResult

    def constraints(self) -> UserConstraints:
        return self.result.constraints

    def last_meal_of(self, day: int) -> str:
        """这一天的**最后一顿**（`done_days` 这个老字段说的就是它）。

        全项目"不给 meal"都表示当天最后一顿（`PlanResult.slot(day, None)`、`core.slot_of(day, None)`
        同一条规则），这里必须一致 —— 否则同一份老数据在"做完了/撤销"两处会落到不同的顿上。
        """
        slots = self.result.slots_for(int(day)) if self.result is not None else []
        return slots[-1].meal if slots else MEAL

    def is_done(self, day: int, meal: str) -> bool:
        """这一顿做过了没。

        **一律按顿判**（`done_slots`）。老字段 `done_days=[3]` 是"一天一顿"时代的写法，
        含义是**第 3 天的那一顿**（当时只有晚餐）—— 多餐之后按"当天最后一顿"解释。

        docs/11 §4.1 P0-5：原来这里写成"`day in done_days` 就直接 True"，
        于是"我勾了今晚做完了"会让**早/午/晚三顿全变已做过** ——
        用户看到的状态是假的，一旦发现就不会再认真用这个 App。
        """
        if slot_key(day, meal) in set(self.done_slots or []):
            return True
        if int(day) not in set(self.done_days or []):
            return False
        return meal == self.last_meal_of(day)

    def done_meals_of(self, day: int, meals: Optional[list[str]] = None) -> set[str]:
        """这一天哪些餐做过了（界面标"已做过"用）。"""
        wanted = list(meals or MEALS)
        return {m for m in wanted if self.is_done(day, m)}
