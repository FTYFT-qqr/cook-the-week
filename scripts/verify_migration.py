"""隔离固定快照迁移校验（docs/15 F-02）。

本脚本只验证“这组固定 JSON 能否完整导入一份全新的 SQLite”：

* 固定 fixture 与真实 `data/` 完全分离；
* 每次使用新的临时数据库，连续运行不会改变真实用户数据；
* 方案、勾选、完成状态、偏好和评分逐字段比较；
* 当前正在使用的数据库由 `audit_live_data.py` 单独做只读审计，不能拿它和
  迁移前冻结的 JSON 强行比较。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "migration_baseline"
sys.path.insert(0, str(ROOT))


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _recipe_snapshot(recipe):
    return {
        "id": recipe.id,
        "name": recipe.name,
        "category": recipe.category,
        "description": recipe.description,
        "difficulty": recipe.difficulty,
        "time_min": recipe.time_min,
        "cost_yuan": float(recipe.cost_yuan),
        "calories": recipe.calories,
        "protein_g": float(recipe.protein_g) if recipe.protein_g is not None else None,
        "taste_tags": list(recipe.taste_tags),
        "spice_level": recipe.spice_level,
        "goal_tags": list(recipe.goal_tags),
        "allergens": list(recipe.allergens),
        "steps": list(recipe.steps),
        "video_url": recipe.video_url,
        "ingredients": [
            {"name": i.name, "amount": i.amount, "category": i.category,
             "grams": float(i.grams) if i.grams is not None else None}
            for i in recipe.ingredients
        ],
    }


def _plan_snapshot(record):
    return {
        "id": record.id,
        "start_date": record.start_date,
        "done_days": sorted(record.done_days),
        "done_slots": sorted(record.done_slots),
        "checked_items": sorted(record.checked_items),
        "constraints": json.loads(record.result.constraints.model_dump_json()),
        "days": [
            {"day": p.day, "meal": p.meal, "skipped": p.skipped, "people": p.people,
             "dishes": [{"recipe_id": d.recipe_id, "reason": d.reason} for d in p.dishes]}
            for p in record.result.days
        ],
        "shopping": [
            {"name": item.name, "category": item.category, "amount": item.amount,
             "needed": item.needed, "for_recipes": list(item.for_recipes)}
            for item in record.result.shopping
        ],
    }


def _profile_snapshot(profile: dict) -> dict:
    return {
        "liked_dishes": sorted(profile.get("liked_dishes", [])),
        "disliked_dishes": sorted(profile.get("disliked_dishes", [])),
        "ratings": {name: int((info or {}).get("score", 1))
                    for name, info in sorted((profile.get("ratings") or {}).items())},
    }


def main() -> int:
    if not FIXTURES.is_dir():
        print(f"✘ 缺少迁移 fixture：{FIXTURES}")
        return 1

    temp_root = Path(tempfile.mkdtemp(prefix="migration-", dir=ROOT / ".tmp"))
    db_path = temp_root / "app.db"
    backup_path = temp_root / "backup"
    keys = (
        "STORAGE", "USE_API", "DATABASE_URL", "RECIPE_DB_FILE",
        "RECIPE_PLAN_FILE", "RECIPE_PROFILE_FILE", "MIGRATION_BACKUP_DIR",
    )
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.update({
            "STORAGE": "db",
            "USE_API": "0",
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
            "RECIPE_DB_FILE": str(FIXTURES / "recipes.json"),
            "RECIPE_PLAN_FILE": str(FIXTURES / "saved_plans.json"),
            "RECIPE_PROFILE_FILE": str(FIXTURES / "customer_profile.json"),
            "MIGRATION_BACKUP_DIR": str(backup_path),
        })

        from recipe_planner import db, profile, store
        from recipe_planner.storage import import_json
        from recipe_planner.storage.engine import reset_engine
        from recipe_planner.models import PlanRecord

        fixture_recipes = db._load_json_db(FIXTURES / "recipes.json")
        fixture_plans = _read(FIXTURES / "saved_plans.json").get("plans", [])
        fixture_profile = _read(FIXTURES / "customer_profile.json")
        result = import_json.import_all()
        print(f"[迁移] 菜谱 {result['recipes']} 道，方案 {result['plans']} 份")

        imported_recipes = db.load_db()
        actual_recipes = sorted((_recipe_snapshot(r) for r in imported_recipes.recipes),
                                key=lambda x: x["id"])
        expected_recipes = sorted((_recipe_snapshot(r) for r in fixture_recipes.recipes),
                                  key=lambda x: x["id"])
        if actual_recipes != expected_recipes:
            print("✘ 菜谱或食材逐字段不一致")
            return 1
        print("  ✔ 菜谱与食材逐字段一致")

        actual_plans = sorted((_plan_snapshot(r) for r in store.load_records()),
                              key=lambda x: x["id"])
        expected_plans = sorted(
            (_plan_snapshot(PlanRecord.model_validate(item)) for item in fixture_plans),
            key=lambda x: x["id"])
        if actual_plans != expected_plans:
            print("✘ 方案、菜、清单、勾选或完成状态不一致")
            print(f"  expected={expected_plans}")
            print(f"  actual={actual_plans}")
            return 1
        print("  ✔ 方案、菜、清单、勾选与完成状态一致")

        actual_profile = _profile_snapshot(profile.load_profile())
        expected_profile = _profile_snapshot(fixture_profile)
        if actual_profile != expected_profile:
            print("✘ 偏好或评分不一致")
            print(f"  expected={expected_profile}")
            print(f"  actual={actual_profile}")
            return 1
        print("  ✔ 喜欢、不喜欢与评分一致")
        print("✅ 隔离固定快照迁移校验通过；真实 data/ 未参与比较")
        return 0
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            reset_engine()
        except UnboundLocalError:
            pass
        shutil.rmtree(temp_root, ignore_errors=False)


if __name__ == "__main__":
    sys.exit(main())
