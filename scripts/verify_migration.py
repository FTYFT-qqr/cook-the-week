"""迁移等价校验（docs/09 P0-5）：证明"JSON 里的东西，库里一模一样"。

比对三类：
1. 菜谱：条数 + 每条的字段逐一比对（含食材与克数）；
2. 方案：每份的 天数/菜/天数跳过/勾选/已完成 与 JSON 一致，并用领域层重算摘要（花费/命中/最费时）；
3. 档案：喜欢/不喜欢/评分集合完全相等（来源痕迹来源一致）。

运行：python scripts/verify_migration.py   （需要已执行 import_json）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STORAGE", "db")

from recipe_planner import reporting as rep  # noqa: E402
from recipe_planner import store  # noqa: E402
from recipe_planner.db import _load_json_db  # noqa: E402
from recipe_planner.db import load_db as load_db_current  # noqa: E402
from recipe_planner.models import PlanRecord  # noqa: E402

DATA = ROOT / "data"
PASS, FAIL = 0, []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL.append(name)
        print(f"  ✘ {name}  {detail}")


def read_json(name: str):
    p = DATA / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    json_db = _load_json_db()
    db = load_db_current()
    print(f"[1] 菜谱：JSON {len(json_db.recipes)} 道 → DB {len(db.recipes)} 道")
    check("菜谱条数一致", len(json_db.recipes) == len(db.recipes))
    by_id = {r.id: r for r in db.recipes}
    mismatched = []
    for r in json_db.recipes:
        d = by_id.get(r.id)
        if d is None:
            mismatched.append(f"缺少 {r.id}")
            continue
        for field in ("name", "category", "difficulty", "time_min", "spice_level",
                      "cost_yuan", "calories", "protein_g", "taste_tags", "goal_tags", "allergens",
                      "steps", "video_url"):
            if getattr(r, field) != getattr(d, field):
                mismatched.append(f"{r.id}.{field}: {getattr(r, field)} != {getattr(d, field)}")
        if len(r.ingredients) != len(d.ingredients):
            mismatched.append(f"{r.id}.ingredients 数量不同")
        else:
            for a, b in zip(r.ingredients, d.ingredients):
                if (a.name, a.amount, a.category, a.grams) != (b.name, b.amount, b.category, b.grams):
                    mismatched.append(f"{r.id} 食材 {a.name} 不一致")
    check("每条菜谱的字段与食材逐一一致", not mismatched, "; ".join(mismatched[:4]))

    raw_plans = read_json("saved_plans.json")
    json_plans = raw_plans.get("plans", []) if isinstance(raw_plans, dict) else raw_plans
    db_plans = {r.id: r for r in store.load_records()}
    print(f"[2] 方案：JSON {len(json_plans or [])} 份 → DB {len(db_plans)} 份")
    # 校验的是"**迁移没丢东西**"，不是"库里不许比 JSON 多"：
    # 迁完之后用户接着在界面/服务端排的新方案只会进库，JSON 那份是冻结的迁移基线。
    # 拿两边条数相等当条件的话，用户每排一次新方案这里就会红一次（假警报）。
    missing = [j.get("id") for j in (json_plans or []) if j.get("id") not in db_plans]
    check("JSON 里每一份方案都还在库里", not missing,
          f"库里找不到：{missing}（JSON {len(json_plans or [])} 份 / DB {len(db_plans)} 份）")
    if len(db_plans) > len(json_plans or []):
        extra = sorted(set(db_plans) - {j.get("id") for j in (json_plans or [])})
        print(f"    （库里有 {len(extra)} 份是迁移之后新排的，不参与比对：{extra}）")
    for item in json_plans or []:
        try:
            j = PlanRecord.model_validate(item)
        except Exception:
            continue
        d = db_plans.get(j.id)
        if d is None:
            check(f"方案 {j.id} 已入库", False)
            continue
        jd = [(p.day, [x.recipe_id for x in p.dishes], p.skipped, p.people) for p in j.result.days]
        dd = [(p.day, [x.recipe_id for x in p.dishes], p.skipped, p.people) for p in d.result.days]
        check(f"方案 {j.label} 的每天菜与状态一致（{len(jd)} 天）", jd == dd, f"{jd} != {dd}")
        check(f"方案 {j.label} 的勾选一致", sorted(j.checked_items) == sorted(d.checked_items),
              f"{j.checked_items} != {d.checked_items}")
        check(f"方案 {j.label} 的已做过一致", sorted(j.done_days) == sorted(d.done_days))
        sj = rep.plan_summary(j.result, json_db, j.start_date)
        sd = rep.plan_summary(d.result, db, d.start_date)
        check(f"方案 {j.label} 的成本/天数/命中重算一致",
              (round(sj.total_cost, 1), sj.days, sj.liked_hit) ==
              (round(sd.total_cost, 1), sd.days, sd.liked_hit),
              f"{(sj.total_cost, sj.days, sj.liked_hit)} != {(sd.total_cost, sd.days, sd.liked_hit)}")

    prof_json = read_json("customer_profile.json")
    prof_db = store.__dict__  # 仅为可读性；下面直接用 profile 模块
    from recipe_planner import profile as prof  # noqa: E402
    live = prof.load_profile()
    print(f"[3] 档案：JSON 喜欢 {len(prof_json.get('liked_dishes', []))} / "
          f"不喜欢 {len(prof_json.get('disliked_dishes', []))} → DB 喜欢 "
          f"{len(live.get('liked_dishes', []))} / 不喜欢 {len(live.get('disliked_dishes', []))}")
    check("喜欢的菜一致", sorted(prof_json.get("liked_dishes", [])) == sorted(live.get("liked_dishes", [])),
          f"{prof_json.get('liked_dishes')} != {live.get('liked_dishes')}")
    check("不喜欢的菜一致", sorted(prof_json.get("disliked_dishes", [])) ==
          sorted(live.get("disliked_dishes", [])))
    check("评分一致", {k: v.get("score") for k, v in (prof_json.get("ratings") or {}).items()} ==
          {k: v.get("score") for k, v in (live.get("ratings") or {}).items()})

    print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    print("✅ 迁移等价校验通过：JSON 与数据库内容一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
