"""「今晚」状态机的单元测试（docs/05 M1 五种状态）。

这段逻辑原来长在 `app.py` 里，P1 抽到 `recipe_planner/tonight.py` 供界面与 API 共用。
纯函数、不碰数据库也不碰 Streamlit，所以可以精确地把"今天"注入进去（不用等到半夜）。
"""
from __future__ import annotations

from datetime import date, datetime

from recipe_planner.models import (ChosenDish, DayPlan, PlanRecord, PlanResult, Recipe,
                                   RecipeDB, UserConstraints)
from recipe_planner.tonight import eat_eta, tonight_view, view_day_index

RECIPES = [
    Recipe(id="a", name="番茄炒蛋", category="热菜", difficulty="简单", time_min=15, cost_yuan=8.0),
    Recipe(id="b", name="清炒时蔬", category="热菜", difficulty="简单", time_min=10, cost_yuan=6.0),
]
DB = RecipeDB(recipes=RECIPES)
START = "2026-09-14"          # 周一


def make_record(*, days: int = 3, done: list[int] | None = None, skipped: list[int] | None = None,
                people: int | None = None, cook_start: str = "18:30",
                start: str = START) -> PlanRecord:
    skipped = skipped or []
    day_plans = [
        DayPlan(day=d, skipped=d in skipped, people=people if d == 1 else None,
                dishes=[] if d in skipped else [ChosenDish(recipe_id="a", reason="快手又下饭"),
                                                ChosenDish(recipe_id="b", reason="清爽解腻")])
        for d in range(1, days + 1)
    ]
    return PlanRecord(
        id="p_test", created_at="2026-09-13 20:00", start_date=start, label="9/14–9/20",
        change_note="", done_days=list(done or []),
        result=PlanResult(constraints=UserConstraints(people=2, days=days, dishes_per_day=2,
                                                     cook_start=cook_start),
                          candidate_count=2, days=day_plans))


def test_no_plan_state():
    view = tonight_view(None, DB, today=date(2026, 9, 16))
    assert view.state == "no_plan"
    assert view.dishes == [] and view.headline
    assert [s["op"] for s in view.next_steps] == ["create_plan"]


def test_planned_state_is_the_protagonist_card():
    view = tonight_view(make_record(), DB, today=date(2026, 9, 16), now=datetime(2026, 9, 16, 18, 0))
    assert view.state == "planned"
    assert view.day == 3 and view.weekday == "周三" and view.date_label == "9/16"
    assert view.headline == "番茄炒蛋、清炒时蔬"
    assert view.minutes == 25                       # 15 + 10
    assert view.cost == 14.0                        # 8 + 6（2 人份基准）
    assert "约 25 分钟" in view.meta and "¥14" in view.meta
    assert view.eat_eta == "18:30 开始做，约 18:55 能吃上"
    assert view.reason == "快手又下饭"
    assert {"start_cooking", "faster", "guests", "mark_done"} <= {s["op"] for s in view.next_steps}


def test_done_state_offers_rating():
    view = tonight_view(make_record(done=[3]), DB, today=date(2026, 9, 16),
                        now=datetime(2026, 9, 16, 20, 0))
    assert view.state == "done"
    assert "已经做过了" in view.meta
    ops = [s["op"] for s in view.next_steps]
    assert ops[:3] == ["rate", "rate", "rate"]
    scores = [s.get("score") for s in view.next_steps if s["op"] == "rate"]
    assert scores == [2, 1, 0]                       # 好吃 / 一般 / 下次不做
    assert "unmark_done" in ops


def test_skipped_state():
    view = tonight_view(make_record(skipped=[3]), DB, today=date(2026, 9, 16))
    assert view.state == "skipped"
    assert view.headline == "今晚不做饭"
    assert "不计花费" in view.meta
    assert view.next_steps[0] == {"op": "restore_day", "label": "改回来做"}


def test_week_over_state_offers_reuse():
    view = tonight_view(make_record(), DB, today=date(2026, 9, 30))
    assert view.state == "week_over"
    assert "这一周已经吃完了" in view.kicker
    ops = [s["op"] for s in view.next_steps]
    assert ops[0] == "reuse_previous" and "create_plan" in ops


def test_future_week_hints_that_it_has_not_started():
    view = tonight_view(make_record(), DB, today=date(2026, 9, 10))
    assert view.state == "planned"
    assert view.day == 1 and "还没开始" in view.hint


def test_after_22_switches_to_tomorrow():
    # 周二 22:30 → 先看周三（第 3 天）
    view = tonight_view(make_record(), DB, today=date(2026, 9, 15),
                        now=datetime(2026, 9, 15, 22, 30))
    assert view.day == 3
    assert "已经过了 22:00" in view.hint


def test_after_22_on_last_day_stays():
    # 周三已经是最后一天，后面没有"明天"可切，就还看今天（不能越界）
    view = tonight_view(make_record(days=3), DB, today=date(2026, 9, 16),
                        now=datetime(2026, 9, 16, 23, 0))
    assert view.day == 3
    assert view.hint == ""


def test_eat_eta_formats_and_tolerates_bad_input():
    assert eat_eta("18:30", 45) == "18:30 开始做，约 19:15 能吃上"
    assert eat_eta("18：30", 45) == "18：30 开始做，约 19:15 能吃上"     # 全角冒号也能认
    assert eat_eta("", 45) == ""
    assert eat_eta("很晚", 45) == ""
    assert eat_eta("18:30", 0) == ""


def test_view_day_index_without_range():
    assert view_day_index(START, 3, today=date(2026, 9, 1))[0] == 1
    assert "还没开始" in view_day_index(START, 3, today=date(2026, 9, 1))[1]
    assert "已经过去" in view_day_index(START, 3, today=date(2026, 10, 1))[1]


# ---------------------------------------------------------------- 手动切到第 N 天

def test_manual_day_switches_which_day_we_look_at():
    """界面"手动切到第 2 天"：其余判定一行不变，只是看的那天换成第 2 天。"""
    view = tonight_view(make_record(), DB, today=date(2026, 9, 14),
                        now=datetime(2026, 9, 14, 18, 0), day=2)
    assert view.day == 2                                  # 自动判定本来会是第 1 天
    assert view.hint == "你手动切到了第 2 天"
    assert view.state == "planned"
    assert view.weekday == "周二" and view.date_label == "9/15"
    assert view.headline == "番茄炒蛋、清炒时蔬"
    assert view.kicker == "今晚 · 周二 9/15（第 2 天）"
    assert "约 25 分钟" in view.meta and "¥14" in view.meta
    assert {"start_cooking", "faster", "guests", "mark_done"} <= {s["op"] for s in view.next_steps}
    # 没说切天时，仍是原来的自动判定
    auto = tonight_view(make_record(), DB, today=date(2026, 9, 14),
                        now=datetime(2026, 9, 14, 18, 0))
    assert auto.day == 1 and auto.hint == ""


def test_manual_day_ignores_numbers_outside_the_plan():
    """方案里没有这一天就忽略它，仍按自动判定（不能越界指到不存在的一天）。"""
    view = tonight_view(make_record(days=3), DB, today=date(2026, 9, 14), day=5)
    assert view.day == 1 and view.hint == ""
    assert view.state == "planned" and view.headline == "番茄炒蛋、清炒时蔬"


def test_manual_day_keeps_the_other_states_working():
    """切到"已经做过/不做饭"的那天，状态照样是 done / skipped（判定顺序没变）。"""
    done = tonight_view(make_record(done=[2]), DB, today=date(2026, 9, 14), day=2)
    assert done.state == "done" and done.day == 2
    assert "已经做过了" in done.meta

    skipped = tonight_view(make_record(skipped=[2]), DB, today=date(2026, 9, 14), day=2)
    assert skipped.state == "skipped" and skipped.headline == "今晚不做饭"
    assert skipped.hint == "你手动切到了第 2 天"
