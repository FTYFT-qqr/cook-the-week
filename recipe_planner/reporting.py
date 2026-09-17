"""把排菜结果翻译成「客户的话」。

界面上的字要回答的是：这周花多少钱、几点能吃上饭、有没有踩到忌口、几道是我爱吃的。
本模块负责这些摘要数字，以及清单的三种带得走的形式（复制文本 / CSV / 可打印视图）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from recipe_planner import store
from recipe_planner.models import MEAL, DayPlan, PlanResult, RecipeDB, ValidationIssue


# ---------------------------------------------------------------- 抬头文案

NUTRITION_GOALS = {"减脂", "控糖", "高蛋白"}


def nutrition_boundary_text(goal: str) -> str:
    """目标标签的用户可见边界说明，避免把方向性推荐说成营养结论。"""
    if goal in NUTRITION_GOALS:
        return "当前按菜谱标签与已录入数据做方向性推荐，不等于营养计算或专业建议。"
    return ""

def menu_title(c, single: str = "一周晚餐菜单") -> str:
    """整份菜单叫什么：只做晚餐时原样返回 `single`（三个出口的老写法不一样，各自保持不变）。

    多餐时这三个出口统一叫「本周菜单」—— 只做早餐却说"晚餐菜单"是错的。
    """
    return single if c.active_meals() == [MEAL] else "本周菜单"


def dishes_phrase(c) -> str:
    """"每顿几道菜"：单餐时保持原来的措辞；多餐时按餐分别说清（早 1 / 午 2 / 晚 3）。

    docs/10：多餐之后不能再拿 `dishes_per_day` 一句话概括 —— 那是**一天总数**的旧字段，
    早/午/晚各自的道数写在 `dishes_per_meal` 里。
    """
    meals = c.active_meals()
    if len(meals) == 1:
        return f"每顿 {c.dishes_for(meals[0])} 道菜"
    return "、".join(f"{m} {c.dishes_for(m)} 道" for m in meals)


def _meal_mark(c, meal: str) -> str:
    """打印/复制文本里要不要带上餐次：只做一顿就什么都不加。"""
    return f"（{meal}）" if c.is_multi_meal() else ""


# ---------------------------------------------------------------- 单天

def day_minutes(plan: DayPlan, db: RecipeDB) -> int:
    """这一天从第一道下锅到最后一道出锅的理论总时长（按顺序做）。"""
    return sum(r.time_min for d in plan.dishes if (r := db.by_id(d.recipe_id)))


def day_cost(plan: DayPlan, db: RecipeDB, people: int) -> float:
    p = plan.people or people        # 「来客人了」只改这一天的份量
    return sum(r.cost_yuan * p / 2.0 for d in plan.dishes if (r := db.by_id(d.recipe_id)))


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
    meal: str = MEAL          # docs/10：一行 = 一天里的一顿（只做晚餐时就是那一天）
    # 这一顿的菜谱 id（docs/12 阶段三）：分享文本要"带上做法"就得按 id 取 steps ——
    # `dishes` 是**菜名**，只够显示，不够回查。
    recipe_ids: list[str] = field(default_factory=list)


@dataclass
class PlanSummary:
    days: int                 # **天数**（不是顿数；docs/10）
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
    slots: int = 0            # 顿数（多餐时 > days）


def plan_summary(result: PlanResult, db: RecipeDB, start_date=None) -> PlanSummary:
    c = result.constraints
    liked_ids = set(c.liked_dishes)
    rows: list[DayRow] = []
    liked_hit = goal_hit = 0
    for p in result.days:
        names = day_dish_names(p, db)
        rows.append(DayRow(
            day=p.day,
            # 用**这一顿自己的天**取星期/日期，不能用 enumerate 序号：
            # 一天多顿时序号会走到第二天去（第 1 天的午餐被写成周二的日期）。
            weekday=store.weekday_name(start_date, p.day - 1),
            date_label=store.day_date_label(start_date, p.day - 1),
            dishes=names,
            minutes=day_minutes(p, db),
            cost=day_cost(p, db, c.people),
            meal=p.meal,
            recipe_ids=[d.recipe_id for d in p.dishes if not p.skipped],
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
        days=len({r.day for r in rows}),        # 天数（多餐时一天有好几行）
        slots=len(rows),
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
    lines = [f"买菜清单{f'（{label}）' if label else ''} · {c.people} 人 · {c.days} 天"]
    total = 0
    for row in shopping_rows(result, checked):
        total += 1
        mark = "[x]" if row["是否已买"] else "[ ]"
        use = f"  ← {row['用于']}" if row["用于"] else ""
        lines.append(f"{mark} {row['食材']}（{row['数量']}）{use}")
    lines.append(f"—— 共 {total} 项；卖场里买一样划一样 ——")
    have = [s.name for s in result.shopping if not s.needed]
    if have:
        lines.append("家里已有，无需购买：" + "、".join(have))
    return "\n".join(lines)


def printable_text(result: PlanResult, db: RecipeDB, start_date=None,
                   checked: set[str] | None = None) -> str:
    """可打印视图：一整周的菜单 + 清单，贴冰箱用（浏览器 Ctrl+P 打印 / 另存 PDF）。"""
    summary = plan_summary(result, db, start_date)
    c = result.constraints
    label = store.week_label(start_date)
    lines = [f"{menu_title(c)}（{label}）　{c.people} 人 · {dishes_phrase(c)}", ""]
    for row in summary.rows:
        dishes = "、".join(row.dishes) or "（未排）"
        lines.append(f"第 {row.day} 天 {row.weekday}{_meal_mark(c, row.meal)} "
                     f"{row.date_label}：{dishes}")
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


BATCH_LATER = {"蔬菜", "菌菇", "水产", "肉蛋"}   # 周中再买更新鲜（F1 采购拆批）


def split_batches(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """把清单拆两批：第一批耐放的（主食/干货/豆制品等），第二批易坏的（青菜/菌菇/肉/水产）。"""
    first = [r for r in rows if r["分类"] not in BATCH_LATER]
    second = [r for r in rows if r["分类"] in BATCH_LATER]
    return first, second


def optional_hint(row: dict) -> str:
    """只在一道菜里用到的小料 → 提示「可以不买」（F1 可选食材标注）。"""
    uses = [x for x in (row.get("用于") or "").split("、") if x]
    return "可选" if len(uses) == 1 else ""


def share_text(result: PlanResult, db: RecipeDB, start_date=None,
               with_steps: bool = False) -> str:
    """E-08：分享给家人的干净视图 —— 只有日期、菜名、时间、金额，没有按钮、没有技术字样。

    `with_steps=True`（docs/12 阶段三 3.3）：在每道菜后面补上做法步骤 ——
    这份文本本来就是"发给做饭的人"的，对方照着做时缺的恰好是步骤。
    默认关：不勾选时仍然是**干净版**（截图发微信不该被一屏步骤淹没）。
    """
    summary = plan_summary(result, db, start_date)
    c = result.constraints
    lines = [f"这一周的{'晚饭' if not c.is_multi_meal() else '饭'}"
             f"（{store.week_label(start_date)}）", ""]
    for row in summary.rows:
        when = f"{row.weekday} {row.date_label}{_meal_mark(c, row.meal)}"
        if not row.dishes:
            lines.append(f"{when}　这天不做饭")
            continue
        lines.append(f"{when}　{'、'.join(row.dishes)}"
                     f"　（约 {row.minutes} 分钟 · ¥{row.cost:.0f}）")
        if with_steps:
            for rid in row.recipe_ids:
                r = db.by_id(rid)
                if r is None or not r.steps:
                    continue
                lines.append(f"　　【{r.name}】")
                lines += [f"　　{i}. {s}" for i, s in enumerate(r.steps, start=1)]
    lines += ["", "本周预计 ¥%.0f" % summary.total_cost
              + (" / 预算 ¥%.0f" % summary.budget_total if summary.budget_total else "")]
    return "\n".join(lines)


def structure_line(result: PlanResult, db: RecipeDB) -> str:
    """E-06：一句话说清这一周的结构（几荤几素几汤），不用客户自己数。"""
    meat = veg = soup = 0
    for p in result.days:
        for d in p.dishes:
            r = db.by_id(d.recipe_id)
            if r is None:
                continue
            if r.category == "汤":
                soup += 1
            elif r.category in {"肉蛋", "水产", "豆制品"} or any(
                i.category in {"肉蛋", "水产", "豆制品"} for i in r.ingredients
            ):
                meat += 1
            else:
                veg += 1
    parts = []
    if meat:
        parts.append(f"{meat} 道荤")
    if veg:
        parts.append(f"{veg} 道素")
    if soup:
        parts.append(f"{soup} 道汤")
    return "、".join(parts) if parts else "—"


PRINT_CSS = """
:root{ --line:#ECE7DF; }
*{ box-sizing:border-box; }
body{ margin:0; padding:18px; background:#FAF8F5; color:#111;
      font-family:"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif; }
.a4{ background:#fff; color:#111; border:1px solid var(--line); border-radius:12px;
     padding:22px 24px; font-size:12pt; line-height:1.5; }
.a4-title{ font-size:16pt; font-weight:700; }
.a4-sub{ font-size:10pt; color:#555; margin:2px 0 10px; }
.a4-title2{ font-size:13pt; font-weight:700; margin:14px 0 6px; }
.a4-table{ width:100%; border-collapse:collapse; font-size:10.5pt; }
.a4-table th,.a4-table td{ border:1px solid #999; padding:5px 7px; text-align:left; }
.a4-table th{ background:#F2F2F2; font-weight:700; }
.a4-n{ text-align:right; white-space:nowrap; }
.a4-cols{ columns:2; column-gap:26px; font-size:10.5pt; }
.a4-item{ margin-bottom:5px; break-inside:avoid; }
.a4-amt{ color:#555; margin-left:6px; font-size:9.5pt; }
.a4-foot{ margin-top:12px; padding-top:6px; border-top:1px solid #999; font-size:9.5pt; color:#444; }
@media print{
  /* 打印时只留 A4 那一块（版式与界面里的「带走清单 → A4 打印」一致，06 §V-12） */
  body{ padding:0; background:#fff; }
  body *{ visibility:hidden !important; }
  .a4, .a4 *{ visibility:visible !important; }
  .a4{ position:absolute; left:0; top:0; width:100%; border:0; padding:0; border-radius:0; }
  @page{ size:A4; margin:14mm; }
}
"""


def printable_document(result: PlanResult, db: RecipeDB, start_date=None,
                       checked: set[str] | None = None) -> str:
    """**可独立打开的** A4 单页（导出用）。

    `printable_html()` 返回的只是一个 `<div class='a4'>` 片段 —— 它的样式在 Streamlit 页面里。
    导出成一个文件就必须自己带上样式，否则打开是一堆没有样式的字。
    样式与 `app.py` 的 `.a4-*` 保持一致（同一份版式，两个出口）。
    """
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{menu_title(result.constraints)} · {store.week_label(start_date)}</title>"
        f"<style>{PRINT_CSS}</style></head><body>"
        f"{printable_html(result, db, start_date, checked)}"
        "</body></html>"
    )


def printable_html(result: PlanResult, db: RecipeDB, start_date=None,
                   checked: set[str] | None = None) -> str:
    """A4 单页打印版式（第二篇 4.9 / V-12）：上半周菜单表格，下半两栏带方框清单。

    黑白友好：不靠颜色，靠边框与方框。样式类（.a4-*）定义在 app.py 的样式表里。
    """
    summary = plan_summary(result, db, start_date)
    c = result.constraints
    label = store.week_label(start_date)
    # 多餐时每天有好几行，只在"周几"后面标出是哪一顿（打印是黑白的，不加新样式）
    rows = "".join(
        f"<tr><td>{r.weekday}{_meal_mark(c, r.meal)}</td><td>{r.date_label}</td>"
        f"<td>{'、'.join(r.dishes) or '—'}</td>"
        f"<td class='a4-n'>{r.minutes} 分钟</td><td class='a4-n'>¥{r.cost:.0f}</td></tr>"
        for r in summary.rows
    )
    items = shopping_rows(result, checked)
    boxes = "".join(
        f"<div class='a4-item'>{'☑' if row['是否已买'] else '□'} {row['食材']}"
        f"<span class='a4-amt'>{row['数量']}</span></div>"
        for row in items
    ) or "<div class='a4-item'>（这一周无需采购）</div>"
    budget = f"预算 ¥{summary.budget_total:.0f}" if summary.budget_total else "未设预算"
    return (
        "<div class='a4'>"
        "<div class='a4-title'>" + menu_title(c, single="本周晚餐菜单") + "</div>"
        f"<div class='a4-sub'>{label}　{c.people} 人　{dishes_phrase(c)}　"
        f"共 {summary.dishes} 道</div>"
        "<table class='a4-table'>"
        "<thead><tr><th>周几</th><th>日期</th><th>菜名</th><th>用时</th><th>金额</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "<tfoot><tr><td colspan='4'>合计</td>"
        f"<td class='a4-n'>¥{summary.total_cost:.0f}</td></tr></tfoot>"
        "</table>"
        "<div class='a4-title2'>买菜清单（买一样划一样）</div>"
        f"<div class='a4-cols'>{boxes}</div>"
        f"<div class='a4-foot'>本周预计 ¥{summary.total_cost:.0f}　{budget}　·　"
        "按菜谱 2 人份单价折算，实际以当地物价为准</div>"
        "</div>"
    )
