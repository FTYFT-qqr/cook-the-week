"""self_check.py —— demo 交付前自测（bug / 边界 / 漏洞）。

离线跑确定性核心 + 全流程（临时去掉 API key 验证 LLM 失败降级），
任何断言失败都会以非零退出码结束。

运行：python scripts/self_check.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recipe_planner.core import (  # noqa: E402
    plan_deterministic,
    retrieve_candidates,
    shopping_list,
    validate_plan,
)
from recipe_planner.db import load_db  # noqa: E402
from recipe_planner.models import UserConstraints  # noqa: E402

PASS = 0
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL.append(name)
        print(f"  ✘ {name}  {detail}")


def main() -> int:
    db = load_db()
    print(f"[1] 数据完整性: 共 {len(db.recipes)} 道菜")
    check("菜谱数 >= 20", len(db.recipes) >= 20)
    check("id 无重复", len({r.id for r in db.recipes}) == len(db.recipes))
    check("每道菜都有食材", all(r.ingredients for r in db.recipes))
    check("每道菜耗时字段合法", all(1 <= r.time_min <= 300 for r in db.recipes))

    print("[2] 检索过滤（确定性）")
    c_nopepper = UserConstraints(people=2, days=3, spice_level="不辣", goal="随便", allergens=[])
    cands = retrieve_candidates(db, c_nopepper)
    check("不辣时无『辣』菜", all(r.spice_level != "辣" for r in cands))
    c_spicy = UserConstraints(people=2, days=3, spice_level="辣", goal="随便", allergens=[])
    cands_spicy = retrieve_candidates(db, c_spicy)
    check("辣度放宽后候选更多", len(cands_spicy) > len(cands))
    c_allergy = UserConstraints(people=2, days=3, allergens=["海鲜"], spice_level="不辣")
    cands_safe = retrieve_candidates(db, c_allergy)
    check("海鲜过敏候选全部安全",
          all(not (r.all_allergen_names() & {"海鲜"}) for r in cands_safe))
    c_time = UserConstraints(people=2, days=3, max_time_min=20, spice_level="不辣")
    check("20分钟上限候选全部达标", all(r.time_min <= 20 for r in retrieve_candidates(db, c_time)))

    print("[2b] 菜品喜好（喜欢优先 / 不喜欢硬排除）")
    c_love = UserConstraints(people=1, days=1, dishes_per_day=1, spice_level="不辣",
                             goal="随便", liked_dishes=["r09"])
    top = retrieve_candidates(db, c_love)[0]
    check("喜欢的菜排到最前", top.id == "r09", f"top={top.id} {top.name}")
    c_hate = UserConstraints(people=2, days=3, spice_level="不辣", disliked_dishes=["r05", "r09"])
    cands_hate = retrieve_candidates(db, c_hate)
    check("不喜欢的菜被排除", all(r.id not in {"r05", "r09"} for r in cands_hate))
    plans_love, _ = plan_deterministic(retrieve_candidates(db, c_love), db, c_love)
    ids_love = [d.recipe_id for p in plans_love for d in p.dishes]
    check("喜欢且唯一候选时被选中", "r09" in ids_love, str(ids_love))

    print("[3] 确定性排菜")
    for label, cc in [
        ("3人4天每顿2菜", UserConstraints(people=3, days=4, dishes_per_day=2, spice_level="不辣")),
        ("1人3天每顿3菜", UserConstraints(people=1, days=3, dishes_per_day=3, spice_level="辣")),
        ("控糖高预算", UserConstraints(people=2, days=5, dishes_per_day=2, goal="控糖",
                                       budget_per_person_day=60.0, spice_level="不辣")),
        ("海鲜过敏高蛋白", UserConstraints(people=2, days=3, dishes_per_day=2, goal="高蛋白",
                                          allergens=["海鲜"], spice_level="不辣")),
    ]:
        cands = retrieve_candidates(db, cc)
        plans, warns = plan_deterministic(cands, db, cc)
        ids = [d.recipe_id for p in plans for d in p.dishes]
        check(f"{label}: {len(ids)}道", len(ids) > 0, f"plans={len(plans)}")
        check(f"{label}: 无重复", len(set(ids)) == len(ids), f"dup={[i for i in ids if ids.count(i) > 1]}")
        issues = validate_plan(plans, db, cc)
        hard = [i for i in issues if i.level == "error"]
        check(f"{label}: 无硬错误", len(hard) == 0, "; ".join(i.message for i in hard[:3]))

    print("[4] 校验器能抓到错误（故意构造坏计划）")
    from recipe_planner.models import ChosenDish, DayPlan
    bad_plans = [DayPlan(day=1, dishes=[ChosenDish(recipe_id="r02"), ChosenDish(recipe_id="r02")]),
                 DayPlan(day=2, dishes=[ChosenDish(recipe_id="r02"), ChosenDish(recipe_id="zzz_no_such")])]
    issues = validate_plan(bad_plans, db,
                           UserConstraints(people=2, days=2, allergens=["海鲜"], budget_per_person_day=5.0,
                                           max_time_min=10))
    codes = {i.code for i in issues}
    check("抓到 duplicate", "duplicate" in codes)
    check("抓到 allergen", "allergen" in codes)
    check("抓到 over_budget", "over_budget" in codes)
    check("抓到 over_time", "over_time" in codes)
    check("抓到 unknown_recipe", "unknown_recipe" in codes)

    print("[5] 买菜清单（扣库存）")
    fixed_plans = [DayPlan(day=1, dishes=[ChosenDish(recipe_id="r01"), ChosenDish(recipe_id="r04")]),
                   DayPlan(day=2, dishes=[ChosenDish(recipe_id="r28"), ChosenDish(recipe_id="r14")])]
    items = shopping_list(fixed_plans, db, UserConstraints(people=2, days=2, pantry_items=["鸡蛋", "土豆"]))
    names = {s.name for s in items}
    egg = next((s for s in items if s.name == "鸡蛋"), None)
    tomato = next((s for s in items if s.name == "西红柿"), None)
    check("清单按食材聚合", "鸡蛋" in names)
    check("库存扣减生效", egg is not None and egg.needed is False, f"egg={egg}")
    check("番茄需购买(若无库存)", tomato is not None and tomato.needed,
          f"tomato={tomato}")

    print("[6] 全流程（设空 API key → 应走确定性兜底，不崩溃）")
    os.environ["DEEPSEEK_API_KEY"] = ""  # 空串视为未配置，且 load_dotenv 不会覆盖已存在变量
    from recipe_planner.graph import run_pipeline
    res = run_pipeline(UserConstraints(people=2, days=3, goal="减脂", taste_tags=["清淡"],
                                       allergens=["海鲜"], budget_per_person_day=40.0,
                                       pantry_items=["鸡蛋"], spice_level="不辣",
                                       liked_dishes=["r19", "r04"], disliked_dishes=["r06"]), db)
    check("流程返回结果", res is not None)
    check("无硬错误", res.final, "; ".join(i.message for i in res.issues if i.level == "error"))
    check("LLM 标记为未使用", res.llm_used is False)
    check("含 3 天计划", len(res.days) == 3)
    total = sum(len(d.dishes) for d in res.days)
    check("每天都有菜", total >= 3)
    hated_used = [d.recipe_id for p in res.days for d in p.dishes if d.recipe_id == "r06"]
    check("不喜欢的菜(r06)绝未出现", not hated_used)
    check("traces 非空", len(res.trace) >= 5, str(res.trace))
    print("    示例 trace:")
    for t in res.trace:
        print(f"      - {t}")

    print("[7] 客户口味档案（独立模块 + 互斥规则）")
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parent.parent
    tmp_dir = _root / ".tmp" / "self_check_profile"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_profile = tmp_dir / "profile.json"
    if tmp_profile.exists():
        tmp_profile.unlink()
    os.environ["RECIPE_PROFILE_FILE"] = str(tmp_profile)
    from recipe_planner import profile as prof
    known = {r.name for r in db.recipes}
    check("初始档案为空", prof.liked_names(known) == [] and prof.disliked_names(known) == [])
    prof.set_feedback("宫保鸡丁", "like", known)
    check("like 写入成功", prof.liked_names(known) == ["宫保鸡丁"])
    prof.set_feedback("宫保鸡丁", "dislike", known)
    check("like→dislike 互斥", prof.liked_names(known) == [] and prof.disliked_names(known) == ["宫保鸡丁"])
    prof.set_feedback("宫保鸡丁", "dislike", known)
    check("再点 dislike = 取消", prof.disliked_names(known) == [])
    prof.set_feedback("不存在的一道菜", "like", known)
    check("未知菜名被过滤", prof.liked_names(known) == [])
    prof.set_feedback("麻婆豆腐", "like", known)
    sig1 = prof.profile_signature()
    prof.set_feedback("", "clear_like", known)
    check("clear_like 生效", prof.liked_names(known) == [])
    prof.set_feedback("麻婆豆腐", "dislike", known)
    check("档案指纹随偏好变化", sig1 != prof.profile_signature())
    prof.set_feedback("麻婆豆腐", "remove", known)
    check("remove 从列表移除(取消表态)",
          prof.liked_names(known) == [] and prof.disliked_names(known) == [])
    prof.set_feedback("红烧肉", "like", known)
    prof.set_feedback("红烧肉", "remove", known)
    check("remove 对「喜欢」同样生效", prof.liked_names(known) == [])
    prof.bulk_feedback(["宫保鸡丁", "酸辣土豆丝", "白灼虾"], "like", known)
    check("批量加入喜欢", set(prof.liked_names(known)) == {"宫保鸡丁", "酸辣土豆丝", "白灼虾"})
    prof.bulk_feedback(["宫保鸡丁"], "dislike", known)
    check("批量 dislike 触发互斥",
          "宫保鸡丁" not in prof.liked_names(known) and "宫保鸡丁" in prof.disliked_names(known))
    prof.bulk_feedback(["酸辣土豆丝"], "remove", known)
    check("批量 remove 生效", "酸辣土豆丝" not in prof.liked_names(known))
    prof.bulk_feedback(["不存在的菜"], "like", known)
    check("批量忽略未知菜名", "不存在的菜" not in prof.liked_names(known))
    prof.clear_all()
    check("清空档案生效", prof.liked_names(known) == [] and prof.disliked_names(known) == [])
    os.environ.pop("RECIPE_PROFILE_FILE", None)

    print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
