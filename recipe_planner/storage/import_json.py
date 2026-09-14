"""JSON → 数据库 一次性导入（docs/09 P0-5）。

- 导入前把三个 JSON 备份到 `data/backup/<时间戳>/`；
- 菜谱：`recipes.json` → `recipe` + `ingredient`；
- 方案：`saved_plans.json` → `plan` + `plan_day` + `plan_dish` + `shopping_item`（勾选与"已做过"一起带入）；
- 档案：`customer_profile.json` → `preference` + `rating`；
- 幂等：可重复执行（菜谱按 id 覆盖，方案按 id 存在则跳过）。

运行：python -m recipe_planner.storage.import_json
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

from recipe_planner.infra import jsonfile, settings
from recipe_planner.models import PlanRecord, PlanResult, RecipeDB, parse_slot_key
from recipe_planner.storage import migrate, sync_bridge
from recipe_planner.storage.repositories import PlanRepo, ProfileRepo, RecipeRepo

DATA = settings.DATA_DIR


def ensure_schema() -> None:
    """本地首次初始化：走 Alembic（`create_all` 只留给测试，见 docs/09 P0-3）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    action = migrate.ensure_schema(verbose=True)
    print(f"  （{action}）")


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
    """读源文件。

    docs/11 §4.1 P0-2：**读不出来不能当成"内容为空"** —— 以前这里 `return {}`，
    于是一个坏掉的 `saved_plans.json` 会打印"方案 0 份入库"然后**正常退出**，
    看起来像"迁移成功了，只是没有方案"。现在读不出来就报错并留档。
    """
    if not path.exists():
        return {}
    try:
        return jsonfile.read_json(path) or {}
    except jsonfile.ArchiveBroken as exc:
        raise SystemExit(f"源文件读不出来，先别迁移：{exc}") from exc


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
    broken: list[str] = []
    for item in items or []:
        try:
            rec = PlanRecord.model_validate(item)
        except Exception as exc:
            # 不能装作没看见：漏掉一份方案，用户以后只会发现"我那一周没了"
            broken.append(f"{item.get('id', '?') if isinstance(item, dict) else '?'}: {exc}")
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
        # docs/10 起"做完了"是按顿记的：漏了这一段，迁移之后多餐的"做完了"就全丢了
        for slot in rec.done_slots or []:
            parsed = parse_slot_key(slot)
            if parsed is None:
                broken.append(f"{rec.id}: 认不出的做完了标记 {slot!r}")
                continue
            sync_bridge.run(PlanRepo.set_done(saved.id, parsed[0], True, parsed[1]))
        if rec.checked_items:
            sync_bridge.run(PlanRepo.set_checked(saved.id, rec.checked_items))
        n_plans += 1
    print(f"方案 {n_plans} 份入库")
    if broken:
        print(f"  ! 有 {len(broken)} 处没导进去（下面每一条都要人看一眼）：")
        for line in broken:
            print(f"    - {line}")

    prof = _read_json(DATA / "customer_profile.json")
    if prof:
        sync_bridge.run(ProfileRepo.save_profile(prof))
        print(f"档案入库：喜欢 {len(prof.get('liked_dishes', []))} 道 / "
              f"不喜欢 {len(prof.get('disliked_dishes', []))} 道 / "
              f"评分 {len(prof.get('ratings', {}))} 条")

    archived = sync_bridge.run(PlanRepo.archive_old(settings.plan_retention_weeks()))
    print(f"按保留策略归档 {archived} 份（超过 {settings.plan_retention_weeks()} 周）")
    return {"recipes": n_recipes, "plans": n_plans, "backup": str(backup), "broken": broken}


if __name__ == "__main__":
    result = import_all()
    print("完成:", result)
    # 有东西没导进去就不能算成功（退出码非 0，脚本/人一眼能看出来）
    sys.exit(2 if result["broken"] else 0)
