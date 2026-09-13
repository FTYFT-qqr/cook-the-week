"""数据库后端的同步门面：与 `store.py` 的公开函数**同名同义**。

`store.py` 末尾按 `STORAGE=db` 决定是否用这里的实现覆盖 JSON 实现，
因此 `app.py`、`scripts/*` 与现有测试一行都不用改（docs/08 §7）。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from recipe_planner.models import PlanRecord, PlanResult
from recipe_planner.storage import sync_bridge
from recipe_planner.storage.repositories import PlanRepo, RecipeRepo

run = sync_bridge.run


def load_records() -> list[PlanRecord]:
    return run(PlanRepo.load_records())


def latest_record() -> Optional[PlanRecord]:
    return run(PlanRepo.latest_record())


def get_record(record_id: str) -> Optional[PlanRecord]:
    return run(PlanRepo.get_record(record_id))


def previous_record(current_id: Optional[str]) -> Optional[PlanRecord]:
    return run(PlanRepo.previous_record(current_id))


def save_plan(result: PlanResult, start_date: Any = None, change_note: str = "",
              now: Optional[datetime] = None) -> PlanRecord:
    return run(PlanRepo.save_plan(result, start_date, change_note, now))


def update_result(record_id: Optional[str], result: PlanResult,
                  change_note: str = "") -> Optional[PlanRecord]:
    return run(PlanRepo.update_result(record_id, result, change_note))


def delete_record(record_id: str) -> None:
    run(PlanRepo.delete_record(record_id))


def set_done(record_id: Optional[str], day: int, done: bool = True,
             meal: Optional[str] = None) -> Optional[PlanRecord]:
    return run(PlanRepo.set_done(record_id, day, done, meal))


def set_checked(record_id: Optional[str], names: list[str]) -> Optional[PlanRecord]:
    return run(PlanRepo.set_checked(record_id, names))


def archive_old(weeks: int = 12) -> int:
    return run(PlanRepo.archive_old(weeks))


def archive_summary() -> list[str]:
    return [f"{r.label}（{r.created_at}）" for r in load_records()]


def today_index(start: Any, days: int, today: Optional[date] = None) -> Optional[int]:
    from recipe_planner import store as store_mod
    return store_mod._json_today_index(start, days, today)
