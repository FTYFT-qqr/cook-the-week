"""确定性核心：检索过滤 / 计划评分 / 校验 / 修正 / 买菜清单。

这些函数不依赖 LLM，可离线测试 —— 是本项目“确定性校验而非全靠 LLM”的卖点。
"""
from __future__ import annotations

import math

from recipe_planner.db import load_db
from recipe_planner.models import (
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

def recipe_conflicts(r: Recipe, c: UserConstraints) -> list[str]:
    """返回该菜谱与硬约束的冲突代码列表（空=无冲突）。"""
    conflicts: list[str] = []
    bad = r.all_allergen_names() & set(c.allergens)
    if bad:
        conflicts.append("allergen")
    if SPICE_ORDER.get(r.spice_level, 0) > SPICE_ORDER.get(c.spice_level, 0):
        conflicts.append("spice")
    if r.time_min > c.max_time_min:
        conflicts.append("time")
    return conflicts


def recipe_score(r: Recipe, c: UserConstraints) -> float:
    """软偏好评分：喜欢的菜、目标标签、口味标签、辣度接近度。"""
    score = 0.0
    if c.goal != "随便" and c.goal in r.goal_tags:
        score += 5.0
    if c.goal == "省钱" and r.cost_yuan <= 10:
        score += 2.0
    overlap = set(c.taste_tags) & set(r.taste_tags)
    score += 1.5 * len(overlap)
    if r.id in set(c.liked_dishes):
        score += 8.0  # 客户喜欢的菜强烈优先
    return score


def retrieve_candidates(db: RecipeDB, c: UserConstraints) -> list[Recipe]:
    """硬过滤（过敏/辣度/时长/不喜欢的菜）后按软评分降序。"""
    disliked = set(c.disliked_dishes)
    out = []
    for r in db.recipes:
        if recipe_conflicts(r, c):
            continue
        if r.id in disliked:
            continue  # 客户明确不喜欢的菜 → 硬性排除
        out.append(r)
    out.sort(key=lambda r: (-recipe_score(r, c), r.time_min, r.cost_yuan))
    return out


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
    """贪心逐天排菜：不重复、每天尽量荤素搭配、满足日预算。

    返回 (每日计划, 附带警告)。可能因候选不足而少菜——由校验层报 warning。
    """
    used_ids: set[str] = set()
    days: list[DayPlan] = []
    warnings: list[ValidationIssue] = []
    budget_per_day = None
    if c.budget_per_person_day is not None:
        budget_per_day = c.budget_per_person_day * c.people  # 一天总预算

    remaining = [r for r in candidates]

    # 硬性指定菜优先占位
    reserved: dict[int, list[str]] = {}
    for i, rid in enumerate(c.must_include_recipes):
        reserved.setdefault(i % c.days, []).append(rid)
        used_ids.add(rid)

    for day_no in range(1, c.days + 1):
        plan_day = DayPlan(day=day_no, dishes=[])
        # 指定菜先入
        for rid in reserved.get(day_no - 1, []):
            r = db.by_id(rid)
            if r:
                plan_day.dishes.append(ChosenDish(recipe_id=r.id, reason=make_reason(r, c)))
        day_used = {d.recipe_id for d in plan_day.dishes}
        spent = _day_total_cost(plan_day, db, c)

        # 每一天尽量保证：第一道偏“主菜”(含蛋白)，第二道起补蔬菜/汤
        pool = [r for r in remaining if r.id not in day_used and r.id not in used_ids]
        prefer_protein = not any(_has_protein(db.by_id(d.recipe_id)) for d in plan_day.dishes if db.by_id(d.recipe_id))
        slots = c.dishes_per_day - len(plan_day.dishes)
        for _ in range(max(slots, 0)):
            feasible = []
            for r in pool:
                if budget_per_day is not None and spent + r.cost_yuan * c.people / 2.0 > budget_per_day + 1e-6:
                    continue
                feasible.append(r)
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
            if chosen.id not in used_ids:
                used_ids.add(chosen.id)
            day_used.add(chosen.id)
            plan_day.dishes.append(ChosenDish(recipe_id=chosen.id, reason=make_reason(chosen, c)))
            spent += chosen.cost_yuan * c.people / 2.0
            if _has_protein(chosen):
                prefer_protein = False
            else:
                # 已有素菜则下轮优先蛋白，避免全天无肉
                if not any(_has_protein(db.by_id(d.recipe_id)) for d in plan_day.dishes if db.by_id(d.recipe_id)):
                    prefer_protein = True

        days.append(plan_day)
        if len(plan_day.dishes) < c.dishes_per_day:
            warnings.append(
                ValidationIssue(level="warning", code="shortage",
                                message=f"第{day_no}天候选不足，只排了{len(plan_day.dishes)}道菜", day=day_no)
            )
    return days, warnings


# ---------------------------------------------------------------- 校验

def validate_plan(plans: list[DayPlan], db: RecipeDB, c: UserConstraints) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen_ids: dict[str, int] = {}  # recipe_id -> day

    for p in plans:
        day_total = _day_total_cost(p, db, c)
        for dish in p.dishes:
            r = db.by_id(dish.recipe_id)
            if r is None:
                issues.append(ValidationIssue(level="error", code="unknown_recipe",
                                              message=f"第{p.day}天引用了不存在的菜谱 {dish.recipe_id}", day=p.day))
                continue
            # 过敏原
            bad = r.all_allergen_names() & set(c.allergens)
            if bad:
                issues.append(ValidationIssue(level="error", code="allergen",
                                              message=f"第{p.day}天「{r.name}」含过敏原：{'、'.join(sorted(bad))}",
                                              day=p.day, recipe_id=r.id))
            if SPICE_ORDER.get(r.spice_level, 0) > SPICE_ORDER.get(c.spice_level, 0):
                issues.append(ValidationIssue(level="error", code="spice",
                                              message=f"第{p.day}天「{r.name}」辣度({r.spice_level})超出你的耐受", day=p.day, recipe_id=r.id))
            if r.time_min > c.max_time_min:
                issues.append(ValidationIssue(level="error", code="over_time",
                                              message=f"第{p.day}天「{r.name}」需{r.time_min}分钟，超出上限{c.max_time_min}分钟", day=p.day, recipe_id=r.id))
            # 客户明确不喜欢的菜
            if r.id in set(c.disliked_dishes):
                issues.append(ValidationIssue(level="error", code="disliked",
                                              message=f"第{p.day}天「{r.name}」是你明确不喜欢的菜", day=p.day, recipe_id=r.id))
            # 重复
            if dish.recipe_id in seen_ids:
                issues.append(ValidationIssue(level="error", code="duplicate",
                                              message=f"「{r.name}」在第{seen_ids[dish.recipe_id]}天和第{p.day}天重复了", day=p.day, recipe_id=r.id))
            else:
                seen_ids[dish.recipe_id] = p.day
        # 预算
        if c.budget_per_person_day is not None:
            limit = c.budget_per_person_day * (p.people or c.people)
            if day_total > limit + 1e-6:
                issues.append(ValidationIssue(level="error", code="over_budget",
                                              message=f"第{p.day}天合计约 {day_total:.1f} 元，超出当日预算 {limit:.1f} 元", day=p.day))
        # 目标覆盖（软）——「这天不做饭」的日子不检查
        if c.goal != "随便" and p.dishes:
            day_goals = [r.goal_tags for dish in p.dishes if (r := db.by_id(dish.recipe_id))]
            if not any(c.goal in tg for tg in day_goals):
                issues.append(ValidationIssue(level="warning", code="goal",
                                              message=f"第{p.day}天没有契合「{c.goal}」目标的菜", day=p.day))
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
                     c: UserConstraints) -> Recipe | None:
    """为某天挑一个替换菜：满足硬约束、全周不重复、不超当天预算。"""
    used = {d.recipe_id for p in plans for d in p.dishes}
    day = next((p for p in plans if p.day == day_no), None)
    others = [d.recipe_id for d in day.dishes if d.recipe_id != old_id] if day else []
    day_others_cost = sum((db.by_id(rid).cost_yuan * c.people / 2.0)
                          for rid in others if db.by_id(rid))
    limit = c.budget_per_person_day * c.people if c.budget_per_person_day is not None else None

    for r in retrieve_candidates(db, c):  # 已按软偏好（含"喜欢"加权）排序
        if r.id == old_id or r.id in used:
            continue
        if limit is not None and day_others_cost + r.cost_yuan * c.people / 2.0 > limit + 1e-6:
            continue
        return r
    return None


def swap_dish(plans: list[DayPlan], day_no: int, old_id: str, db: RecipeDB,
              c: UserConstraints) -> tuple[list[DayPlan], Recipe | None]:
    """只替换某天的一道菜，其余各天原样保留（点「换一道」或「不喜欢」时使用）。"""
    rep = pick_replacement(plans, day_no, old_id, db, c)
    if rep is None:
        return [p.model_copy(deep=True) for p in plans], None
    new_plans: list[DayPlan] = []
    for p in plans:
        if p.day != day_no:
            new_plans.append(p.model_copy(deep=True))
            continue
        dishes = [
            ChosenDish(recipe_id=rep.id, reason=make_reason(rep, c)) if d.recipe_id == old_id else d
            for d in p.dishes
        ]
        new_plans.append(DayPlan(day=p.day, meal=p.meal, dishes=dishes))
    return new_plans, rep


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
                   c: UserConstraints) -> list[DayPlan]:
    """把某天的一道菜换成指定菜，其余各天原样保留。"""
    out: list[DayPlan] = []
    for p in plans:
        if p.day != day_no:
            out.append(p.model_copy(deep=True))
            continue
        dishes = [
            ChosenDish(recipe_id=new_recipe.id, reason=make_reason(new_recipe, c))
            if d.recipe_id == old_id else d
            for d in p.dishes
        ]
        out.append(DayPlan(day=p.day, meal=p.meal, dishes=dishes, skipped=p.skipped))
    return out


def candidates_matching(plans: list[DayPlan], day_no: int, keyword: str, db: RecipeDB,
                        c: UserConstraints, replace_id: str | None = None) -> list[Recipe]:
    """在「满足硬约束 + 全周不重复 + 不超当天预算」的前提下，找匹配关键词的候选菜。"""
    allowed = {r.id for r in recipes_matching(db, keyword)}
    if not allowed:
        return []
    used = {d.recipe_id for p in plans for d in p.dishes if d.recipe_id != replace_id}
    day = next((p for p in plans if p.day == day_no), None)
    others_cost = 0.0
    if day is not None:
        for d in day.dishes:
            if d.recipe_id == replace_id:
                continue
            r = db.by_id(d.recipe_id)
            if r:
                others_cost += r.cost_yuan * c.people / 2.0
    limit = c.budget_per_person_day * c.people if c.budget_per_person_day is not None else None
    out: list[Recipe] = []
    for r in retrieve_candidates(db, c):        # 已按软偏好（含"喜欢"加权）排序
        if r.id in used or r.id not in allowed:
            continue
        if limit is not None and others_cost + r.cost_yuan * c.people / 2.0 > limit + 1e-6:
            continue
        out.append(r)
    return out


def skip_day(plans: list[DayPlan], day_no: int) -> list[DayPlan]:
    """「这天不做饭」：清空当天菜品（清单与花费自然跟着变）。"""
    out: list[DayPlan] = []
    for p in plans:
        if p.day == day_no:
            out.append(DayPlan(day=p.day, meal=p.meal, dishes=[], skipped=True))
        else:
            out.append(p.model_copy(deep=True))
    return out


def restore_day(plans: list[DayPlan], day_no: int, db: RecipeDB,
                c: UserConstraints) -> list[DayPlan]:
    """把「这天不做饭」改回来：重新给这天挑菜，不与其他天重复、不超当天预算。"""
    used = {d.recipe_id for p in plans for d in p.dishes}
    limit = c.budget_per_person_day * c.people if c.budget_per_person_day is not None else None
    pool = [r for r in retrieve_candidates(db, c) if r.id not in used]
    picked: list[ChosenDish] = []
    spent = 0.0
    need_protein = True
    while len(picked) < c.dishes_per_day and pool:
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
    out: list[DayPlan] = []
    for p in plans:
        if p.day == day_no:
            out.append(DayPlan(day=p.day, meal=p.meal, dishes=picked, skipped=False))
        else:
            out.append(p.model_copy(deep=True))
    return out


def _swap_by_rule(plans: list[DayPlan], db: RecipeDB, c: UserConstraints,
                  chooser) -> tuple[list[DayPlan], int, Recipe, Recipe] | None:
    """通用：按 chooser(当天菜) 决定要换掉哪道，再找替换菜。"""
    used = {d.recipe_id for p in plans for d in p.dishes}
    for p in plans:
        for d in p.dishes:
            old = db.by_id(d.recipe_id)
            if old is None:
                continue
            kind = chooser(p, old)
            if kind is None:
                continue
            for cand in retrieve_candidates(db, c):
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
                return (replace_in_day(plans, p.day, d.recipe_id, cand, c), p.day, old, cand)
    return None


def _fits_day_budget(plans: list[DayPlan], day_no: int, replace_id: str, cand: Recipe,
                     db: RecipeDB, c: UserConstraints) -> bool:
    if c.budget_per_person_day is None:
        return True
    day = next((p for p in plans if p.day == day_no), None)
    if day is None:
        return True
    total = 0.0
    for d in day.dishes:
        if d.recipe_id == replace_id:
            continue
        r = db.by_id(d.recipe_id)
        if r:
            total += r.cost_yuan * c.people / 2.0
    return total + cand.cost_yuan * c.people / 2.0 <= c.budget_per_person_day * c.people + 1e-6


def cheapest_swap(plans: list[DayPlan], db: RecipeDB,
                  c: UserConstraints) -> tuple[list[DayPlan], int, Recipe, Recipe, float] | None:
    """E-05：把最贵的一道换成更便宜的一道，并算出省了多少钱。"""
    best = None
    for p in plans:
        for d in p.dishes:
            r = db.by_id(d.recipe_id)
            if r is not None and (best is None or r.cost_yuan > best[1].cost_yuan):
                best = (d, r)
    if best is None or best[1].cost_yuan <= 0:
        return None
    d, old = best
    day_no = next((p.day for p in plans if any(x.recipe_id == old.id for x in p.dishes)), 1)
    used = {x.recipe_id for pp in plans for x in pp.dishes}
    pool = [r for r in retrieve_candidates(db, c)
            if r.id not in used and r.cost_yuan < old.cost_yuan * 0.7
            and _fits_day_budget(plans, day_no, old.id, r, db, c)]
    if not pool:
        return None
    pool.sort(key=lambda r: (r.cost_yuan, -recipe_score(r, c)))
    new_recipe = pool[0]
    saving = (old.cost_yuan - new_recipe.cost_yuan) * c.people / 2.0
    return (replace_in_day(plans, day_no, old.id, new_recipe, c), day_no, old, new_recipe, saving)


def best_day_no(plans: list[DayPlan], recipe: Recipe) -> int:
    for p in plans:
        if any(d.recipe_id == recipe.id for d in p.dishes):
            return p.day
    return 1


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
                c: UserConstraints) -> tuple[list[DayPlan], list[Recipe]] | None:
    """「回家晚了」：只把这一天换成最快能做完的组合（05 M1）。"""
    day = next((p for p in plans if p.day == day_no), None)
    if day is None:
        return None
    used = {d.recipe_id for p in plans for d in p.dishes if p.day != day_no}
    pool = [r for r in retrieve_candidates(db, c) if r.id not in used]
    pool.sort(key=lambda r: (r.time_min, -(recipe_score(r, c))))
    picked: list[Recipe] = []
    spent = 0.0
    limit = c.budget_per_person_day * (day.people or c.people) if c.budget_per_person_day else None
    for r in pool:
        if len(picked) >= c.dishes_per_day:
            break
        if limit is not None and spent + r.cost_yuan * (day.people or c.people) / 2.0 > limit + 1e-6:
            continue
        picked.append(r)
        spent += r.cost_yuan * (day.people or c.people) / 2.0
    if not picked:
        return None
    if day.dishes and max(r.time_min for r in picked) >= max(
            (db.by_id(d.recipe_id).time_min for d in day.dishes if db.by_id(d.recipe_id)), default=999):
        return None       # 换不更快就没必要换
    out: list[DayPlan] = []
    for p in plans:
        if p.day == day_no:
            out.append(DayPlan(day=p.day, meal=p.meal, skipped=False, people=p.people,
                               dishes=[ChosenDish(recipe_id=r.id, reason=f"快手：约 {r.time_min} 分钟就能上桌。")
                                       for r in picked]))
        else:
            out.append(p.model_copy(deep=True))
    return out, picked
