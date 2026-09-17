"""本地真实使用周报（docs/15 M-01）。

只读取方案和菜品事件，不创建模拟用户、不修改真实档案；样本不足时只报告
可观测数量，不输出趋势结论。这个模块把统计口径做成纯函数，JSON/SQLite
后端只在调用方的读取边界上有差异。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from recipe_planner.models import PlanRecord

MIN_OBSERVED_WEEKS = 4
RATING_ACTIONS = {"rate_good": "好吃", "rate_ok": "一般", "rate_never": "下次不做"}


def _parse_datetime(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
    return None


def _parse_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _period(today: Optional[date], weeks: int) -> tuple[date, date]:
    end = today or date.today()
    return end - timedelta(weeks=max(1, weeks)), end


def _in_period(raw: Any, start: date, end: date) -> bool:
    when = _parse_datetime(raw)
    return bool(when and start <= when.date() <= end)


def _plan_when(record: PlanRecord) -> Optional[datetime]:
    return _parse_datetime(record.created_at) or _parse_datetime(record.start_date)


def _slot_key(day: int, meal: str) -> tuple[int, str]:
    return int(day), str(meal or "晚餐")


def _planned_slots(record: PlanRecord) -> list[tuple[int, str]]:
    return [_slot_key(p.day, p.meal) for p in record.result.days
            if not p.skipped and p.dishes]


def _recipe_ids(record: PlanRecord) -> list[str]:
    return [dish.recipe_id for p in record.result.days if not p.skipped for dish in p.dishes]


def _latest_by_week(records: Iterable[PlanRecord]) -> dict[str, PlanRecord]:
    latest: dict[str, PlanRecord] = {}
    for record in records:
        week = record.start_date or f"record:{record.id}"
        old = latest.get(week)
        if old is None or (_plan_when(record) or datetime.min) > (_plan_when(old) or datetime.min):
            latest[week] = record
    return latest


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _event_key(event: dict[str, Any]) -> Optional[tuple[int, str]]:
    if event.get("day_no") is None:
        return None
    try:
        return _slot_key(int(event["day_no"]), str(event.get("meal") or "晚餐"))
    except (TypeError, ValueError):
        return None


def _empty_week(week: str) -> dict[str, Any]:
    return {"week_start": week, "planned_slots": 0, "completed_slots": 0,
            "completion_rate": None, "swaps": 0, "duplicate_count": 0,
            "structure_warnings": 0}


def build_report(*, records: Optional[Iterable[PlanRecord]] = None,
                 event_rows: Optional[Iterable[dict[str, Any]]] = None,
                 today: Optional[date] = None, weeks: int = 8) -> dict[str, Any]:
    """从已有本地数据生成一份不带原始偏好内容的周报字典。"""
    if records is None:
        from recipe_planner import store

        records = store.load_records()
    if event_rows is None:
        from recipe_planner import events

        event_rows = events.load_events()
    start, end = _period(today, weeks)
    period_records = [r for r in records if _in_period(_plan_when(r), start, end)]
    latest = _latest_by_week(period_records)
    selected = list(latest.values())
    selected_ids = {record.id for record in selected}
    period_events = [event for event in event_rows
                     if _in_period(event.get("created_at"), start, end)]
    plan_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in period_events:
        plan_id = str(event.get("plan_id") or "")
        if plan_id in selected_ids:
            plan_events[plan_id].append(event)

    planned_slots = completed_slots = direct_accepted = observed_slots = 0
    unobserved_slots = 0
    weekly: list[dict[str, Any]] = []
    for record in sorted(selected, key=lambda item: item.start_date or item.id):
        slots = set(_planned_slots(record))
        slot_events = plan_events.get(record.id, [])
        changed = {_event_key(e) for e in slot_events if e.get("action") in {"swap_out", "skip"}}
        changed.discard(None)
        done_from_events = {_event_key(e) for e in slot_events if e.get("action") == "done"}
        done_from_events.discard(None)
        done = {key for key in slots if record.is_done(*key)} | (done_from_events & slots)
        direct = done - changed
        observed = (changed | done) & slots
        ids = _recipe_ids(record)
        duplicate_count = len(ids) - len(set(ids))
        structure_warnings = sum(
            issue.level == "warning" and issue.code in {"structure", "structure_shortage"}
            for issue in record.result.issues
        )
        planned_slots += len(slots)
        completed_slots += len(done)
        direct_accepted += len(direct)
        observed_slots += len(observed)
        unobserved_slots += len(slots - observed)
        weekly.append({
            **_empty_week(record.start_date or "未知周期"),
            "planned_slots": len(slots),
            "completed_slots": len(done),
            "completion_rate": _rate(len(done), len(slots)),
            "swaps": sum(e.get("action") == "swap_out" for e in slot_events),
            "duplicate_count": duplicate_count,
            "structure_warnings": structure_warnings,
        })

    actions = Counter(str(event.get("action") or "") for event in period_events)
    ratings = {label: actions[action] for action, label in RATING_ACTIONS.items()}
    week_starts = sorted(latest)
    overlap_values: list[float] = []
    for previous, current in zip(week_starts, week_starts[1:]):
        old_ids = set(_recipe_ids(latest[previous]))
        new_ids = set(_recipe_ids(latest[current]))
        union = old_ids | new_ids
        if union:
            overlap_values.append(round(len(old_ids & new_ids) / len(union), 4))

    sample_weeks = len(selected)
    enough = sample_weeks >= MIN_OBSERVED_WEEKS
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": start.isoformat(), "end": end.isoformat(), "weeks_requested": weeks},
        "sample": {
            "status": "可观察" if enough else "样本不足",
            "observed_plan_weeks": sample_weeks,
            "minimum_plan_weeks": MIN_OBSERVED_WEEKS,
            "enough_for_trend": enough,
            "message": ("已达到最少四周观察期，可继续积累后再判断趋势"
                         if enough else f"当前只有 {sample_weeks} 周，需要至少 {MIN_OBSERVED_WEEKS} 周；不输出趋势结论"),
        },
        "metrics": {
            "planned_slots": planned_slots,
            "completed_slots": completed_slots,
            "completion_rate": _rate(completed_slots, planned_slots),
            "direct_acceptance_observed_slots": direct_accepted,
            "direct_acceptance_observed_total": observed_slots,
            "direct_acceptance_rate": _rate(direct_accepted, observed_slots),
            "unobserved_slots": unobserved_slots,
            "weekly_swaps": actions["swap_out"],
            "temporary_snoozes": actions["snooze"],
            "temporary_unsnoozes": actions["unsnooze"],
            "permanent_dislikes": actions["dislike"],
            "ratings": ratings,
            "rating_events": sum(ratings.values()),
            "weekly_duplicate_count": sum(item["duplicate_count"] for item in weekly),
            "structure_warning_count": sum(item["structure_warnings"] for item in weekly),
            "consecutive_week_overlap_rate": (
                round(sum(overlap_values) / len(overlap_values), 4) if overlap_values else None
            ),
            "consecutive_week_pairs": len(overlap_values),
        },
        "weeks": weekly,
        "data_scope": {
            "simulated_data": False,
            "plans_read": len(period_records),
            "latest_plan_per_week": len(selected),
            "events_read": len(period_events),
            "note": "只读本地方案和菜品事件；没有读取或输出原始用户偏好内容",
        },
    }


def _percent(value: Optional[float]) -> str:
    return "样本不足" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    sample = report["sample"]
    metrics = report["metrics"]
    ratings = metrics["ratings"]
    lines = [
        "# 本地真实使用周报",
        "",
        f"数据窗口：{report['period']['start']} 至 {report['period']['end']}",
        f"样本状态：**{sample['status']}**（{sample['message']}）",
        "",
        "> 本报告只读取本地方案和菜品事件，不使用模拟数据；样本不足时不做趋势判断。",
        "",
        "## 核心指标",
        "",
        (
            "| 指标 | 当前值 | 口径 |\n"
            "|---|---:|---|\n"
            f"| 计划餐次 | {metrics['planned_slots']} | 最近版本每周计划中的非跳过餐次 |\n"
            f"| 已完成餐次 | {metrics['completed_slots']} | `做完了` 事件或存档完成状态 |\n"
            f"| 计划完成率 | {_percent(metrics['completion_rate'])} | 已完成餐次 / 计划餐次 |\n"
            f"| 可观测直接接受率 | {_percent(metrics['direct_acceptance_rate'])} | 仅在有完成或换菜证据的餐次中计算 |\n"
            f"| 每周换菜次数 | {metrics['weekly_swaps']} | `swap_out` 事件 |\n"
            f"| 临时避开 / 恢复 | {metrics['temporary_snoozes']} / {metrics['temporary_unsnoozes']} | `snooze` / `unsnooze` |\n"
            f"| 永久不喜欢 | {metrics['permanent_dislikes']} | `dislike` 事件 |\n"
            f"| 连续周重合率 | {_percent(metrics['consecutive_week_overlap_rate'])} | 相邻周菜谱 ID 的 Jaccard 均值 |\n"
            f"| 周内重复数 | {metrics['weekly_duplicate_count']} | 每周菜谱 ID 重复次数 |\n"
            f"| 结构不足记录 | {metrics['structure_warning_count']} | 存档中的结构告警代理，不等同于用户主动放宽 |\n"
        ),
        "",
        "## 评分分布",
        "",
        f"好吃：{ratings['好吃']}；一般：{ratings['一般']}；下次不做：{ratings['下次不做']}",
        "",
        "## 分周数据",
        "",
        (
            "| 周起始日 | 计划餐次 | 完成餐次 | 完成率 | 换菜 | 周内重复 | 结构告警 |\n"
            "|---|---:|---:|---:|---:|---:|---|"
        ),
    ]
    for item in report["weeks"]:
        lines.append(
            f"| {item['week_start']} | {item['planned_slots']} | {item['completed_slots']} "
            f"| {_percent(item['completion_rate'])} | {item['swaps']} | "
            f"{item['duplicate_count']} | {item['structure_warnings']} |"
        )
    lines.extend(["", "## 数据范围", "", f"- 读取方案版本：{report['data_scope']['plans_read']} 份；"
                  f"按周去重后：{report['data_scope']['latest_plan_per_week']} 周",
                  f"- 读取菜品事件：{report['data_scope']['events_read']} 条",
                  f"- {report['data_scope']['note']}", ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path | str) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    json_path = directory / f"usage-report-{stamp}.json"
    md_path = directory / f"usage-report-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path
