"""self_check.py —— demo 交付前自测（bug / 边界 / 漏洞）。

离线跑确定性核心 + 全流程（临时去掉 API key 验证 LLM 失败降级），
任何断言失败都会以非零退出码结束。

运行：python scripts/self_check.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# **偏好事件（第 4 个存储）在这里设一次、全程不撤**（docs/12 阶段二）。
# 教训：只在"档案那一段"设是不够的 —— 后面还有改方案的段落，它们也会写事件
# （`store.update_result` 的 source 是"本机"），于是落到真实的 data/dish_events.json。
# 实测确实发生过一次（写进了一条 swap_out），所以改成开头一次性设好。
os.environ["RECIPE_EVENTS_FILE"] = os.path.join(_root_dir, ".tmp", "self_check_events.json")

# DB 模式（STORAGE=db）下用一份临时库跑，避免把测试数据写进真实 data/app.db
if os.environ.get("STORAGE", "db").lower() == "db":
    _tmp_db = os.path.join(_root_dir, ".tmp", "self_check.db")
    os.makedirs(os.path.dirname(_tmp_db), exist_ok=True)
    for _suffix in ("", "-wal", "-shm"):
        if os.path.exists(_tmp_db + _suffix):
            try:
                os.remove(_tmp_db + _suffix)
            except OSError:
                pass
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + _tmp_db.replace(os.sep, "/")

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
    if os.environ.get("STORAGE", "db").lower() == "db":
        # DB 模式：临时库需要先建表并导入菜谱，再读（JSON 模式无需这一步）
        from recipe_planner.db import _load_json_db
        from recipe_planner.storage import sync_bridge
        from recipe_planner.storage.engine import create_all
        from recipe_planner.storage.repositories import RecipeRepo

        sync_bridge.run(create_all())
        sync_bridge.run(RecipeRepo.upsert_many(_load_json_db().recipes))
        print("[存储后端] STORAGE=db（临时库）")
    else:
        print("[存储后端] STORAGE=json")
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
    # 偏好事件是第 4 个存储（docs/12 阶段二）：下面这些 set_feedback 会写事件，
    # 不指到 .tmp 就会落到真实的 data/dish_events.json
    os.environ["RECIPE_EVENTS_FILE"] = str(tmp_dir / "events.json")
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
    # 注意：**不要**在这里 pop `RECIPE_EVENTS_FILE` —— 它是开头一次性设好的（全程有效）。
    # 撤掉它之后，后面改方案的段落会写事件到真实的 data/dish_events.json（实测发生过）。

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
    expected_cost = round(sum(db.by_id(d.recipe_id).cost_yuan * cc.people / 2.0
                              for p in res.days for d in p.dishes
                              if db.by_id(d.recipe_id)), 2)
    # 口径：总费用 = 菜单里每道菜的成本 × 人数 / 2（菜谱是 2 人份基准）。
    # 原来是 `!= old_cost or True` —— 恒真，等于没检查（docs/11 §3.3 质量问题 2）。
    check("刷新后费用已重算（等于按菜单重算的值）",
          res.estimated_cost_yuan == expected_cost,
          f"{old_cost} → {res.estimated_cost_yuan}，按菜单重算得 {expected_cost}")
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

    print("[11] 方案存档 store（关掉页面明天再来，这周还是我的那一周）")
    from datetime import date as _date  # noqa: E402
    from recipe_planner import store  # noqa: E402
    plan_dir = _root / ".tmp" / "self_check_plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    tmp_plans = plan_dir / "plans.json"
    if tmp_plans.exists():
        tmp_plans.unlink()
    os.environ["RECIPE_PLAN_FILE"] = str(tmp_plans)

    check("初始状态无存档", store.latest_record() is None)
    cc_s = UserConstraints(people=2, days=3, dishes_per_day=2, spice_level="不辣")
    res_s = run_pipeline(cc_s, db)
    r1 = store.save_plan(res_s, start_date="2026-08-12", change_note="首次生成")
    got = store.latest_record()
    check("落盘后能读回来", got is not None and got.id == r1.id)
    check("周期标签为 8/12–8/18", r1.label == "8/12–8/18", r1.label)
    check("存档里带着完整菜单（3 天）", got is not None and len(got.result.days) == 3)
    check("存档里带着买菜清单", got is not None and len(got.result.shopping) > 0)

    r2 = store.save_plan(res_s, start_date="2026-08-19", change_note="重新排了一版")
    check("重排是追加而不是覆盖", len(store.load_records()) == 2)
    check("最近一条是最新那版", store.latest_record().id == r2.id)
    check("能取到上一版", (store.previous_record(r2.id) or r2).id == r1.id)
    check("最新一版没有更早的版本", store.previous_record(r1.id) is None)

    upd = res_s.model_copy(deep=True)
    upd.days[0].dishes = upd.days[0].dishes[:-1]
    store.update_result(r2.id, upd, "换了一道菜")
    check("就地更新写回存档（换一道后刷新不丢）",
          len(store.get_record(r2.id).result.days[0].dishes) == len(res_s.days[0].dishes) - 1)
    check("更新不会连累其他版本",
          len(store.get_record(r1.id).result.days[0].dishes) == len(res_s.days[0].dishes))

    store.save_plan(res_s, start_date="2026-08-26")
    store.save_plan(res_s, start_date="2026-09-02")
    if os.environ.get("STORAGE", "db").lower() == "db":
        # DB 后端按「保留 12 周」策略归档（决策 3），不截断到 3 份；文件级容错也不适用
        check("数据库后端：多份方案都能读回", len(store.load_records()) >= 3,
              f"records={len(store.load_records())}")
        check("数据库后端：最新一份仍是最新的", store.latest_record() is not None)
    else:
        check(f"存档只保留最近 {store.MAX_PLANS} 份",
              len(store.load_records()) == store.MAX_PLANS)
        check("存档文件写在指定路径", tmp_plans.exists() and tmp_plans.stat().st_size > 100)

        # 留档文件会在多次运行之间累积，所以只比对"这一次新产生的那一个"
        broken_before = set(plan_dir.glob("plans.json.broken-*"))
        tmp_plans.write_text("{ 这不是合法 json", encoding="utf-8")
        try:
            store.load_records()
            check("坏掉的存档必须报错（不能当成空存档）", False, "居然被读成了空存档")
        except store.ArchiveBroken as exc:
            check("坏掉的存档必须报错（不能当成空存档）", True, str(exc)[:40])
        new_broken = sorted(set(plan_dir.glob("plans.json.broken-*")) - broken_before)
        check("坏文件被原样留档（下一个保存覆盖不到它）",
              len(new_broken) == 1
              and new_broken[0].read_text(encoding="utf-8") == "{ 这不是合法 json",
              str([b.name for b in new_broken]))
        tmp_plans.write_text('{"version":1,"plans":[{"id":"x"}]}', encoding="utf-8")
        try:
            store.load_records()
            check("缺字段的单条也要报错（照旧写回去就等于删掉那份方案）", False, "居然被读成了空存档")
        except store.ArchiveBroken as exc:
            check("缺字段的单条也要报错（照旧写回去就等于删掉那份方案）", True, str(exc)[:40])
    os.environ.pop("RECIPE_PLAN_FILE", None)

    check("周期标签能跨月", store.week_label("2026-08-31") == "8/31–9/6", store.week_label("2026-08-31"))
    check("默认从下周一开始（周六生成→下周一）",
          store.next_monday(_date(2026, 8, 8)).isoformat() == "2026-08-10")
    check("默认从下周一开始（周一生成→下周一，不排今天）",
          store.next_monday(_date(2026, 8, 10)).isoformat() == "2026-08-17")
    back = store.inputs_from_constraints(res_s.constraints, "2026-08-12")
    check("回访能还原需求（人数/天数/开始日期）",
          back["people"] == 2 and back["days"] == 3 and back["start_date"] == "2026-08-12", str(back))
    check("E-01 今天在周期内 → 返回第几天（8/12 是第 3 天）",
          store.today_index("2026-08-12", 3, today=_date(2026, 8, 14)) == 2)
    check("E-01 今天在周期之前 → None",
          store.today_index("2026-08-12", 3, today=_date(2026, 8, 10)) is None)
    check("E-01 今天在周期之后 → None",
          store.today_index("2026-08-12", 3, today=_date(2026, 8, 20)) is None)

    print("[12] 报告：人话摘要 + 清单导出 / 可打印")
    from recipe_planner import reporting as rep  # noqa: E402
    cc_r = UserConstraints(people=4, days=2, dishes_per_day=2, spice_level="不辣",
                           budget_per_person_day=30.0, goal="减脂",
                           pantry_items=["鸡蛋"], liked_dishes=["r01"])
    res_r = run_pipeline(cc_r, db)
    sm = rep.plan_summary(res_r, db, "2026-08-12")
    check("摘要：天数与菜数正确",
          sm.days == 2 and sm.dishes == sum(len(p.dishes) for p in res_r.days))
    check("摘要：总花费与菜单一致", abs(sm.total_cost - res_r.estimated_cost_yuan) < 0.01,
          f"{sm.total_cost} vs {res_r.estimated_cost_yuan}")
    check("摘要：周预算=每人每天×人数×天数", sm.budget_total == 30.0 * 4 * 2, str(sm.budget_total))
    check("摘要：最费时的一天确实是天数里最长的",
          sm.hardest_minutes == max(rep.day_minutes(p, db) for p in res_r.days),
          f"{sm.hardest_minutes}")
    check("摘要：收藏命中数正确",
          sm.liked_hit == sum(1 for p in res_r.days for d in p.dishes
                              if d.recipe_id in set(cc_r.liked_dishes)))
    check("摘要：给出每天的日期与星期（便于贴冰箱）",
          len(sm.rows) == 2 and sm.rows[0].weekday == "周三" and sm.rows[0].date_label == "8/12",
          f"{sm.rows[0].weekday if sm.rows else None}")

    soup = next(r for r in db.recipes if r.category == "汤")
    cold = next(r for r in db.recipes if r.category == "凉菜")
    quick = next(r for r in db.recipes if r.category == "热菜" and r.time_min <= 20)
    order_plan = DayPlan(day=1, dishes=[ChosenDish(recipe_id=quick.id),
                                        ChosenDish(recipe_id=cold.id),
                                        ChosenDish(recipe_id=soup.id)])
    lines, has_slow = rep.cook_order(order_plan, db)
    check("下锅顺序：汤/炖菜先上火", len(lines) == 3 and soup.name in lines[0], str(lines))
    check("下锅顺序：凉菜最后拌", cold.name in lines[-1], str(lines))
    check("有汤时说明可以并行（不只报一个总数）", has_slow is True)

    rows = rep.shopping_rows(res_r)
    csv_text = rep.shopping_csv(res_r)
    check("CSV 带 BOM（Excel 打开不乱码）", csv_text.startswith("\ufeff"))
    check("CSV 表头正确", csv_text.splitlines()[0].lstrip("\ufeff") == "分类,食材,数量,是否已买,用于",
          csv_text.splitlines()[0])
    check("CSV 行数=采购项数", len(csv_text.strip().splitlines()) == len(rows) + 1)
    check("库存已覆盖的食材不进采购清单",
          all(not any(s.name == r["食材"] and not s.needed for s in res_r.shopping) for r in rows))
    if rows:
        check("勾选后 CSV 会标注已买",
              "✅ 已买" in rep.shopping_csv(res_r, {rows[0]["食材"]}))
    check("复制文本里带勾选框（可发微信）",
          "[ ]" in rep.shopping_text(res_r) and "买菜清单" in rep.shopping_text(res_r))
    ptxt = rep.printable_text(res_r, db, "2026-08-12")
    check("打印视图含整周菜单", "一周晚餐菜单（8/12–8/18）" in ptxt, ptxt[:40])
    check("打印视图每天一行", "第 1 天" in ptxt and "第 2 天" in ptxt)
    check("打印视图含买菜清单", "买菜清单" in ptxt)
    check("花费口径写在付钱的地方", "实际以当地物价为准" in ptxt)

    html = rep.printable_html(res_r, db, "2026-08-12")
    check("A4 打印：含周菜单表格表头", "本周晚餐菜单" in html and "<table class='a4-table'>" in html,
          html[:80])
    check("A4 打印：7 列之外每天一行都在",
          all(f"<td>{w}</td>" in html for w in
              [rep.store.weekday_name("2026-08-12", i) for i in range(2)]))
    check("A4 打印：清单带手写方框", "□" in html)
    check("A4 打印：金额与预算写在页脚",
          "本周预计" in html and ("预算" in html or "未设预算" in html))
    check("A4 打印：黑白友好（不用彩色）",
          "color:#E" not in html and "#FDEEE8" not in html)
    check("E-06 结构与一句话统计", rep.structure_line(res_r, db).count("道") >= 1,
          rep.structure_line(res_r, db))

    print("[13] 生成过程：阶段反馈 + 可真的取消")
    import time as _time  # noqa: E402
    from recipe_planner.progress import PlanJob  # noqa: E402
    job = PlanJob(UserConstraints(people=2, days=2, dishes_per_day=2, spice_level="不辣"), db).start()
    check("后台任务能跑完", job.wait(60) and job.done)
    check("后台任务产出结果", job.result is not None)
    check("阶段反馈覆盖了挑菜/搭配/校验/清单",
          {"retrieve", "plan", "validate", "shopping", "answer"} <= set(job.stages_seen),
          str(job.stages_seen))
    check("结束时给出完成文案", job.stage.startswith("✅"), job.stage)

    class _SlowGraph:
        """假图：让每个阶段慢一点，用来验证「停止」真的能停下来。"""

        def stream(self, init, stream_mode="updates"):
            for node in ["retrieve", "plan", "validate", "repair", "shopping"]:
                _time.sleep(0.15)
                yield {node: {}}

    job2 = PlanJob(UserConstraints(people=2, days=1), db,
                   graph_factory=lambda _db: _SlowGraph()).start()
    _time.sleep(0.3)
    job2.cancel()
    job2.wait(10)
    check("取消后任务会停下来", job2.done)
    check("取消后不产出结果", job2.cancelled and job2.result is None,
          f"cancelled={job2.cancelled} result={job2.result}")
    check("取消后文案是「已停止」", job2.stage.startswith("⏹️"), job2.stage)

    print("[14] 说一句改需求（第一篇 E2 / 第二篇 E-03、E-05、E-02）")
    from recipe_planner import assistant  # noqa: E402

    cc_a = UserConstraints(people=2, days=3, dishes_per_day=2, spice_level="不辣",
                           budget_per_person_day=60.0, goal="随便")
    res_a = run_pipeline(cc_a, db)
    for text, action in [
        ("周二换成鱼", "swap_day"),
        ("第 3 天不做饭", "skip_day"),
        ("周三别做饭了", "skip_day"),
        ("今天 4 个人吃", "set_people"),
        ("预算改成 40 元一人一天", "set_budget"),
        ("每道菜别超过 30 分钟", "set_max_time"),
        ("帮我省点钱", "cheaper"),
        ("这周别太素", "more_protein"),
        ("太油了想吃清爽点", "more_veg"),
        ("把蒜蓉西兰花定住", "lock_dish"),
        ("我想吃红烧肉", "add_dish"),
        ("随便说点什么吧", "unknown"),
    ]:
        got = assistant.parse_intent(text, db, cc_a, res_a, 3, today_idx=1, use_llm=False)
        check(f"听得懂「{text}」→ {action}", got.action == action, f"{got} 期望 {action}")

    before = {p.day: [d.recipe_id for d in p.dishes] for p in res_a.days}
    used_now = {d.recipe_id for p in res_a.days for d in p.dishes}
    pick = next(r for r in retrieve_candidates(db, cc_a) if r.id not in used_now)
    assistant.apply_intent(assistant.Intent("swap_day", day=2, keyword=pick.name), res_a, db)
    after = {p.day: [d.recipe_id for d in p.dishes] for p in res_a.days}
    check("换某天的菜：其余天一处没动",
          all(after[d] == before[d] for d in before if d != 2), f"{before} -> {after}")
    check("指定的菜进了第 2 天", pick.id in after[2], f"{after[2]} 期望含 {pick.id}")

    cost_before = res_a.estimated_cost_yuan
    out = assistant.apply_intent(assistant.Intent("skip_day", day=3), res_a, db)
    check("这天不做饭：当天没菜且带 skipped 标记",
          res_a.days[2].skipped and not res_a.days[2].dishes)
    check("不做饭后总花费下降", res_a.estimated_cost_yuan < cost_before)
    check("回执说清「不计花费」（E1 透明）", "不计花费" in out.text, out.text)
    out = assistant.apply_intent(assistant.Intent("restore_day", day=3), res_a, db)
    check("还能改回来（恢复做饭）", not res_a.days[2].skipped and len(res_a.days[2].dishes) > 0)

    menu_before = {p.day: [d.recipe_id for d in p.dishes] for p in res_a.days}
    old_cost = res_a.estimated_cost_yuan
    out = assistant.apply_intent(assistant.Intent("set_people", value=4), res_a, db)
    check("改人数不动菜单本身",
          {p.day: [d.recipe_id for d in p.dishes] for p in res_a.days} == menu_before)
    check("改人数后花费按人数翻倍", abs(res_a.estimated_cost_yuan - old_cost * 2) < 1,
          f"{old_cost} -> {res_a.estimated_cost_yuan}")
    check("回执说明只改了份量", "4 人" in out.text and "菜单本身没动" in out.text, out.text)

    out = assistant.apply_intent(assistant.Intent("cheaper"), res_a, db)
    check("省钱模式给的是具体金额或诚实回绝",
          ("省了约" in out.text) or ("没有明显更省的换法" in out.text), out.text)

    lock_target = db.by_id(menu_before[1][0])
    out = assistant.apply_intent(assistant.Intent("lock_dish", keyword=lock_target.name), res_a, db)
    check("定住会写进必做清单并要求重排",
          out.replan and lock_target.id in res_a.constraints.must_include_recipes, out.text)
    res_locked = run_pipeline(res_a.constraints.model_copy(deep=True), db)
    locked_ids = [d.recipe_id for p in res_locked.days for d in p.dishes]
    check("定住的菜重排之后仍在菜单里", lock_target.id in locked_ids,
          f"{locked_ids} 期望含 {lock_target.id}")

    out = assistant.apply_intent(assistant.Intent("unknown"), res_a, db)
    check("听不懂时给例子而不是报错", "没太听懂" in out.text and not out.replan)

    print("[15] 收尾功能：厨艺 / 省钱 / 拆批 / 可选 / 分享视图 / 打分")
    from recipe_planner import reporting as rep2  # noqa: E402
    from recipe_planner.core import cheapest_swap  # noqa: E402
    cc_f = UserConstraints(people=2, days=3, dishes_per_day=2, spice_level="不辣",
                           goal="随便", budget_per_person_day=80.0)
    res_f = run_pipeline(cc_f, db)
    cc_new = UserConstraints(people=2, days=3, dishes_per_day=2, spice_level="辣",
                             max_time_min=90, skill="新手", goal="随便")
    cand_new = retrieve_candidates(db, cc_new)
    check("D4 新手不排「较难」的菜", all(r.difficulty != "较难" for r in cand_new),
          [r.name for r in cand_new if r.difficulty == "较难"][:3])
    cc_any = UserConstraints(people=2, days=3, max_time_min=90, spice_level="辣")
    check("D4 选「随便」时较难的菜还在",
          any(r.difficulty == "较难" for r in retrieve_candidates(db, cc_any)))

    got_save = cheapest_swap(res_f.days, db, cc_f)
    # 这份约束（预算 80/人/天、3 天每顿 2 道）里一定存在更省的换法；`is None` 也算过
    # 就是恒真断言（docs/11 §3.3 质量问题 2）—— 真要是 None，说明"省钱"这个功能坏了。
    check("E-05 省钱换菜能算出差额", got_save is not None and got_save[5] > 0,
          f"{got_save[5] if got_save else 'None'}")
    if got_save:
        # 返回 (菜单, 第几天, 哪一顿, 旧菜, 新菜, 省了多少) —— docs/10 起多了"哪一顿"
        check("E-05 省钱换菜只动一顿",
              len([p for p in res_f.days if any(
                  d.recipe_id == got_save[3].id for d in p.dishes)]) <= 1)

    rows_f = rep2.shopping_rows(res_f)
    b1, b2 = rep2.split_batches(rows_f)
    check("F1 采购拆批不丢项、不重复", len(b1) + len(b2) == len(rows_f))
    check("F1 青菜/肉放第二批", all(r["分类"] in rep2.BATCH_LATER for r in b2))
    check("F1 可选标注只给单菜小料",
          all(rep2.optional_hint(r) == "" for r in rows_f if len(r["用于"].split("、")) > 1))

    share = rep2.share_text(res_f, db, "2026-08-12")
    check("E-08 分享视图有日期与菜名", "周三" in share and "¥" in share, share[:60])
    check("E-08 分享视图没有工程词与 emoji",
          not any(w in share for w in ["候选", "校验", "兜底", "🛒", "🏠"]))
    check("E-08 分享视图不含按钮",
          "换一道" not in share and "不喜欢" not in share)

    import os as _os
    _prof_dir = _root / ".tmp" / "self_check_final"
    _prof_dir.mkdir(parents=True, exist_ok=True)
    _prof_file = _prof_dir / "profile.json"
    if _prof_file.exists():
        _prof_file.unlink()
    _os.environ["RECIPE_PROFILE_FILE"] = str(_prof_file)
    _os.environ["RECIPE_EVENTS_FILE"] = str(_prof_dir / "events.json")   # 第 4 个存储
    from recipe_planner import profile as prof2  # noqa: E402
    known2 = {r.name for r in db.recipes}
    first_name = db.by_id(res_f.days[0].dishes[0].recipe_id).name
    prof2.rate(first_name, 2, known2, source="做完了打分")
    check("E-07 打「好吃」会记进喜欢", first_name in prof2.liked_names(known2))
    check("E-07 评分本身也留档", prof2.rating_of(first_name).get("score") == 2)
    prof2.rate(first_name, 0, known2)
    check("E-07 打「下次不做」会移进不喜欢",
          first_name in prof2.disliked_names(known2) and first_name not in prof2.liked_names(known2))
    # 顺手验一下"打分真的落成了事件"（docs/12 阶段二）——这是权重的时间维度来源
    from recipe_planner import events as _ev  # noqa: E402
    _rows = _ev.load_events()
    check("打分与偏好都落成了偏好事件",
          any(e["action"] == _ev.RATE_GOOD for e in _rows)
          and any(e["action"] == _ev.LIKE for e in _rows),
          [e["action"] for e in _rows])
    _os.environ.pop("RECIPE_PROFILE_FILE", None)
    # 同上一处：`RECIPE_EVENTS_FILE` 全程有效，谁都不许 pop（见文件开头那段注释）

    print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
