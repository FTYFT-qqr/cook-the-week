"""M-01 本地周报口径测试：只读真实形状，不伪造趋势结论。"""
from __future__ import annotations

from datetime import date

from recipe_planner.models import ChosenDish, DayPlan, PlanRecord, PlanResult, UserConstraints
from recipe_planner.usage_report import build_report, render_markdown


def _record(record_id: str, created_at: str, week: str, first: str, second: str,
            done_slots: list[str] | None = None) -> PlanRecord:
    result = PlanResult(
        constraints=UserConstraints(days=2, dishes_per_day=1),
        candidate_count=2,
        days=[
            DayPlan(day=1, dishes=[ChosenDish(recipe_id=first)]),
            DayPlan(day=2, dishes=[ChosenDish(recipe_id=second)]),
        ],
    )
    return PlanRecord(id=record_id, created_at=created_at, start_date=week,
                      done_slots=done_slots or [], result=result)


def test_m01_统计事件并在样本不足时不输出趋势结论():
    records = [
        _record("p1", "2026-08-03 09:00", "2026-08-03", "r101", "r102",
                ["1|晚餐"]),
        _record("p2", "2026-08-10 09:00", "2026-08-10", "r102", "r103",
                ["1|晚餐", "2|晚餐"]),
    ]
    events = [
        {"plan_id": "p1", "recipe_id": "r101", "action": "done",
         "day_no": 1, "meal": "晚餐", "created_at": "2026-08-03T19:00:00"},
        {"plan_id": "p1", "recipe_id": "r102", "action": "swap_out",
         "day_no": 2, "meal": "晚餐", "created_at": "2026-08-04T19:00:00"},
        {"plan_id": "p2", "recipe_id": "r102", "action": "done",
         "day_no": 1, "meal": "晚餐", "created_at": "2026-08-10T19:00:00"},
        {"plan_id": "p2", "recipe_id": "r103", "action": "done",
         "day_no": 2, "meal": "晚餐", "created_at": "2026-08-11T19:00:00"},
        {"recipe_id": "r103", "action": "rate_good", "created_at": "2026-08-11T20:00:00"},
        {"recipe_id": "r102", "action": "rate_ok", "created_at": "2026-08-11T20:00:00"},
        {"recipe_id": "r101", "action": "snooze", "created_at": "2026-08-12T20:00:00"},
        {"recipe_id": "r104", "action": "dislike", "created_at": "2026-08-12T20:00:00"},
    ]
    report = build_report(records=records, event_rows=events,
                          today=date(2026, 9, 17), weeks=8)
    metrics = report["metrics"]

    assert report["sample"]["status"] == "样本不足"
    assert report["sample"]["enough_for_trend"] is False
    assert metrics["planned_slots"] == 4
    assert metrics["completed_slots"] == 3
    assert metrics["weekly_swaps"] == 1
    assert metrics["temporary_snoozes"] == 1
    assert metrics["permanent_dislikes"] == 1
    assert metrics["ratings"] == {"好吃": 1, "一般": 1, "下次不做": 0}
    assert metrics["direct_acceptance_rate"] == 0.75
    assert metrics["consecutive_week_overlap_rate"] == 0.3333
    assert "不输出趋势结论" in render_markdown(report)


def test_m01_无数据时仍生成诚实的空报():
    report = build_report(records=[], event_rows=[], today=date(2026, 9, 17), weeks=8)
    assert report["sample"]["status"] == "样本不足"
    assert report["metrics"]["completion_rate"] is None
    assert report["metrics"]["consecutive_week_overlap_rate"] is None
    assert "样本不足" in render_markdown(report)
