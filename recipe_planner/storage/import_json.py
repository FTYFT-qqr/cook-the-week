"""JSON → 数据库 一次性导入（docs/09 P0-5）。

- 导入前把三个 JSON 备份到 `data/backup/<时间戳>/`；
- 菜谱：`recipes.json` → `recipe` + `ingredient`；
- 方案：`saved_plans.json` → `plan` + `plan_day` + `plan_dish` + `shopping_item`（勾选与"已做过"一起带入）；
- 档案：`customer_profile.json` → `preference` + `rating`；
- 幂等：可重复执行（菜谱按 id 覆盖，方案按 id 存在则跳过）。

运行：python -m recipe_planner.storage.import_json
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from recipe_planner.infra import settings
from recipe_planner.models import PlanRecord, PlanResult, RecipeDB
from recipe_planner.storage import sync_bridge
from recipe_planner.storage.engine import create_all
from recipe_planner.storage.repositories import PlanRepo, ProfileRepo, RecipeRepo

DATA = settings.DATA_DIR


def ensure_schema() -> None:
    """本地首次初始化：建表（生产走 `alembic upgrade head`，见 docs/09 P0-3）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    sync_bridge.run(create_all())
    print(f"数据库就绪：{settings.database_url()}")


def _backup() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = DATA / "backup" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("recipes.json", "saved_plans.json", "customer_profile.json"):
        src = DATA / name
        if src.exists():
            shutil.copy2(src, dest / name)
    return dest


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ! 读不了 {path.name}: {exc}")
        return {}


def import_all() -> dict:
    ensure_schema()
    backup = _backup()
    print(f"备份到 {backup}")

    from recipe_planner.db import _load_json_db

    db: RecipeDB = _load_json_db()
    n_recipes = sync_bridge.run(RecipeRepo.upsert_many(db.recipes))
    print(f"菜谱 {n_recipes} 道入库")

    plans_raw = _read_json(DATA / "saved_plans.json")
    items = plans_raw.get("plans", []) if isinstance(plans_raw, dict) else plans_raw
    n_plans = 0
    for item in items or []:
        try:
            rec = PlanRecord.model_validate(item)
        except Exception as exc:
            print(f"  ! 跳过一条损坏的方案: {exc}")
            continue
        if sync_bridge.run(PlanRepo.get_record(rec.id)) is not None:
            continue
        created = None
        try:
            created = datetime.strptime(rec.created_at, "%Y-%m-%d %H:%M")
        except Exception:
            created = None
        saved = sync_bridge.run(PlanRepo.save_plan(rec.result, rec.start_date,
                                                   rec.change_note, created,
                                                   make_active=False, plan_id=rec.id))
        if rec.done_days:
            for day in rec.done_days:
                sync_bridge.run(PlanRepo.set_done(saved.id, day, True))
        if rec.checked_items:
            sync_bridge.run(PlanRepo.set_checked(saved.id, rec.checked_items))
        n_plans += 1
    print(f"方案 {n_plans} 份入库")

    prof = _read_json(DATA / "customer_profile.json")
    if prof:
        sync_bridge.run(ProfileRepo.save_profile(prof))
        print(f"档案入库：喜欢 {len(prof.get('liked_dishes', []))} 道 / "
              f"不喜欢 {len(prof.get('disliked_dishes', []))} 道 / "
              f"评分 {len(prof.get('ratings', {}))} 条")

    archived = sync_bridge.run(PlanRepo.archive_old(settings.plan_retention_weeks()))
    print(f"按保留策略归档 {archived} 份（超过 {settings.plan_retention_weeks()} 周）")
    return {"recipes": n_recipes, "plans": n_plans, "backup": str(backup)}


if __name__ == "__main__":
    result = import_all()
    print("完成:", result)
    sys.exit(0)
