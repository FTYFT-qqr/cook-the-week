"""方案存档：把「这一周」写进磁盘。

MVP 阶段菜单只活在会话里 —— 关掉页面、刷新、第二天再来就没了。
对一个「排一周」的产品，这是最伤的一刀。本模块负责：

- 落盘：菜单 / 清单 / 校验结果 / 约束一起存下来；
- 回访：打开页面就是「你有一份 8/12–8/18 的菜单」，而不是又一张空表单；
- 不静默覆盖：重排会追加新方案，旧方案可一键找回。

通过 RECIPE_PLAN_FILE 环境变量可指向临时文件，便于自动化测试。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from recipe_planner.models import PlanResult, UserConstraints

DEFAULT_PLANS_FILE = Path(__file__).resolve().parent.parent / "data" / "saved_plans.json"
MAX_PLANS = 3  # 只留最近几份；方案历史与对比属于正式版后续能力


class PlanRecord(BaseModel):
    id: str
    created_at: str = ""      # "2026-08-09 15:20"
    start_date: str = ""      # ISO 日期，这一周的第一天（周一）
    label: str = ""           # "8/12–8/18"
    change_note: str = ""     # 最近一次改动的说明
    result: PlanResult

    def constraints(self) -> UserConstraints:
        return self.result.constraints


# ---------------------------------------------------------------- 路径与读写

def plans_path() -> Path:
    return Path(os.environ.get("RECIPE_PLAN_FILE", str(DEFAULT_PLANS_FILE)))


def load_records() -> list[PlanRecord]:
    """按「最近生成的在最前」返回全部存档；坏掉的单条会被跳过而不是让整页崩掉。"""
    p = plans_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("plans", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: list[PlanRecord] = []
    for item in raw:
        try:
            out.append(PlanRecord.model_validate(item))
        except Exception:
            continue
    return out


def _write(records: list[PlanRecord]) -> None:
    p = plans_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "plans": [json.loads(r.model_dump_json()) for r in records[:MAX_PLANS]],
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 查询

def latest_record() -> Optional[PlanRecord]:
    recs = load_records()
    return recs[0] if recs else None


def get_record(record_id: str) -> Optional[PlanRecord]:
    return next((r for r in load_records() if r.id == record_id), None)


def previous_record(record_id: Optional[str]) -> Optional[PlanRecord]:
    """比当前这份更早的一版（用于「回到上一版」）。"""
    recs = load_records()
    if record_id is None:
        return recs[0] if recs else None
    for i, r in enumerate(recs):
        if r.id == record_id:
            return recs[i + 1] if i + 1 < len(recs) else None
    return recs[0] if recs else None


# ---------------------------------------------------------------- 写入

def save_plan(result: PlanResult, start_date: Any = None,
              change_note: str = "", now: Optional[datetime] = None) -> PlanRecord:
    """把这次生成的方案存为新的一份（追加，不覆盖旧的）。"""
    now = now or datetime.now()
    start = normalize_start(start_date if start_date is not None else next_monday(now.date()))
    rec = PlanRecord(
        id=uuid.uuid4().hex[:8],
        created_at=now.strftime("%Y-%m-%d %H:%M"),
        start_date=start.isoformat(),
        label=week_label(start),
        change_note=change_note,
        result=result,
    )
    _write([rec] + load_records())
    return rec


def update_result(record_id: Optional[str], result: PlanResult,
                  change_note: str = "") -> Optional[PlanRecord]:
    """就地更新某一版的菜单（换一道 / 撤销之后调用），刷新页面不丢。"""
    if not record_id:
        return None
    recs = load_records()
    for i, r in enumerate(recs):
        if r.id == record_id:
            recs[i] = r.model_copy(
                update={"result": result, "change_note": change_note or r.change_note}
            )
            _write(recs)
            return recs[i]
    return None


def delete_record(record_id: str) -> None:
    _write([r for r in load_records() if r.id != record_id])


def archive_summary() -> list[str]:
    return [f"{r.label}（{r.created_at}）" for r in load_records()]


# ---------------------------------------------------------------- 日期与标签

def next_monday(today: Optional[date] = None) -> date:
    """默认排「下一周」：今天是周几都能得到一个即将到来的周一。"""
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def normalize_start(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return next_monday()


def week_label(start: Any) -> str:
    s = normalize_start(start)
    e = s + timedelta(days=6)
    return f"{s.month}/{s.day}–{e.month}/{e.day}"


WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_name(start: Any, day_index: int) -> str:
    """day_index 从 0 开始。"""
    s = normalize_start(start)
    return WEEKDAYS[(s.weekday() + day_index) % 7]


def day_date_label(start: Any, day_index: int) -> str:
    s = normalize_start(start) + timedelta(days=day_index)
    return f"{s.month}/{s.day}"


# ---------------------------------------------------------------- 表单回填

def inputs_from_constraints(c: UserConstraints, start_date: Any = None) -> dict:
    """把存档里的约束还原成表单字典，让「回访」与「重排」都能直接用。"""
    return {
        "people": c.people,
        "days": c.days,
        "dishes_per_day": c.dishes_per_day,
        "allergens": list(c.allergens),
        "spice": c.spice_level,
        "taste_tags": list(c.taste_tags),
        "goal": c.goal,
        "max_time_min": c.max_time_min,
        "budget_per_person_day": c.budget_per_person_day,
        "pantry_items": list(c.pantry_items),
        "start_date": normalize_start(start_date).isoformat(),
    }
