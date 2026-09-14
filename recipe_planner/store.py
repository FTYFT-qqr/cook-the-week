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

from recipe_planner.infra.jsonfile import ArchiveBroken, read_json, write_json
from recipe_planner.models import PlanRecord as PlanRecordModel
from recipe_planner.models import PlanResult, UserConstraints, slot_key

DEFAULT_PLANS_FILE = Path(__file__).resolve().parent.parent / "data" / "saved_plans.json"
MAX_PLANS = 3  # 只留最近几份；方案历史与对比属于正式版后续能力


class PlanRecord(PlanRecordModel):
    """兼容别名：结构定义已上移到 models（JSON / DB 两种后端共用）。"""

    def constraints(self) -> UserConstraints:
        return self.result.constraints


# ---------------------------------------------------------------- 路径与读写

def plans_path() -> Path:
    return Path(os.environ.get("RECIPE_PLAN_FILE", str(DEFAULT_PLANS_FILE)))


def load_records() -> list[PlanRecord]:
    """按「最近生成的在最前」返回全部存档。

    docs/11 §4.1 P0-2：**读不出来 ≠ 还没有存档**。以前这里 `except: return []`，
    于是一个被写坏的文件会和"空存档"变成同一件事，紧接着的保存就拿空基覆盖了历史。
    现在：文件不在 → 空列表（真的还没有）；文件在但读不出来 → 抛 `ArchiveBroken`。
    """
    p = plans_path()
    data = read_json(p)
    if data is None:
        return []
    raw = data.get("plans", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ArchiveBroken(p, f"内容是 {type(raw).__name__}，不是方案列表")
    out: list[PlanRecord] = []
    for item in raw:
        try:
            out.append(PlanRecord.model_validate(item))
        except Exception as exc:
            # 单条读不出来也不能当没有 —— 照旧写回去就等于**删掉那一份方案**
            raise ArchiveBroken(p, f"有一份方案读不出来（{type(exc).__name__}: {exc}）") from exc
    return out


def _write(records: list[PlanRecord]) -> None:
    payload = {
        "version": 1,
        "plans": [json.loads(r.model_dump_json()) for r in records[:MAX_PLANS]],
    }
    write_json(plans_path(), payload)


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


def set_done(record_id: Optional[str], day: int, done: bool = True,
             meal: Optional[str] = None) -> Optional[PlanRecord]:
    """标记/取消「做过了」（M1 状态③）。

    docs/10：给了 `meal` 就记**这一顿**（`done_slots`）；不给就沿用老行为 ——
    记"这一天"（`done_days`），只做晚餐时两者等价。
    """
    if not record_id:
        return None
    recs = load_records()
    for i, r in enumerate(recs):
        if r.id == record_id:
            if meal is None:
                days = set(r.done_days)
                days.add(day) if done else days.discard(day)
                recs[i] = r.model_copy(update={"done_days": sorted(days)})
            else:
                slots = set(r.done_slots or [])
                key = slot_key(day, meal)
                slots.add(key) if done else slots.discard(key)
                recs[i] = r.model_copy(update={"done_slots": sorted(slots)})
            _write(recs)
            return recs[i]
    return None


def set_checked(record_id: Optional[str], names: list[str]) -> Optional[PlanRecord]:
    """记住买菜清单的勾选（M4：一周边买边勾，关掉浏览器再打开还在）。"""
    if not record_id:
        return None
    recs = load_records()
    for i, r in enumerate(recs):
        if r.id == record_id:
            recs[i] = r.model_copy(update={"checked_items": sorted(set(names))})
            _write(recs)
            return recs[i]
    return None


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


def today_index(start: Any, days: int, today: Optional[date] = None) -> Optional[int]:
    """今天是这一周的第几天（从 0 起）；不在这一周范围内返回 None。

    客户的原话：「我周三打开它，它给我的是第 1 天、第 2 天……没有『今晚轮到第几天』。」
    """
    s = normalize_start(start)
    today = today or date.today()
    delta = (today - s).days
    return delta if 0 <= delta < max(days, 0) else None


# ---------------------------------------------------------------- 表单回填

def inputs_from_constraints(c: UserConstraints, start_date: Any = None) -> dict:
    """把存档里的约束还原成表单字典，让「回访」与「重排」都能直接用。"""
    return {
        "people": c.people,
        "days": c.days,
        "dishes_per_day": c.dishes_per_day,
        "meals": list(c.active_meals()),                 # docs/10：这一周吃哪几顿
        "dishes_per_meal": dict(c.dishes_per_meal or {}),  # 每餐几道菜（空=用 dishes_per_day）
        "breakfast_max_time_min": c.breakfast_max_time_min,
        "allergens": list(c.allergens),
        "spice": c.spice_level,
        "taste_tags": list(c.taste_tags),
        "goal": c.goal,
        "max_time_min": c.max_time_min,
        "skill": c.skill,
        "cook_start": c.cook_start,
        "budget_per_person_day": c.budget_per_person_day,
        "pantry_items": list(c.pantry_items),
        "must_include": list(c.must_include_recipes),   # 被「定住 / 加一道」的菜
        "start_date": normalize_start(start_date).isoformat(),
    }


# ---------------------------------------------------------------- 后端切换（docs/08 §7）
# STORAGE=db 时，用数据库实现覆盖上面的 JSON 实现；`app.py` 与现有测试一行都不用改。
_json_today_index = today_index          # db_store 复用这段纯日期逻辑（无 IO）

from recipe_planner.infra import settings as _settings  # noqa: E402

# USE_API=1 要**优先**判断，而不是和 db 分支并列：服务化之后界面进程不应该再去碰数据库文件，
# 所以这个分支必须盖住下面的 db 分支（否则界面会一边连 API 一边自己开库）。
if _settings.use_api():  # pragma: no cover - 由环境变量决定
    from recipe_planner.client import (  # noqa: E402,F401,F811
        archive_summary, delete_record, get_record, latest_record, load_records,
        previous_record, save_plan, set_checked, set_done, update_result,
    )
elif _settings.storage_kind() == "db":  # pragma: no cover - 由环境变量决定
    from recipe_planner.storage.db_store import (  # noqa: E402,F401,F811
        archive_old, archive_summary, delete_record, get_record, latest_record,
        load_records, previous_record, save_plan, set_checked, set_done,
        today_index, update_result,
    )
