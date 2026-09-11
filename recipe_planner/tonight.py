"""「今晚」这一页该显示什么：状态①–⑤ 的判定 + 文案（纯函数，不碰 Streamlit、不写库）。

原来这段逻辑长在 `app.py: render_tonight()` 里，和渲染混在一起。服务化之后
「空/加载/失败/成功」的措辞必须由服务端产出（docs/08 §5），所以抽到这里：
界面与 API 共用同一套判定与文案，`today`/`now` 可注入，测试不用等到半夜才验「22:00 切明天」。

五种状态（docs/05 M1）：
- `no_plan`   还没有这一周的菜单
- `week_over` 这一周已经吃完了
- `skipped`   今天标记了不做饭
- `done`      今天已经做过了（可以打分）
- `planned`   今天有安排（主角卡）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from recipe_planner import reporting as rep
from recipe_planner import store
from recipe_planner.models import PlanRecord, RecipeDB

STATES = ("no_plan", "week_over", "skipped", "done", "planned")


@dataclass
class TonightDish:
    recipe_id: str
    name: str
    time_min: int
    difficulty: str
    reason: str = ""


@dataclass
class TonightView:
    state: str
    kicker: str = ""            # 小标题（"今晚 · 周三 9/17（第 3 天）"）
    headline: str = ""          # 主角大字（菜名串，或"今晚不做饭"）
    meta: str = ""              # "约 45 分钟 · 预计 ¥62"
    reason: str = ""            # 一句推荐理由
    day: int = 0                # 正在看的第几天（1 起）
    weekday: str = ""
    date_label: str = ""
    week_label: str = ""
    dishes: list[TonightDish] = field(default_factory=list)
    minutes: int = 0
    cost: float = 0.0
    people: Optional[int] = None      # 「来客人了」当天的份量
    eat_eta: str = ""                  # "18:30 开始做，约 19:15 能吃上"
    hint: str = ""                     # "已经过了 22:00，下面先看明天"
    next_steps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "kicker": self.kicker,
            "headline": self.headline,
            "meta": self.meta,
            "reason": self.reason,
            "day": self.day,
            "weekday": self.weekday,
            "date_label": self.date_label,
            "week_label": self.week_label,
            "dishes": [d.__dict__ for d in self.dishes],
            "minutes": self.minutes,
            "cost": round(self.cost, 2),
            "people": self.people,
            "eat_eta": self.eat_eta,
            "hint": self.hint,
            "next_steps": self.next_steps,
        }


def _step(op: str, label: str, **extra: Any) -> dict:
    return {"op": op, "label": label, **extra}


def eat_eta(cook_start: str, minutes: int) -> str:
    """"18:30 开始做，约 19:15 能吃上"（B2 开饭倒推）；算不出来就返回空串。"""
    if not cook_start or minutes <= 0:
        return ""
    try:
        hh, mm = (int(x) for x in cook_start.replace("：", ":").split(":"))
        at = (datetime(2000, 1, 1, hh, mm) + timedelta(minutes=minutes)).strftime("%H:%M")
    except Exception:
        return ""
    return f"{cook_start} 开始做，约 {at} 能吃上"


def view_day_index(start_date: Any, days: int, today: Optional[date] = None,
                   now: Optional[datetime] = None) -> tuple[int, str]:
    """今晚看哪一天（1 起）+ 一句说明。过了 22:00 默认切到明天（05 M1 边界）。"""
    now = now or datetime.now()
    today = today or now.date()
    idx = store.today_index(start_date, days, today)
    if idx is not None:
        if now.hour >= 22 and idx + 1 < days:
            return idx + 2, f"已经过了 22:00，下面先看明天（第 {idx + 2} 天）"
        return idx + 1, ""
    if store.normalize_start(start_date) > today:
        return 1, "这一周还没开始，下面是第 1 天"
    return 1, "这一周已经过去，下面是第 1 天"


def tonight_view(record: Optional[PlanRecord], db: RecipeDB, today: Optional[date] = None,
                 now: Optional[datetime] = None) -> TonightView:
    """把一份方案存档翻译成「今晚」页要的东西。"""
    if record is None or not record.result.days:
        return TonightView(
            state="no_plan", kicker="还没有这周的菜单", headline="先花 20 秒排一周",
            meta="填几口人、忌口和预算，我会排出这一周并给出买菜清单。",
            next_steps=[_step("create_plan", "帮我排一周")])

    result = record.result
    c = result.constraints
    start = record.start_date
    label = store.week_label(start)
    summary = rep.plan_summary(result, db, start)
    day_no, hint = view_day_index(start, len(result.days), today=today, now=now)
    day_plan = next((p for p in result.days if p.day == day_no), result.days[0])
    row = next((r for r in summary.rows if r.day == day_no), summary.rows[0])
    done_days = set(record.done_days or [])

    base = TonightView(state="planned", day=row.day, weekday=row.weekday,
                       date_label=row.date_label, week_label=label, hint=hint,
                       minutes=row.minutes, cost=row.cost, people=day_plan.people)

    week_end = store.normalize_start(start) + timedelta(days=len(result.days) - 1)
    if week_end < (today or date.today()):
        base.state = "week_over"
        base.kicker = "这一周已经吃完了"
        base.headline = f"{label} 的菜单在这里"
        base.meta = "要不要照上周再来一份，或者重新排一周？"
        base.next_steps = [_step("reuse_previous", "照上周"),
                           _step("create_plan", "重新排"),
                           _step("view_week", "看这一周")]
        return base

    if day_plan.skipped:
        base.state = "skipped"
        base.kicker = f"第 {row.day} 天 {row.weekday} {row.date_label}"
        base.headline = "今晚不做饭"
        base.meta = "你标记过这天不做饭：不计花费，也不进买菜清单。"
        base.next_steps = [_step("restore_day", "改回来做"), _step("view_week", "看这一周")]
        return base

    dishes = [TonightDish(recipe_id=d.recipe_id, name=r.name, time_min=r.time_min,
                          difficulty=r.difficulty, reason=d.reason)
              for d in day_plan.dishes if (r := db.by_id(d.recipe_id))]
    base.dishes = dishes
    base.reason = day_plan.dishes[0].reason if day_plan.dishes else ""

    if day_no in done_days:
        base.state = "done"
        base.kicker = f"第 {row.day} 天 {row.weekday} {row.date_label}"
        base.headline = "、".join(row.dishes) or "（未排）"
        base.meta = "已经做过了。这几道怎么样？（说一句就够，下周会照你的口味排）"
        base.next_steps = [
            _step("rate", "好吃", score=2),
            _step("rate", "一般", score=1),
            _step("rate", "下次不做", score=0),
            _step("next_day", "看看明天"),
            _step("unmark_done", "再做一次"),
        ]
        return base

    base.kicker = f"今晚 · {row.weekday} {row.date_label}（第 {row.day} 天）"
    base.headline = "、".join(row.dishes) or "（未排）"
    minutes_txt = f"约 {row.minutes} 分钟 · 预计 ¥{row.cost:.0f}"
    if day_plan.people:
        minutes_txt += f" · 按 {day_plan.people} 人算"
    today_idx = store.today_index(start, len(result.days), today)
    if c.cook_start and (today_idx is None or day_no == today_idx + 1):
        eta = eat_eta(c.cook_start, row.minutes)
        if eta:
            minutes_txt += f" · {eta}"
            base.eat_eta = eta
    base.meta = minutes_txt
    base.next_steps = [
        _step("start_cooking", "开始做饭"),
        _step("faster", "回家晚了"),
        _step("guests", "来客人了"),
        _step("mark_done", "做完了"),
        _step("view_week", "看这一周"),
    ]
    return base
