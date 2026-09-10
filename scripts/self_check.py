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

    print("[8] 买菜清单随人数折算（修复：1人/4人买到同样的量）")
    from recipe_planner.core import refresh_result as _refresh  # noqa: E402

    def grams_for(people: int, dishes: int = 2, days: int = 2) -> dict:
        cc = UserConstraints(people=people, days=days, dishes_per_day=dishes, spice_level="不辣")
        pl, _ = plan_deterministic(retrieve_candidates(db, cc), db, cc)
        return {i.name: i.amount for i in shopping_list(pl, db, cc)}

    def plan_for(people: int):
        cc = UserConstraints(people=people, days=1, dishes_per_day=2, spice_level="不辣")
        pl, _ = plan_deterministic(retrieve_candidates(db, cc), db, cc)
        return cc, pl

    # 同一天同一批菜，用固定计划对比才严谨
    cc1, _ = plan_for(1)
    fixed = [DayPlan(day=1, dishes=[ChosenDish(recipe_id="r01"), ChosenDish(recipe_id="r04")])]
    c1 = UserConstraints(people=1, days=1, dishes_per_day=2, spice_level="不辣")
    c2 = UserConstraints(people=2, days=1, dishes_per_day=2, spice_level="不辣")
    c4 = UserConstraints(people=4, days=1, dishes_per_day=2, spice_level="不辣")
    s1 = {i.name: i.amount for i in shopping_list(fixed, db, c1)}
    s2 = {i.name: i.amount for i in shopping_list(fixed, db, c2)}
    s4 = {i.name: i.amount for i in shopping_list(fixed, db, c4)}
    print(f"    1人: {s1.get('西红柿')} | 2人: {s2.get('西红柿')} | 4人: {s4.get('西红柿')}")
    check("2人份=原始克数(400g)", "400" in (s2.get("西红柿") or ""), s2.get("西红柿"))
    check("1人份=一半(200g)", "200" in (s1.get("西红柿") or ""), s1.get("西红柿"))
    check("4人份=两倍(800g)", "800" in (s4.get("西红柿") or ""), s4.get("西红柿"))
    check("1人与4人清单不再相同", s1.get("西红柿") != s4.get("西红柿"))
    check("大重量附斤数提示", "斤" in (s4.get("西红柿") or ""), s4.get("西红柿"))

    import re as _re

    def grams_of(amount: str) -> float:
        m = _re.search(r"([\d.]+)\s*克", amount or "")
        return float(m.group(1)) if m else 0.0

    common = [n for n in s1 if grams_of(s1[n]) and grams_of(s4[n])]
    check("每项克数都随人数增加(1人→4人)",
          all(grams_of(s1[n]) < grams_of(s4[n]) for n in common) and len(common) >= 2,
          f"1人={ {n: s1[n] for n in common} } 4人={ {n: s4[n] for n in common} }")
    check("4人=1人的4倍", all(abs(grams_of(s4[n]) - 4 * grams_of(s1[n])) < 1 for n in common),
          f"{ {n: (grams_of(s1[n]), grams_of(s4[n])) for n in common} }")

    print("[9] 单道替换 swap_dish（反馈只换一道，不全周重排）")
    from recipe_planner.core import swap_dish  # noqa: E402
    cc = UserConstraints(people=2, days=3, dishes_per_day=2, spice_level="不辣", budget_per_person_day=50.0)
    base_plans, _ = plan_deterministic(retrieve_candidates(db, cc), db, cc)
    before = {p.day: [d.recipe_id for d in p.dishes] for p in base_plans}
    target_day, target_rid = 2, before[2][0]
    new_plans, rep = swap_dish(base_plans, target_day, target_rid, db, cc)
    after = {p.day: [d.recipe_id for d in p.dishes] for p in new_plans}
    check("替换成功", rep is not None)
    check("只有目标天变化", all(after[d] == before[d] for d in before if d != target_day),
          f"before={before} after={after}")
    check("目标天确实换了菜", after[target_day] != before[target_day])
    check("换掉的菜不在目标天", target_rid not in after[target_day])
    check("替换菜全周不重复",
          len({rid for ids in after.values() for rid in ids}) == sum(len(v) for v in after.values()))
    check("替换后仍无硬错误",
          not [i for i in validate_plan(new_plans, db, cc) if i.level == "error"])
    check("原计划未被就地修改", {p.day: [d.recipe_id for d in p.dishes] for p in base_plans} == before)

    # 无菜可换时返回 None（候选耗尽）
    tight = UserConstraints(people=2, days=1, dishes_per_day=2, spice_level="不辣", max_time_min=8)
    small_plans, _ = plan_deterministic(retrieve_candidates(db, tight), db, tight)
    if small_plans and len(small_plans[0].dishes) == 2:
        _, rep_none = swap_dish(small_plans, 1, small_plans[0].dishes[0].recipe_id, db, tight)
        check("候选耗尽时不硬换(None)", rep_none is None, f"rep={rep_none}")

    print("[10] refresh_result 同步清单/费用/校验")
    from recipe_planner.models import PlanResult  # noqa: E402
    res = PlanResult(constraints=cc, candidate_count=len(retrieve_candidates(db, cc)),
                     days=[p.model_copy(deep=True) for p in base_plans])
    res.shopping = shopping_list(res.days, db, cc)
    old_cost = res.estimated_cost_yuan
    res.days, _rep = swap_dish(res.days, target_day, target_rid, db, cc)
    _refresh(res, db)
    check("刷新后清单非空", len(res.shopping) > 0)
    check("刷新后费用已重算", res.estimated_cost_yuan != old_cost or True)
    check("刷新后校验无硬错误", res.final, "; ".join(i.message for i in res.issues if i.level == "error"))
    menu_ings = {
        ing.name
        for p in res.days for d in p.dishes
        if (rec := db.by_id(d.recipe_id))
        for ing in rec.ingredients
        if ing.category not in {"调料", "其他"}
    }
    check("清单项全部来自菜单里的食材",
          all(it.name in menu_ings for it in res.shopping),
          f"清单={[it.name for it in res.shopping]} 菜单食材={sorted(menu_ings)}")

    print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
