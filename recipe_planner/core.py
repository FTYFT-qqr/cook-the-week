"""确定性核心：检索过滤 / 计划评分 / 校验 / 修正 / 买菜清单。

这些函数不依赖 LLM，可离线测试 —— 是本项目“确定性校验而非全靠 LLM”的卖点。
"""
from __future__ import annotations

import math

from recipe_planner.db import load_db
from recipe_planner.models import (
    MEALS,
    DayPlan,
    ChosenDish,
    Recipe,
    RecipeDB,
    ShoppingItem,
    UserConstraints,
    ValidationIssue,
)

SPICE_ORDER = {"不辣": 0, "微辣": 1, "辣": 2}

# 蛋白质主料所在的食材类别（用于保证每顿“有肉/有蛋/有豆”）
PROTEIN_CATS = {"肉蛋", "水产", "豆制品"}
MAIN_PROTEIN_WORDS = ["鸡", "鸭", "牛", "猪", "鱼", "虾", "蛋", "豆腐", "里脊", "五花", "排", "腩", "翅"]


# ---------------------------------------------------------------- 检索

def in_meal_pool(r: Recipe, meal: str) -> bool:
    """这道菜属于这一顿的候选池吗（docs/10 §5）—— **全项目唯一的判定处**。

    - 早餐池：分类「早餐」+「主食」+ 主料里有蛋的菜（粥/面/饼/蛋类都算得上早饭）；
    - 午/晚池：除早餐菜以外的都能用（热菜/凉菜/汤/主食）。

    判定"含蛋"看的是**食材名**（`鸡蛋`/`皮蛋`…），不是菜名里的字 ——
    按名字猜菜系是另一种"两处规则迟早漂"的开端。
    """
    if meal == "早餐":
        return (r.category in ("早餐", "主食")
                or any("蛋" in i.name for i in r.ingredients))
    return r.category != "早餐"


def recipe_conflicts(r: Recipe, c: UserConstraints, meal: Optional[str] = None) -> list[str]:
    """返回该菜谱与硬约束的冲突代码列表（空=无冲突）。

    给了 `meal` 就按**那一顿**的口径判：候选池归属（`in_meal_pool`）与单菜时长上限
    （早餐默认 15 分钟）。不给 `meal` 时行为与 docs/10 之前**完全一致**（用 `max_time_min`），
    所以老调用方一条都不用改。
    """
    conflicts: list[str] = []
    if meal is not None and not in_meal_pool(r, meal):
        conflicts.append("meal")
    bad = r.all_allergen_names() & set(c.allergens)
    if bad:
        conflicts.append("allergen")
    if SPICE_ORDER.get(r.spice_level, 0) > SPICE_ORDER.get(c.spice_level, 0):
        conflicts.append("spice")
    time_cap = c.max_time_min if meal is None else c.time_cap_for(meal)
    if r.time_min > time_cap:
        conflicts.append("time")
    if getattr(c, "skill", "随便") == "新手" and r.difficulty == "较难":
        conflicts.append("difficulty")     # D4：新手不排功夫菜
    # docs/10：早餐菜（粥/面/蛋饼…）只在**勾了早餐**时才是候选，
    # 否则"只做晚餐"的用户会被排出一碗皮蛋瘦肉粥当晚饭。
    if r.category == "早餐" and "早餐" not in c.active_meals():
        conflicts.append("breakfast")
    return conflicts


def recipe_score(r: Recipe, c: UserConstraints) -> float:
    """软偏好评分：目标标签、口味标签、**逐菜权重**。

    docs/12 阶段二：`c.dish_weights` 有值时用它（一串带时间戳的事件算出来的
    "会衰减的记忆"，见 `preference.py`）；**空字典时保持老行为**（喜欢就 +8）——
    所以这条改动对老数据与既有测试是零影响，有偏好信号时才换挡。
    """
    score = 0.0
    if c.goal != "随便" and c.goal in r.goal_tags:
        score += 5.0
    if c.goal == "省钱" and r.cost_yuan <= 10:
        score += 2.0
    overlap = set(c.taste_tags) & set(r.taste_tags)
    score += 1.5 * len(overlap)
    weights = getattr(c, "dish_weights", None) or {}
    if weights:
        score += float(weights.get(r.id, 0.0))
    elif r.id in set(c.liked_dishes):
        score += 8.0  # 老行为：客户喜欢的菜强烈优先（没有事件时不知道"多久没吃了"）
    return score


def retrieve_candidates(db: RecipeDB, c: UserConstraints,
                        meal: Optional[str] = None) -> list[Recipe]:
    """硬过滤（过敏/辣度/时长/不喜欢的菜）后按软评分降序。

    `meal=None`（默认）就是 docs/10 之前的行为：不按餐分池、用全局 `max_time_min`。
    """
    disliked = set(c.disliked_dishes)
    out = []
    for r in db.recipes:
        if recipe_conflicts(r, c, meal):
            continue
        if r.id in disliked:
            continue  # 客户明确不喜欢的菜 → 硬性排除
        out.append(r)
    out.sort(key=lambda r: (-recipe_score(r, c), r.time_min, r.cost_yuan))
    return out


def pool_for_meal(candidates: list[Recipe], c: UserConstraints, meal: str) -> list[Recipe]:
    """把调用方给的候选**并集**切成"这一顿能用的菜"（docs/10）。

    刻意不调 `retrieve_candidates(db, c, meal)`：排菜拿到的候选是 graph 的 retrieve 节点
    给过来的（测试里也常常是自己造的候选），这里只做切分，不去重新读库。
    判定仍然复用 `recipe_conflicts(..., meal)` —— 池子归属规则只有一处。
    """
    return [r for r in candidates if not recipe_conflicts(r, c, meal)]


def where_text(day: int, meal: "Optional[str]", c: UserConstraints, spaced: bool = True) -> str:
    """「第 3 天」/「第 3 天午餐」—— 措辞规则**只写在这里**。

    只做一顿时（或没给餐次）**不加餐次**，老措辞一个字都不改；
    一天多顿时必须说清是哪一顿，否则回执写"已把第 3 天换成…"根本看不出动的是早餐还是晚餐。

    `spaced`：界面/回执是"第 3 天"，校验消息（docs 早期就定了，有测试盯着）是"第3天"。
    """
    if not (meal and c.is_multi_meal()):
        return f"第 {day} 天" if spaced else f"第{day}天"
    return f"第 {day} 天{meal}" if spaced else f"第{day}天{meal}"


def _where(day: int, meal: str, c: UserConstraints) -> str:
    """"第 N 天"还是"第 N 天午餐" —— 多顿才说餐次，单顿时保持原来的措辞。"""
    return where_text(day, meal, c, spaced=False)


def day_slots(plans: list[DayPlan], day_no: int) -> list[DayPlan]:
    """这一天所有的餐（算"当天总花费"时必须用这个，预算是一整天的）。"""
    return [p for p in plans if p.day == day_no]


def slot_of(plans: list[DayPlan], day_no: int, meal: "Optional[str]" = None) -> "Optional[DayPlan]":
    """取「这一天这一顿」的计划条目（docs/10）。

    一天多顿之后**不能**再用 `next(p for p in plans if p.day == X)`：那会静默拿到**早餐** ——
    于是"今晚换一道"去改了早上的粥，而界面上完全看不出来。取 DayPlan 一律走这里。
    `meal=None` = 当天最后一顿（界面「今晚」页看的就是它）。
    """
    slots = day_slots(plans, day_no)
    if not slots:
        return None
    if meal is None:
        order = {m: i for i, m in enumerate(MEALS)}
        return sorted(slots, key=lambda p: order.get(p.meal, len(MEALS)))[-1]
    return next((p for p in slots if p.meal == meal), None)


def with_slot(plans: list[DayPlan], target: "Optional[DayPlan]",
              new_slot: DayPlan) -> list[DayPlan]:
    """把 plans 里的 target 换成 new_slot，其余原样。按对象身份比较（不做深比较，也不会误伤同名的一天）。"""
    return [new_slot if p is target else p.model_copy(deep=True) for p in plans]


# ---------------------------------------------------------------- 确定性排菜

def _day_total_cost(plan: DayPlan, db: RecipeDB, c: UserConstraints) -> float:
    people = plan.people or c.people      # 「来客人了」只改这一天的份量
    total = 0.0
    for dish in plan.dishes:
        r = db.by_id(dish.recipe_id)
        if r:
            total += r.cost_yuan * people / 2.0
    return total


def make_reason(r: Recipe, c: UserConstraints) -> str:
    """模板化理由（确定性计划用）。"""
    bits = []
    if c.goal != "随便" and c.goal in r.goal_tags:
        bits.append(f"契合你的「{c.goal}」目标")
    elif r.goal_tags:
        bits.append("营养搭配均衡")
    if r.time_min <= 20:
        bits.append("快手省时")
    if r.cost_yuan <= 12:
        bits.append("成本友好")
    if r.category == "汤":
        bits.append("滋润暖胃")
    if not bits:
        bits.append("家常好味")
    return f"{r.name}：{'，'.join(bits)}。约 {r.time_min} 分钟，成本约 {r.cost_yuan} 元/份（{r.category}）。"


def _has_protein(r: Recipe) -> bool:
    return any(ing.category in PROTEIN_CATS for ing in r.ingredients) or any(
        w in r.name for w in MAIN_PROTEIN_WORDS
    )


def plan_deterministic(candidates: list[Recipe], db: RecipeDB, c: UserConstraints) -> tuple[list[DayPlan], list[ValidationIssue]]:
    """贪心逐天**逐顿**排菜：一周内不重复、每顿尽量荤素搭配、满足**当天**预算。

    docs/10 之后一天可能有早/午/晚多顿：

    - 候选池与单菜时长上限**按餐分开**（早餐 ≤15 分钟，池子是早餐+主食+蛋类）；
    - 结构规则（第一道偏主菜、第二道起补蔬菜/汤）**按餐各跑一遍**，规则本身没改；
    - 预算仍然是"每人每天"，所以 `spent` 在一天内**跨餐累加** —— 不能每顿各花一份预算。

    返回 (每一顿一条 DayPlan, 附带警告)。可能因候选不足而少菜——由校验层报 warning。
    """
    used_ids: set[str] = set()
    plans: list[DayPlan] = []
    warnings: list[ValidationIssue] = []
    meals = c.active_meals()
    budget_per_day = None
    if c.budget_per_person_day is not None:
        budget_per_day = c.budget_per_person_day * c.people  # 一天总预算（含当天所有餐）

    # 硬性指定菜优先占位：放在当天**最后一顿**（一般是晚餐；只勾早餐时就是早餐）
    reserved: dict[int, list[str]] = {}
    for i, rid in enumerate(c.must_include_recipes):
        reserved.setdefault(i % max(c.days, 1), []).append(rid)
        used_ids.add(rid)

    for day_no in range(1, c.days + 1):
        spent = 0.0
        for meal in meals:
            plan_slot = DayPlan(day=day_no, meal=meal, dishes=[])
            # 指定菜先入
            if meal == meals[-1]:
                for rid in reserved.get(day_no - 1, []):
                    r = db.by_id(rid)
                    if r is not None:
                        plan_slot.dishes.append(ChosenDish(recipe_id=r.id, reason=make_reason(r, c)))
                        spent += r.cost_yuan * c.people / 2.0

            # 每一顿都从"第一道偏主菜"重新开始（结构规则按餐跑）
            prefer_protein = not any(_has_protein(db.by_id(d.recipe_id))
                                     for d in plan_slot.dishes if db.by_id(d.recipe_id))
            need = c.dishes_for(meal)
            pool = pool_for_meal(candidates, c, meal)
            day_used = set(plan_slot.recipe_ids())
            for _ in range(max(need - len(plan_slot.dishes), 0)):
                feasible = [r for r in pool
                            if r.id not in day_used and r.id not in used_ids
                            and (budget_per_day is None
                                 or spent + r.cost_yuan * c.people / 2.0 <= budget_per_day + 1e-6)]
                if not feasible:
                    break
                # 主菜优先取评分高且含蛋白的；第二道起优先非蛋白（素/汤）追求多样性
                def pick_key(r: Recipe):
                    is_protein = _has_protein(r)
                    diversity_bonus = 0.0
                    if prefer_protein and is_protein:
                        diversity_bonus = 3.0
                    if not prefer_protein and not is_protein:
                        diversity_bonus = 1.5
                    return (-(recipe_score(r, c) + diversity_bonus), r.time_min)

                feasible.sort(key=pick_key)
                chosen = feasible[0]
                pool.remove(chosen)
                used_ids.add(chosen.id)
                day_used.add(chosen.id)
                plan_slot.dishes.append(ChosenDish(recipe_id=chosen.id, reason=make_reason(chosen, c)))
                spent += chosen.cost_yuan * c.people / 2.0
                prefer_protein = not _has_protein(chosen)

            plans.append(plan_slot)
            if len(plan_slot.dishes) < need:
                warnings.append(
                    ValidationIssue(level="warning", code="shortage",
                                    message=f"{_where(day_no, meal, c)}候选不足，"
                                            f"只排了{len(plan_slot.dishes)}道菜", day=day_no)
                )
    return plans, warnings


# ---------------------------------------------------------------- 校验

def validate_plan(plans: list[DayPlan], db: RecipeDB, c: UserConstraints) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen: dict[str, tuple[int, str]] = {}  # recipe_id -> (day, meal)

    # 预算与人数都是**按天**的（docs/10 §2）：一天多顿时必须先把当天各餐加起来，
    # 否则"每人每天 ¥45"会被当成"每顿 ¥45"，预算校验形同虚设。
    cost_by_day: dict[int, float] = {}
    people_by_day: dict[int, int] = {}
    for p in plans:
        cost_by_day[p.day] = cost_by_day.get(p.day, 0.0) + _day_total_cost(p, db, c)
        people_by_day[p.day] = max(people_by_day.get(p.day, 0), p.people or c.people)

    for p in plans:
        where = _where(p.day, p.meal, c)
        time_cap = c.time_cap_for(p.meal)
        for dish in p.dishes:
            r = db.by_id(dish.recipe_id)
            if r is None:
                issues.append(ValidationIssue(level="error", code="unknown_recipe",
                                              message=f"{where}引用了不存在的菜谱 {dish.recipe_id}", day=p.day))
                continue
            # 过敏原
            bad = r.all_allergen_names() & set(c.allergens)
            if bad:
                issues.append(ValidationIssue(level="error", code="allergen",
                                              message=f"{where}「{r.name}」含过敏原：{'、'.join(sorted(bad))}",
                                              day=p.day, recipe_id=r.id))
            if SPICE_ORDER.get(r.spice_level, 0) > SPICE_ORDER.get(c.spice_level, 0):
                issues.append(ValidationIssue(level="error", code="spice",
                                              message=f"{where}「{r.name}」辣度({r.spice_level})超出你的耐受", day=p.day, recipe_id=r.id))
            if r.time_min > time_cap:
                issues.append(ValidationIssue(level="error", code="over_time",
                                              message=f"{where}「{r.name}」需{r.time_min}分钟，超出上限{time_cap}分钟", day=p.day, recipe_id=r.id))
            if getattr(c, "skill", "随便") == "新手" and r.difficulty == "较难":
                issues.append(ValidationIssue(level="warning", code="difficulty",
                                              message=f"{where}「{r.name}」对新手偏难（{r.difficulty}）", day=p.day, recipe_id=r.id))
            # 客户明确不喜欢的菜
            if r.id in set(c.disliked_dishes):
                issues.append(ValidationIssue(level="error", code="disliked",
                                              message=f"{where}「{r.name}」是你明确不喜欢的菜", day=p.day, recipe_id=r.id))
            # 重复（一周内不重样；同一天的两顿也算重复）
            if dish.recipe_id in seen:
                was = seen[dish.recipe_id]
                issues.append(ValidationIssue(level="error", code="duplicate",
                                              message=f"「{r.name}」在{_where(was[0], was[1], c)}和{where}重复了",
                                              day=p.day, recipe_id=r.id))
            else:
                seen[dish.recipe_id] = (p.day, p.meal)
        # 预算（当天合计，含当天所有餐）
        if c.budget_per_person_day is not None:
            limit = c.budget_per_person_day * people_by_day.get(p.day, c.people)
            if cost_by_day.get(p.day, 0.0) > limit + 1e-6:
                issues.append(ValidationIssue(level="error", code="over_budget",
                                              message=f"{_where(p.day, p.meal, c)}当天合计约 "
                                                      f"{cost_by_day[p.day]:.1f} 元，超出当日预算 {limit:.1f} 元",
                                              day=p.day))
        # 目标覆盖（软）——「这顿不做饭」的日子不检查
        if c.goal != "随便" and p.dishes:
            day_goals = [r.goal_tags for dish in p.dishes if (r := db.by_id(dish.recipe_id))]
            if not any(c.goal in tg for tg in day_goals):
                issues.append(ValidationIssue(level="warning", code="goal",
                                              message=f"{where}没有契合「{c.goal}」目标的菜", day=p.day))
    return issues


# ---------------------------------------------------------------- 修正

def repair_plan(plans: list[DayPlan], candidates: list[Recipe], db: RecipeDB, c: UserConstraints,
                issues: list[ValidationIssue]) -> tuple[list[DayPlan], bool]:
    """对硬错误做确定性修正：重新用确定性排菜保证约束，返回 (计划, 是否已无硬错)。"""
    new_plans, warns = plan_deterministic(candidates, db, c)
    hard = [i for i in issues if i.level == "error"]
    # 若原计划没有未知菜谱等灾难性问题，优先保留确定性结果即可
    return new_plans, len(hard) == 0


# ---------------------------------------------------------------- 买菜清单

def shopping_list(plans: list[DayPlan], db: RecipeDB, c: UserConstraints) -> list[ShoppingItem]:
    """汇总买菜清单。

    重要：菜谱食材是「约 2 人份」，必须按 c.people 折算，
    否则 1 人和 4 人会买到完全一样的量（客户买菜量错误）。
    """
    pantry = {s.strip() for s in c.pantry_items if s and s.strip()}
    agg: dict[tuple[str, str], dict] = {}  # (name, category) -> {grams, amount_texts, recipes}

    def norm(s: str) -> str:
        return "".join(s.split())

    for p in plans:
        scale = (p.people or c.people) / 2.0     # 每天都可能人数不同（来客人了）
        for dish in p.dishes:
            r = db.by_id(dish.recipe_id)
            if not r:
                continue
            for ing in r.ingredients:
                if ing.category in {"调料", "其他"}:
                    continue  # 基础调料不算入采购项（油盐酱醋一般家里有）
                key = (norm(ing.name), ing.category)
                slot = agg.setdefault(key, {"grams": 0.0, "texts": [], "recipes": []})
                if ing.grams:
                    slot["grams"] += ing.grams * scale
                if ing.amount:
                    slot["texts"].append(ing.amount)
                if r.name not in slot["recipes"]:
                    slot["recipes"].append(r.name)

    items: list[ShoppingItem] = []
    for (name, cat), slot in agg.items():
        # 匹配：库存关键词与食材名双向包含即可视为已覆盖
        covered = any(norm(pw) in name or name in norm(pw) for pw in pantry)
        if slot["grams"]:
            g = slot["grams"]
            # 国人买菜论斤，≥500g 时附上斤数
            amount = f"约 {g / 500:.1f} 斤（{g:.0f} 克）" if g >= 500 else f"约 {g:.0f} 克"
        else:
            amount = "、".join(sorted(set(slot["texts"])))
            if c.people != 2:
                amount += f"（按 {c.people} 人调整）"
        items.append(ShoppingItem(name=name, category=cat, amount=amount,
                                  needed=not covered, for_recipes=slot["recipes"]))
    order = {"蔬菜": 0, "菌菇": 1, "肉蛋": 2, "水产": 3, "豆制品": 4, "主食": 5, "干货": 6}
    items.sort(key=lambda it: (order.get(it.category, 99), it.name))
    return items


# ---------------------------------------------------------------- 单道替换（客户反馈用）

def pick_replacement(plans: list[DayPlan], day_no: int, old_id: str, db: RecipeDB,
                     c: UserConstraints, meal: "Optional[str]" = None) -> Recipe | None:
    """为某天某顿挑一个替换菜：满足硬约束、全周不重复、不超当天预算。"""
    used = {d.recipe_id for p in plans for d in p.dishes}
    target = slot_of(plans, day_no, meal)
    # 预算按**整天**算（docs/10）：把当天其它餐的花费也算进来
    others = [d.recipe_id for p in day_slots(plans, day_no) for d in p.dishes
              if not (target is not None and p is target and d.recipe_id == old_id)]
    day_others_cost = sum((db.by_id(rid).cost_yuan * c.people / 2.0)
                          for rid in set(others) if db.by_id(rid))
    limit = c.budget_per_person_day * c.people if c.budget_per_person_day is not None else None

    pool = retrieve_candidates(db, c, target.meal if target is not None else meal)
    for r in pool:  # 已按软偏好（含"喜欢"加权）排序
        if r.id == old_id or r.id in used:
            continue
        if limit is not None and day_others_cost + r.cost_yuan * c.people / 2.0 > limit + 1e-6:
            continue
        return r
    return None


def swap_dish(plans: list[DayPlan], day_no: int, old_id: str, db: RecipeDB,
              c: UserConstraints, meal: "Optional[str]" = None) -> tuple[list[DayPlan], Recipe | None]:
    """只替换某天某顿的一道菜，其余原样保留（点「换一道」或「不喜欢」时使用）。"""
    rep = pick_replacement(plans, day_no, old_id, db, c, meal)
    if rep is None:
        return [p.model_copy(deep=True) for p in plans], None
    target = slot_of(plans, day_no, meal)
    if target is None:
        return [p.model_copy(deep=True) for p in plans], None
    dishes = [
        ChosenDish(recipe_id=rep.id, reason=make_reason(rep, c)) if d.recipe_id == old_id else d
        for d in target.dishes
    ]
    new_slot = DayPlan(day=target.day, meal=target.meal, dishes=dishes,
                       skipped=target.skipped, people=target.people)
    return with_slot(plans, target, new_slot), rep


def refresh_result(result, db: RecipeDB):
    """替换单道菜后同步刷新清单/费用/校验，保证界面与数据一致。"""
    c = result.constraints
    result.shopping = shopping_list(result.days, db, c)
    result.issues = validate_plan(result.days, db, c)
    total = 0.0
    for p in result.days:
        for d in p.dishes:
            r = db.by_id(d.recipe_id)
            if r:
                total += r.cost_yuan * c.people / 2.0
    result.estimated_cost_yuan = round(total, 2)
    result.final = not any(i.level == "error" for i in result.issues)
    return result


# ---------------------------------------------------------------- 人话改菜单（E2 / E-03 / E-05 / E-02）
# 客户的一句话（"周二换成鱼""这天不做饭""帮我省点"）落到这里变成对菜单的一次具体改动。

def recipes_matching(db: RecipeDB, keyword: str) -> list[Recipe]:
    """按关键词找菜：菜名 / 主料 / 口味标签 / 类别 里包含这个词就算。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    out: list[Recipe] = []
    for r in db.recipes:
        text = r.name + r.category + "".join(i.name for i in r.ingredients) + "".join(r.taste_tags)
        if kw in text:
            out.append(r)
    return out


def replace_in_day(plans: list[DayPlan], day_no: int, old_id: str, new_recipe: Recipe,
                   c: UserConstraints, meal: "Optional[str]" = None) -> list[DayPlan]:
    """把某天某顿的一道菜换成指定菜，其余原样保留。

    `meal=None` = 当天最后一顿（单顿时就是那一天，与 docs/10 之前完全一致）。
    """
    target = slot_of(plans, day_no, meal)
    if target is None:
        return [p.model_copy(deep=True) for p in plans]
    dishes = [
        ChosenDish(recipe_id=new_recipe.id, reason=make_reason(new_recipe, c))
        if d.recipe_id == old_id else d
        for d in target.dishes
    ]
    new_slot = DayPlan(day=target.day, meal=target.meal, dishes=dishes,
                       skipped=target.skipped, people=target.people)
    return with_slot(plans, target, new_slot)


def candidates_matching(plans: list[DayPlan], day_no: int, keyword: str, db: RecipeDB,
                        c: UserConstraints, replace_id: str | None = None,
                        meal: "Optional[str]" = None) -> list[Recipe]:
    """在「满足硬约束 + 全周不重复 + 不超当天预算」的前提下，找匹配关键词的候选菜。

    docs/10：给了 `meal` 就只在**那一顿能用的菜**里找（否则「早餐来个鱼」会排上一道硬菜）。
    """
    allowed = {r.id for r in recipes_matching(db, keyword)}
    if not allowed:
        return []
    used = {d.recipe_id for p in plans for d in p.dishes if d.recipe_id != replace_id}
    # 预算按**整天**算（docs/10）：当天其它餐的花费也要算进来
    others_cost = 0.0
    for slot in day_slots(plans, day_no):
        for d in slot.dishes:
            if d.recipe_id == replace_id:
                continue
            r = db.by_id(d.recipe_id)
            if r:
                others_cost += r.cost_yuan * c.people / 2.0
    limit = c.budget_per_person_day * c.people if c.budget_per_person_day is not None else None
    pool = retrieve_candidates(db, c)
    if meal is not None:
        pool = pool_for_meal(pool, c, meal)
    out: list[Recipe] = []
    for r in pool:                              # 已按软偏好（含"喜欢"加权）排序
        if r.id in used or r.id not in allowed:
            continue
        if limit is not None and others_cost + r.cost_yuan * c.people / 2.0 > limit + 1e-6:
            continue
        out.append(r)
    return out


def skip_day(plans: list[DayPlan], day_no: int, meal: "Optional[str]" = None) -> list[DayPlan]:
    """「这顿不做饭」：清空那一顿的菜（清单与花费自然跟着变）。

    docs/10：清的是**这一顿**（`meal=None` = 当天最后一顿，单顿时即那一天）。
    """
    target = slot_of(plans, day_no, meal)
    if target is None:
        return [p.model_copy(deep=True) for p in plans]
    new_slot = DayPlan(day=target.day, meal=target.meal, dishes=[], skipped=True,
                       people=target.people)
    return with_slot(plans, target, new_slot)


def restore_day(plans: list[DayPlan], day_no: int, db: RecipeDB,
                c: UserConstraints, meal: "Optional[str]" = None) -> list[DayPlan]:
    """把「这顿不做饭」改回来：重新给这一顿挑菜，不与其他餐重复、不超当天预算。

    docs/10：候选池与道数都按**这一顿**来（早餐池 + 早餐道数），
    `meal=None` = 当天最后一顿（单顿时与 docs/10 之前完全一致）。
    """
    target = slot_of(plans, day_no, meal)
    if target is None:
        return [p.model_copy(deep=True) for p in plans]
    used = {d.recipe_id for p in plans for d in p.dishes}
    limit = c.budget_per_person_day * c.people if c.budget_per_person_day is not None else None
    pool = pool_for_meal(retrieve_candidates(db, c), c, target.meal)
    pool = [r for r in pool if r.id not in used]
    picked: list[ChosenDish] = []
    spent = 0.0
    need_protein = True
    while len(picked) < c.dishes_for(target.meal) and pool:
        pool.sort(key=lambda r: (-(recipe_score(r, c) + (3.0 if (need_protein and _has_protein(r)) else 0.0)),
                                 r.time_min))
        chosen = None
        for r in pool:
            if limit is not None and spent + r.cost_yuan * c.people / 2.0 > limit + 1e-6:
                continue
            chosen = r
            break
        if chosen is None:
            break
        pool.remove(chosen)
        picked.append(ChosenDish(recipe_id=chosen.id, reason=make_reason(chosen, c)))
        spent += chosen.cost_yuan * c.people / 2.0
        if _has_protein(chosen):
            need_protein = False
    new_slot = DayPlan(day=target.day, meal=target.meal, dishes=picked, skipped=False,
                       people=target.people)
    return with_slot(plans, target, new_slot)


def _swap_by_rule(plans: list[DayPlan], db: RecipeDB, c: UserConstraints,
                  chooser) -> tuple[list[DayPlan], int, str, Recipe, Recipe] | None:
    """通用：按 chooser(当天菜) 决定要换掉哪道，再找替换菜。

    docs/10：候选池按**那一顿**取 —— 否则「太油腻了」可能把晚餐的肉换成早餐的粥。
    """
    used = {d.recipe_id for p in plans for d in p.dishes}
    for p in plans:
        pool = pool_for_meal(retrieve_candidates(db, c), c, p.meal)
        for d in p.dishes:
            old = db.by_id(d.recipe_id)
            if old is None:
                continue
            kind = chooser(p, old)
            if kind is None:
                continue
            for cand in pool:
                if cand.id in used or cand.id == old.id:
                    continue
                if kind == "cheaper" and cand.cost_yuan >= old.cost_yuan * 0.7:
                    continue
                if kind == "protein" and not _has_protein(cand):
                    continue
                if kind == "veg" and _has_protein(cand):
                    continue
                if not _fits_day_budget(plans, p.day, d.recipe_id, cand, db, c):
                    continue
                return (replace_in_day(plans, p.day, d.recipe_id, cand, c, p.meal),
                        p.day, p.meal, old, cand)
    return None


def _fits_day_budget(plans: list[DayPlan], day_no: int, replace_id: str, cand: Recipe,
                     db: RecipeDB, c: UserConstraints) -> bool:
    """换成 cand 之后，**当天**（含当天所有餐）还超不超预算。"""
    if c.budget_per_person_day is None:
        return True
    total = 0.0
    for slot in day_slots(plans, day_no):
        for d in slot.dishes:
            if d.recipe_id == replace_id:
                continue
            r = db.by_id(d.recipe_id)
            if r:
                total += r.cost_yuan * c.people / 2.0
    return total + cand.cost_yuan * c.people / 2.0 <= c.budget_per_person_day * c.people + 1e-6


def cheapest_swap(plans: list[DayPlan], db: RecipeDB, c: UserConstraints
                  ) -> tuple[list[DayPlan], int, str, Recipe, Recipe, float] | None:
    """E-05：把最贵的一道换成更便宜的一道，并算出省了多少钱。

    docs/10：最贵的那道在**哪一顿**，就去**那一顿**的池子里找替代 ——
    不能用全局池，否则会把晚餐的硬菜换成早餐的燕麦牛奶。返回值里也带上餐次。
    """
    best = None
    for p in plans:
        for d in p.dishes:
            r = db.by_id(d.recipe_id)
            if r is not None and (best is None or r.cost_yuan > best[2].cost_yuan):
                best = (p, d, r)
    if best is None or best[2].cost_yuan <= 0:
        return None
    slot, d, old = best
    day_no, meal = slot.day, slot.meal
    used = {x.recipe_id for pp in plans for x in pp.dishes}
    pool = [r for r in pool_for_meal(retrieve_candidates(db, c), c, meal)
            if r.id not in used and r.cost_yuan < old.cost_yuan * 0.7
            and _fits_day_budget(plans, day_no, old.id, r, db, c)]
    if not pool:
        return None
    pool.sort(key=lambda r: (r.cost_yuan, -recipe_score(r, c)))
    new_recipe = pool[0]
    saving = (old.cost_yuan - new_recipe.cost_yuan) * c.people / 2.0
    return (replace_in_day(plans, day_no, old.id, new_recipe, c, meal),
            day_no, meal, old, new_recipe, saving)


def best_day_no(plans: list[DayPlan], recipe: Recipe) -> int:
    for p in plans:
        if any(d.recipe_id == recipe.id for d in p.dishes):
            return p.day
    return 1


def protein_swap(plans: list[DayPlan], db: RecipeDB,
                 c: UserConstraints) -> tuple[list[DayPlan], int, Recipe, Recipe] | None:
    """「这周别太素」：找一天缺蛋白的菜换掉。"""
    return _swap_by_rule(plans, db, c,
                         lambda p, old: "protein" if not _has_protein(old) and not any(
                             _has_protein(db.by_id(d.recipe_id)) for d in p.dishes
                             if db.by_id(d.recipe_id) and d.recipe_id != old.id) else None)


def veg_swap(plans: list[DayPlan], db: RecipeDB,
             c: UserConstraints) -> tuple[list[DayPlan], int, Recipe, Recipe] | None:
    """「太油腻了」：把一道荤菜换成素菜。"""
    return _swap_by_rule(plans, db, c,
                         lambda p, old: "veg" if _has_protein(old) else None)


def fastest_day(plans: list[DayPlan], day_no: int, db: RecipeDB,
                c: UserConstraints, meal: "Optional[str]" = None) -> tuple[list[DayPlan], list[Recipe]] | None:
    """「回家晚了」：只把这一顿换成最快能做完的组合（05 M1）。

    docs/10：换的是**这一顿**（`meal=None` = 当天最后一顿），候选池与道数按餐来。
    """
    target = slot_of(plans, day_no, meal)
    if target is None:
        return None
    used = {d.recipe_id for p in plans for d in p.dishes if p is not target}
    pool = pool_for_meal(retrieve_candidates(db, c), c, target.meal)
    pool = [r for r in pool if r.id not in used]
    pool.sort(key=lambda r: (r.time_min, -(recipe_score(r, c))))
    picked: list[Recipe] = []
    spent = 0.0
    people = target.people or c.people
    limit = c.budget_per_person_day * people if c.budget_per_person_day else None
    for r in pool:
        if len(picked) >= c.dishes_for(target.meal):
            break
        if limit is not None and spent + r.cost_yuan * people / 2.0 > limit + 1e-6:
            continue
        picked.append(r)
        spent += r.cost_yuan * people / 2.0
    if not picked:
        return None
    if target.dishes and max(r.time_min for r in picked) >= max(
            (db.by_id(d.recipe_id).time_min for d in target.dishes if db.by_id(d.recipe_id)), default=999):
        return None       # 换不更快就没必要换
    new_slot = DayPlan(day=target.day, meal=target.meal, skipped=False, people=target.people,
                       dishes=[ChosenDish(recipe_id=r.id, reason=f"快手：约 {r.time_min} 分钟就能上桌。")
                               for r in picked])
    return with_slot(plans, target, new_slot), picked
