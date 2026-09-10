"""把排菜结果翻译成「客户的话」。

界面上的字要回答的是：这周花多少钱、几点能吃上饭、有没有踩到忌口、几道是我爱吃的。
本模块负责这些摘要数字，以及清单的三种带得走的形式（复制文本 / CSV / 可打印视图）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from recipe_planner import store
from recipe_planner.models import DayPlan, PlanResult, RecipeDB, ValidationIssue


# ---------------------------------------------------------------- 单天

def day_minutes(plan: DayPlan, db: RecipeDB) -> int:
    """这一天从第一道下锅到最后一道出锅的理论总时长（按顺序做）。"""
    return sum(r.time_min for d in plan.dishes if (r := db.by_id(d.recipe_id)))


def day_cost(plan: DayPlan, db: RecipeDB, people: int) -> float:
    return sum(r.cost_yuan * people / 2.0 for d in plan.dishes if (r := db.by_id(d.recipe_id)))


def day_dish_names(plan: DayPlan, db: RecipeDB) -> list[str]:
    return [r.name for d in plan.dishes if (r := db.by_id(d.recipe_id))]


SLOW_CATEGORIES = {"汤"}
SLOW_MINUTES = 45


def _is_slow(r) -> bool:
    return r.category in SLOW_CATEGORIES or r.time_min >= SLOW_MINUTES


def _is_cold(r) -> bool:
    return r.category == "凉菜"


def cook_order(plan: DayPlan, db: RecipeDB) -> tuple[list[str], bool]:
    """轻量下锅顺序建议（只用菜谱已有的耗时/类别字段，不需要"做法步骤"数据）。

    规则：费火的汤/炖菜先上火 → 主菜 → 快炒 → 凉菜最后拌。
    返回 (顺序说明列表, 是否有可并行的汤/炖菜)。
    """
    recipes = [r for d in plan.dishes if (r := db.by_id(d.recipe_id))]
    if not recipes:
        return [], False
    slow = sorted([r for r in recipes if _is_slow(r)], key=lambda r: -r.time_min)
    cold = [r for r in recipes if not _is_slow(r) and _is_cold(r)]
    hot = sorted([r for r in recipes if not _is_slow(r) and not _is_cold(r)],
                 key=lambda r: -r.time_min)
    ordered = slow + hot + cold

    notes: list[str] = []
    for i, r in enumerate(ordered):
        mark = "①②③④⑤⑥⑦⑧⑨"[i] if i < 9 else f"{i + 1}."
        if r in slow:
            notes.append(f"{mark} 先上火：{r.name}（{r.time_min} 分钟，炖着的同时备其他菜）")
        elif r in cold:
            notes.append(f"{mark} 最后拌：{r.name}（{r.time_min} 分钟，上桌前再拌更爽口）")
        elif i == len(ordered) - 1:
            notes.append(f"{mark} 最后快炒：{r.name}（{r.time_min} 分钟）")
        else:
            notes.append(f"{mark} 接着做：{r.name}（{r.time_min} 分钟）")
    return notes, bool(slow)


# ---------------------------------------------------------------- 概览摘要

@dataclass
class DayRow:
    day: int
    weekday: str
    date_label: str
    dishes: list[str]
    minutes: int
    cost: float


@dataclass
class PlanSummary:
    days: int
    dishes: int
    total_cost: float
    budget_total: float | None
    over_budget: float
    liked_hit: int
    goal: str
    goal_hit: int
    hardest_day: int
    hardest_minutes: int
    hard_issues: list[ValidationIssue] = field(default_factory=list)
    allergen_issues: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    rows: list[DayRow] = field(default_factory=list)


def plan_summary(result: PlanResult, db: RecipeDB, start_date=None) -> PlanSummary:
    c = result.constraints
    liked_ids = set(c.liked_dishes)
    rows: list[DayRow] = []
    liked_hit = goal_hit = 0
    for idx, p in enumerate(result.days):
        names = day_dish_names(p, db)
        rows.append(DayRow(
            day=p.day,
            weekday=store.weekday_name(start_date, idx),
            date_label=store.day_date_label(start_date, idx),
            dishes=names,
            minutes=day_minutes(p, db),
            cost=day_cost(p, db, c.people),
        ))
        for d in p.dishes:
            if d.recipe_id in liked_ids:
                liked_hit += 1
            r = db.by_id(d.recipe_id)
            if r and c.goal != "随便" and c.goal in r.goal_tags:
                goal_hit += 1
    total = sum(r.cost for r in rows)
    budget_total = c.budget_per_person_day * c.people * c.days if c.budget_per_person_day else None
    hardest = max(rows, key=lambda r: r.minutes) if rows else None
    hard = [i for i in result.issues if i.level == "error"]
    return PlanSummary(
        days=len(result.days),
        dishes=sum(len(r.dishes) for r in rows),
        total_cost=round(total, 2),
        budget_total=round(budget_total, 2) if budget_total else None,
        over_budget=round(max(0.0, total - budget_total), 2) if budget_total else 0.0,
        liked_hit=liked_hit,
        goal=c.goal,
        goal_hit=goal_hit,
        hardest_day=hardest.day if hardest else 0,
        hardest_minutes=hardest.minutes if hardest else 0,
        hard_issues=hard,
        allergen_issues=[i for i in hard if i.code == "allergen"],
        warnings=[i for i in result.issues if i.level == "warning"],
        rows=rows,
    )


# ---------------------------------------------------------------- 清单导出

CSV_FIELDS = ["分类", "食材", "数量", "是否已买", "用于"]


def shopping_rows(result: PlanResult, checked: set[str] | None = None) -> list[dict]:
    checked = checked or set()
    rows = []
    for it in result.shopping:
        if not it.needed:
            continue  # 家里已有库存的不进采购清单
        rows.append({
            "分类": it.category,
            "食材": it.name,
            "数量": it.amount,
            "是否已买": "✅ 已买" if it.name in checked else "",
            "用于": "、".join(it.for_recipes),
        })
    return rows


def shopping_csv(result: PlanResult, checked: set[str] | None = None) -> str:
    """导出 CSV；带 UTF-8 BOM，Excel 直接打开不乱码。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(shopping_rows(result, checked))
    return "\ufeff" + buf.getvalue()


def shopping_text(result: PlanResult, checked: set[str] | None = None,
                  label: str = "") -> str:
    """纯文本清单：可以直接复制到微信 / 备忘录。"""
    c = result.constraints
    checked = checked or set()
    lines = [f"🛒 买菜清单{f'（{label}）' if label else ''} · {c.people} 人 · {c.days} 天"]
    total = 0
    for row in shopping_rows(result, checked):
        total += 1
        mark = "[x]" if row["是否已买"] else "[ ]"
        use = f"  ← {row['用于']}" if row["用于"] else ""
        lines.append(f"{mark} {row['食材']}（{row['数量']}）{use}")
    lines.append(f"—— 共 {total} 项；卖场里买一样划一样 ——")
    have = [s.name for s in result.shopping if not s.needed]
    if have:
        lines.append("🏠 家里已有，无需购买：" + "、".join(have))
    return "\n".join(lines)


def printable_text(result: PlanResult, db: RecipeDB, start_date=None,
                   checked: set[str] | None = None) -> str:
    """可打印视图：一整周的菜单 + 清单，贴冰箱用（浏览器 Ctrl+P 打印 / 另存 PDF）。"""
    summary = plan_summary(result, db, start_date)
    c = result.constraints
    label = store.week_label(start_date)
    lines = [f"一周晚餐菜单（{label}）　{c.people} 人 · 每顿 {c.dishes_per_day} 道菜", ""]
    for row in summary.rows:
        dishes = "、".join(row.dishes) or "（未排）"
        lines.append(f"第 {row.day} 天 {row.weekday} {row.date_label}：{dishes}")
        lines.append(f"    合计约 {row.minutes} 分钟 · 约 ¥{row.cost:.0f}")
    lines += [
        "",
        f"本周预计花费：约 ¥{summary.total_cost:.0f}"
        + (f" / 预算 ¥{summary.budget_total:.0f}" if summary.budget_total else "")
        + "（按菜谱 2 人份单价折算，实际以当地物价为准）",
        "",
        "—" * 18,
        shopping_text(result, checked, label),
    ]
    return "\n".join(lines)
