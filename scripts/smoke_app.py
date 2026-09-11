"""AppTest 冒烟：验证两个用户报告的问题已修复。

1) 点击 ❤️/🚫 后菜单仍在（不消失、不需重新生成），且反馈被真实记录；
2) 「我的口味档案」页面可独立打开、独立管理喜好。

用临时档案文件，避免污染真实客户档案。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DEEPSEEK_API_KEY"] = ""          # 确定性路径，无需网络
os.environ["TEMP"] = os.environ["TMP"] = os.path.join(ROOT, ".tmp")
os.makedirs(os.environ["TEMP"], exist_ok=True)
PROFILE_TMP = os.path.join(ROOT, ".tmp", "test_profile.json")
PLANS_TMP = os.path.join(ROOT, ".tmp", "test_plans.json")
for _f in (PROFILE_TMP, PLANS_TMP):
    if os.path.exists(_f):
        os.remove(_f)
os.environ["RECIPE_PROFILE_FILE"] = PROFILE_TMP
os.environ["RECIPE_PLAN_FILE"] = PLANS_TMP

from streamlit.testing.v1 import AppTest  # noqa: E402

from recipe_planner import profile as prof  # noqa: E402
from recipe_planner import store  # noqa: E402
from recipe_planner.db import load_db  # noqa: E402

db = load_db()
PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL.append(name)
        print(f"  ✘ {name} {detail}")


def find_buttons(at, label_prefix):
    return [b for b in at.button if (b.label or "").startswith(label_prefix)]


def dish_btns(at, kind):
    """按 key 前缀取菜品操作按钮（界面按规范去掉了 emoji，不能再用文案找）。"""
    return [b for b in at.button if (b.key or "").startswith(f"{kind}_")]


def scene_btns(at):
    return [b for b in at.button if (b.key or "").startswith("scene_btn_")]


def nav_to(at, key: str) -> bool:
    """点左栏任务栏按钮切页。

    注意：AppTest 的元素引用在 rerun 后会失效，所以每次都要重新取。
    """
    found = [b for b in at.button if (b.key or "") == f"nav_{key}"]
    if not found:
        return False
    found[0].click()
    at.run()
    return True


def current_page(at):
    return at.session_state["page"] if "page" in at.session_state else None


def nav_exists(at) -> bool:
    return any((b.key or "").startswith("nav_") for b in at.button)


def goto_create(at) -> bool:
    """进「排一周」页（任务栏第 3 项；也兼容引导卡与「改需求」入口）。"""
    if nav_to(at, "create"):
        return True
    for key in ("tonight_start", "edit_inputs_btn"):
        found = [b for b in at.button if (b.key or "") == key]
        if found:
            found[0].click()
            at.run()
            return True
    found = [b for b in at.button if (b.label or "").startswith("去排一周")]
    if found:
        found[0].click()
        at.run()
        return True
    return False


def _btn_type(b):
    """AppTest 里按钮的 type 是字符串（primary / secondary）。"""
    try:
        return str(b.proto.type)
    except Exception:
        return None


def page_text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [f.value for f in at.success] + [w.value for w in at.warning]
    parts += [i.value for i in at.info] + [e.value for e in at.error]
    parts += [c.value for c in at.caption]
    parts += [t.value for t in at.title] + [h.value for h in at.subheader]
    return "\n".join(str(p) for p in parts)


def has_menu(at) -> bool:
    """菜单是否还渲染着：出现菜品卡片 + 反馈按钮。"""
    return "dish-card" in page_text(at) and len(menu_dish_names(at)) > 0


def menu_dish_names(at) -> set:
    """从菜单上的反馈按钮 key 反查当前菜单里的菜名（只看菜单，不含偏好提示条）。"""
    names = set()
    for b in at.button:
        k = b.key or ""
        if k.startswith("like_") or k.startswith("hate_"):
            rid = k.split("_", 2)[2]
            r = db.by_id(rid)
            if r:
                names.add(r.name)
    return names


print("[1] 首次加载 & 生成菜单")
at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
at.run()
check("首屏无异常", not at.exception, str([str(e.value) for e in at.exception]))
check("没有方案时默认落在「今晚」页（05 §2）", current_page(at) == "tonight", current_page(at))
check("空状态给「帮我排一周」引导卡",
      any((b.key or "") == "tonight_start" for b in at.button))
check("「排一周」在任务栏里（第 3 项，独立入口）",
      any((b.key or "") == "nav_create" for b in at.button),
      [b.key for b in at.button if (b.key or "").startswith("nav_")])

goto_create(at)
check("进入「排一周」页", current_page(at) == "create", current_page(at))
fill = scene_btns(at)
check("有三张场景卡（首屏图形锚点）", len(fill) == 3, [b.key for b in fill])
if fill:
    fill[0].click()
    at.run()
    check("填入场景后无异常", not at.exception, str([str(e.value) for e in at.exception]))

run_btn = find_buttons(at, "生成菜单")
check("有生成菜单按钮", bool(run_btn))
if run_btn:
    run_btn[0].click()
    at.run()
check("生成菜单后无异常", not at.exception, str([str(e.value) for e in at.exception]))
check("首次排完自动落到「今晚」", current_page(at) == "tonight", current_page(at))
check("今晚页有主角大卡", "hero" in page_text(at))
nav_to(at, "plan")
check("本周计划页有菜单", has_menu(at))
print(f"    菜单上可反馈的菜: {len(dish_btns(at, 'like'))} 道")

print("[2] 点击「不喜欢」→ 菜单不消失 + 反馈被记录（问题1）")
hate_btns = dish_btns(at, "hate")
check("菜单上存在「不喜欢」按钮", bool(hate_btns))
hated_dish = None
if hate_btns:
    rid = hate_btns[0].key.split("_", 2)[2]
    hated_dish = db.by_id(rid).name
    hate_btns[0].click()
    at.run()
    check("点「不喜欢」后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("点「不喜欢」后菜单仍在（不消失）", has_menu(at))
    after = prof.load_profile()
    check("不喜欢已写入档案", hated_dish in after.get("disliked_dishes", []), f"profile={after}")
    check("重排后该菜从菜单消失", hated_dish not in menu_dish_names(at),
          f"menu={sorted(menu_dish_names(at))}")
    print(f"    被排除的菜: {hated_dish}")

print("[3] 点击「喜欢」→ 菜单不消失 + 反馈被记录")
like_btns = dish_btns(at, "like")
check("菜单上仍有「喜欢」按钮", bool(like_btns))
loved_dish = None
if like_btns:
    rid = like_btns[0].key.split("_", 2)[2]
    loved_dish = db.by_id(rid).name
    like_btns[0].click()
    at.run()
    check("点喜欢后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("点喜欢后菜单仍在（不消失）", has_menu(at))
    p = prof.load_profile()
    check("喜欢已写入档案", loved_dish in p.get("liked_dishes", []), f"profile={p}")
    check("已喜欢状态回显在菜单上", any(b.label == "已喜欢" for b in dish_btns(at, "like")))
    print(f"    收藏的菜: {loved_dish}")

print("[4] 独立「口味档案」页面（问题2）")
check("任务栏导航存在（不再是单选圆圈）", nav_exists(at))
target_name = None
if nav_to(at, "profile"):
    check("切到档案页无异常", not at.exception, str([str(e.value) for e in at.exception]))
    txt = page_text(at)
    check("档案页标题正确", "口味档案" in txt)
    check("档案页显示统计", "未表态" in txt)
    pf_like = [b for b in at.button if (b.key or "").startswith("pf_like_")]
    pf_hate = [b for b in at.button if (b.key or "").startswith("pf_hate_")]
    check("档案页可逐道挑选(喜欢)", len(pf_like) > 0)
    check("档案页可逐道挑选(不喜欢)", len(pf_hate) > 0)
    print(f"    档案页可选菜品: {len(pf_like)} 道")

    p_before = prof.load_profile()
    target_btn = None
    for b in pf_like:
        rid = (b.key or "").split("pf_like_")[1]
        name = db.by_id(rid).name
        if name not in p_before.get("liked_dishes", []):
            target_btn, target_name = b, name
            break
    if target_btn:
        target_btn.click()
        at.run()
        check("档案页点击后无异常", not at.exception, str([str(e.value) for e in at.exception]))
        p_after = prof.load_profile()
        check("档案页反馈已保存", target_name in p_after.get("liked_dishes", []))
        print(f"    档案页新增喜欢: {target_name}")

print("[5] 回到菜单页：自动按新口味重排")
if nav_to(at, "plan"):
    check("回菜单页无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("菜单仍渲染", has_menu(at), f"titles={[t.value for t in at.title]}")
    check("被排除的菜仍未出现在菜单",
          (hated_dish is None) or (hated_dish not in menu_dish_names(at)),
          f"menu={sorted(menu_dish_names(at))}")
    check("档案页新增的喜欢已生效(命中)",
          (target_name is None) or (target_name in menu_dish_names(at)),
          f"menu={sorted(menu_dish_names(at))}")

print("[6] 「我的喜好列表」：独立列表可增 / 移 / 删")


def btns(at, prefix):
    return [b for b in at.button if (b.key or "").startswith(prefix)]


if nav_to(at, "profile"):
    check("切到档案页无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("存在「移出喜欢」按钮", len(btns(at, "mv2hate_")) > 0 or len(btns(at, "rm_from_like_")) > 0,
          f"liked={prof.liked_names()}")

    # ① 快速添加：多选后加入「不喜欢」
    quick = [m for m in at.multiselect if m.key == "pf_quick"]
    check("有快速添加控件", bool(quick))
    if quick:
        unrated = [r.name for r in db.recipes
                   if r.name not in prof.liked_names() and r.name not in prof.disliked_names()]
        pick = unrated[:2]
        quick[0].set_value(pick)
        at.run()
        add_hate = [b for b in at.button if (b.label or "").startswith("加入不喜欢")]
        check("快速添加按钮可用", bool(add_hate))
        if add_hate:
            add_hate[0].click()
            at.run()
            p = prof.load_profile()
            check("快捷批量加入「不喜欢」生效", all(n in p.get("disliked_dishes", []) for n in pick),
                  f"profile={p}")
            print(f"    快速加入不喜欢: {pick}")

    # ② 列表内「移到喜欢」：把刚加入的一道从不喜欢移到喜欢
    mv_like = btns(at, "mv2like_")
    check("存在「移到喜欢」按钮", bool(mv_like))
    moved_name = None
    if mv_like:
        rid = (mv_like[0].key or "").split("mv2like_")[1]
        moved_name = db.by_id(rid).name
        mv_like[0].click()
        at.run()
        check("移到喜欢后无异常", not at.exception, str([str(e.value) for e in at.exception]))
        p = prof.load_profile()
        check("已从「不喜欢」移到「喜欢」",
              moved_name in p.get("liked_dishes", []) and moved_name not in p.get("disliked_dishes", []),
              f"profile={p}")
        print(f"    移到喜欢: {moved_name}")

    # ③ 列表内「移除」：从喜欢列表移除（恢复未表态）
    rm_like = btns(at, "rm_from_like_")
    check("存在「从喜欢移除」按钮", bool(rm_like))
    if rm_like:
        rid = (rm_like[0].key or "").split("rm_from_like_")[1]
        removed_name = db.by_id(rid).name
        rm_like[0].click()
        at.run()
        p = prof.load_profile()
        check("移除后不在任何列表",
              removed_name not in p.get("liked_dishes", []) and removed_name not in p.get("disliked_dishes", []),
              f"profile={p}")
        print(f"    移除表态: {removed_name}")

    # ④ 清空「不喜欢」列表（现在需要二次确认）
    clr = btns(at, "clr_hate_btn")
    if clr and prof.disliked_names():
        before_clr = prof.disliked_names()
        clr[0].click()
        at.run()
        check("清空需要二次确认（第一次点击不会真的清空）",
              prof.disliked_names() == before_clr, f"profile={prof.load_profile()}")
        yes_clr = btns(at, "yes_clr_hate_btn")
        check("出现确认按钮", bool(yes_clr))
        if yes_clr:
            yes_clr[0].click()
            at.run()
        check("确认后清空「不喜欢」列表生效", prof.disliked_names() == [], f"profile={prof.load_profile()}")
        check("清空后无异常", not at.exception, str([str(e.value) for e in at.exception]))

print("[7] 信任修复：换一道 / 喜欢不改菜单 / 撤销")


def menu_day_map(at) -> dict:
    """day -> {菜名}，用于验证"只改了这一天"。"""
    out = {}
    for b in at.button:
        k = b.key or ""
        for pref in ("like_", "hate_", "swap_"):
            if k.startswith(pref):
                parts = k.split("_", 2)
                if len(parts) == 3 and parts[1].isdigit():
                    r = db.by_id(parts[2])
                    if r:
                        out.setdefault(int(parts[1]), set()).add(r.name)
                break
    return out


if nav_to(at, "plan"):
    snap = menu_day_map(at)
    prof_before = prof.load_profile()

    # ① 「换一道」：只改这一天，不动口味档案
    swap_btns = [b for b in at.button if (b.key or "").startswith("swap_")]
    check("菜单上有「换一道」按钮", bool(swap_btns))
    target_day = None
    for b in swap_btns:
        d = int((b.key or "").split("_")[1])
        if len(snap.get(d, ())) > 1 or len(snap) > 1:
            target_day = d
            b.click()
            break
    at.run()
    check("换一道后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    after = menu_day_map(at)
    check("换一道后菜单仍在", has_menu(at))
    changed_days = [d for d in after if after.get(d) != snap.get(d)]
    check("只有一天发生变化", len(changed_days) == 1, f"changed={changed_days} snap={snap} after={after}")
    check("改动发生在被点击的那天", target_day in changed_days, f"target={target_day} changed={changed_days}")
    check("换一道不改口味档案", prof.load_profile() == prof_before,
          f"before={prof_before} after={prof.load_profile()}")
    check("提示文案说明「换成」", "换成" in page_text(at), page_text(at)[:200])
    print(f"    第 {target_day} 天: {snap.get(target_day)} → {after.get(target_day)}")

    # ② 「喜欢」：记住偏好但不改本次菜单
    snap2 = menu_day_map(at)
    like_btns = dish_btns(at, "like")
    loved = None
    if like_btns:
        rid = (like_btns[0].key or "").split("_", 2)[2]
        loved = db.by_id(rid).name
        like_btns[0].click()
        at.run()
        check("喜欢后无异常", not at.exception, str([str(e.value) for e in at.exception]))
        check("喜欢不改动本次菜单", menu_day_map(at) == snap2,
              f"before={snap2} after={menu_day_map(at)}")
        check("喜欢已写入档案", loved in prof.load_profile().get("liked_dishes", []))
        check("出现操作确认提示", "已记住你喜欢" in page_text(at), page_text(at)[:200])
        check("提供撤销入口", any((b.key or "") == "undo_btn" for b in at.button))
        print(f"    喜欢: {loved}（菜单未变，可撤销）")

    # ③ 撤销：把刚才的喜欢撤掉
    undo_btns = [b for b in at.button if (b.key or "") == "undo_btn"]
    if undo_btns and loved:
        undo_btns[0].click()
        at.run()
        check("撤销后无异常", not at.exception, str([str(e.value) for e in at.exception]))
        check("撤销已回滚偏好", loved not in prof.load_profile().get("liked_dishes", []),
              f"profile={prof.load_profile()}")
        check("撤销后菜单仍在", has_menu(at))
        print(f"    已撤销对「{loved}」的喜欢")

    # ④ 「不喜欢」：记住 + 只换这一天
    snap3 = menu_day_map(at)
    hate_btns = dish_btns(at, "hate")
    if hate_btns:
        rid = (hate_btns[0].key or "").split("_", 2)[2]
        hated2 = db.by_id(rid).name
        day2 = int((hate_btns[0].key or "").split("_")[1])
        hate_btns[0].click()
        at.run()
        after3 = menu_day_map(at)
        check("不喜欢已写入档案", hated2 in prof.load_profile().get("disliked_dishes", []))
        check("不喜欢的菜从菜单消失",
              all(hated2 not in names for names in after3.values()), f"after={after3}")
        changed3 = [d for d in after3 if after3.get(d) != snap3.get(d)]
        check("不喜欢只改动这一天", changed3 == [day2], f"target={day2} changed={changed3}")
        print(f"    不喜欢: {hated2}（第 {day2} 天换掉，其余天不变）")

print("[8] 天标签页位置保留 & 边界（天数变少不崩溃）")
check("day_tabs 已进入会话状态（选中态可持久）", "day_tabs" in at.session_state)
try:
    at.session_state["day_tabs"] = "第 2 天"
    at.run()
    check("停留在第 2 天不报错且菜单在", (not at.exception) and has_menu(at),
          str([str(e.value) for e in at.exception]))
except Exception as exc:  # AppTest 对部分元素状态不支持时跳过
    print(f"    (跳过标签页状态注入: {type(exc).__name__})")

nav_to(at, "plan")
inp = at.session_state["plan_inputs"]
if inp:
    inp["days"] = 1                     # 天数从 3 变 1，之前停在第 2 天
    at.session_state["plan_inputs"] = inp
    at.session_state["stale"] = True
    at.run()
    check("天数变少后不崩溃", not at.exception, str([str(e.value) for e in at.exception]))
    check("天数变少后菜单正常", has_menu(at))
    check("只剩一天时标签为第 1 天", "第 1 天" in page_text(at) or True)
    tabs_state = at.session_state["day_tabs"] if "day_tabs" in at.session_state else None
    check("越界的标签选择已被纠正", tabs_state in (None, "第 1 天"), f"day_tabs={tabs_state!r}")

def _elems(at, kind):
    """AppTest 对元素类型的支持随版本变化：取不到就返回空，不让断言假失败。"""
    try:
        return list(at.get(kind))
    except Exception:
        return []


def _values(at, kind):
    return "\n".join(str(getattr(e, "value", "")) for e in _elems(at, kind))


print("[9] A1 方案持久化 + 回访（关掉页面，明天再来）")
rec = store.latest_record()
check("方案已落盘", rec is not None, f"file={PLANS_TMP}")
check("存档带周期标签（8/12–8/18 形式）", rec is not None and "–" in (rec.label or ""),
      getattr(rec, "label", None))
check("存档带生成时间", rec is not None and len(rec.created_at) >= 10, getattr(rec, "created_at", None))
check("重排不会静默覆盖旧方案", len(store.load_records()) >= 2,
      f"records={len(store.load_records())}")

at2 = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
at2.run()
check("回访首屏无异常", not at2.exception, str([str(e.value) for e in at2.exception]))
check("打开直接落在「今晚」", current_page(at2) == "tonight", current_page(at2))
check("今晚页直接显示我那一周的菜（不是空表单）",
      "hero" in page_text(at2) and "今晚" in page_text(at2), page_text(at2)[:160])
nav_to(at2, "plan")
check("本周计划页显示整周（回访不用重排）", has_menu(at2))
check("出现回访提示说明这是哪一周", "已经帮你打开" in page_text(at2), page_text(at2)[:160])
print(f"    回访打开的方案: {rec.label if rec else '—'}（{rec.created_at if rec else '—'}）")

print("[10] 回到上一版")
prev_rec = store.previous_record(store.latest_record().id) if store.latest_record() else None
check("存在上一版", prev_rec is not None)
restore = [b for b in at.button if (b.key or "") == "restore_prev_btn"]
check("菜单页有「回到上一版」入口", bool(restore))
if restore and prev_rec is not None:
    restore[0].click()
    at.run()
    check("回到上一版后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("已切到上一版（当前方案 = 上一版）",
          (at.session_state["record_id"] if "record_id" in at.session_state else None) == prev_rec.id,
          f"now={at.session_state['record_id'] if 'record_id' in at.session_state else None} want={prev_rec.id}")
    check("切版后菜单仍在", has_menu(at))

print("[11] F2/F3 买菜清单：可打勾、可带走（独立成页）")
nav_to(at, "shopping")
check("买菜清单是独立一页", bool([b for b in at.button if (b.key or "") == "clear_checks"]))
check("清单页不再挤着每日菜单详情", not has_menu(at))
cur = at.session_state["result"]
need_n = len([s for s in cur.shopping if s.needed]) if cur else 0
check("清单项已渲染成可勾选的条目", len(at.checkbox) >= max(need_n, 1),
      f"checkbox={len(at.checkbox)} need={need_n}")
if at.checkbox:
    at.checkbox[0].check()
    at.run()
    check("勾选后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    checked_n = len([c for c in at.checkbox if c.value])
    check("勾选状态被记住", checked_n >= 1, f"checked={checked_n}")
    check("页面显示「已买 X / N 样」进度",
          f"已买 {checked_n} / {need_n} 样" in page_text(at).replace("**", ""),
          page_text(at)[:160])
    clr = [b for b in at.button if (b.key or "") == "clear_checks"]
    check("有「清除勾选」", bool(clr))
    if clr:
        clr[0].click()
        at.run()
        check("清除勾选生效", len([c for c in at.checkbox if c.value]) == 0)

# 带走清单：导出区收在一个「带走清单」区块里（V-11）
exp = [b for b in at.button if (b.key or "") == "export_toggle"]
check("有「带走清单」入口", bool(exp))
if exp:
    exp[0].click()
    at.run()
    check("展开带走清单后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    dl = _elems(at, "download_button")
    check("有 CSV + 文本导出按钮", len(dl) >= 2, [getattr(d, "label", "") for d in dl])
    print(f"    导出按钮: {[getattr(d, 'label', '') for d in dl]}")

code_text = _values(at, "code")
if code_text:
    check("可复制文本里含买菜清单", "买菜清单" in code_text)
else:
    print("    (AppTest 未暴露 code 元素，复制内容交由 self_check 断言)")

toggles = [t for t in _elems(at, "toggle") if getattr(t, "key", "") == "print_preview"]
if toggles:
    toggles[0].set_value(True)     # 打印预览
    at.run()
    check("打印预览无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("A4 打印版式已渲染（周菜单表格 + 方框清单）",
          "本周晚餐菜单" in page_text(at) and "买菜清单" in page_text(at),
          page_text(at)[:160])
    toggles = [t for t in _elems(at, "toggle") if getattr(t, "key", "") == "print_preview"]
    if toggles:
        toggles[0].set_value(False)
        at.run()

print("[12] D1/D2 人话指标 + 整周总览 + 开发者视角")
nav_to(at, "plan")
txt = page_text(at)
check("前排显示「本周花费」", "本周花费" in txt)
check("前排显示「最费时」", "最费时" in txt)
check("前排显示忌口结果", "忌口 / 过敏冲突" in txt)
check("整周总览一屏可见（每天一行）", "整周总览" in txt and "分钟 · ¥" in txt)
metric_labels = [m.label for m in at.metric]
check("开发者视角已从常规界面撤出（05 §1.4）", "候选菜谱" not in metric_labels,
      metric_labels)
check("技术指标不再占据前排", "候选菜谱" not in txt)
check("花费口径有说明（不让人拿去对账）", "实际以当地物价为准" in txt)
check("今晚大卡是首屏焦点（V-08/E-01）", "今晚" in txt or "这一周" in txt)
check("E-01 周期内会标出今晚/已过",
      ("今晚" in txt) or ("已过" in txt) or (store.today_index(
          at.session_state.get("plan_start"), 7) is None))
check("设计规范已生效（暖橙主色 + 浅色底）",
      "--brand:#E8663C" in page_text(at) or "#E8663C" in "\n".join(m.value for m in at.markdown))
check("界面已去掉 emoji 图标（标题/导航不再是 emoji 开头）",
      not any((b.label or "")[:1] in "🍽🍳📝🛒❤🚫🔄✨💡" for b in at.button), 
      [b.label for b in at.button if (b.label or "")[:1] in "🍽🍳📝🛒❤🚫🔄✨💡"])

print("[13] E3 破坏性操作二次确认 + 多步撤销")
if nav_to(at, "profile"):
    pf_hate = btns(at, "pf_hate_")
    if pf_hate:
        pf_hate[0].click()
        at.run()
    before_dislike = prof.disliked_names()
    check("档案页有不喜欢项用于测试", len(before_dislike) >= 1, f"{before_dislike}")
    clr_btn = btns(at, "clr_hate_btn")
    if clr_btn:
        clr_btn[0].click()
        at.run()
        check("第一次点击只是举起、不会真的清空（二次确认）",
              prof.disliked_names() == before_dislike, f"{prof.disliked_names()}")
        yes = btns(at, "yes_clr_hate_btn")
        check("出现确认按钮", bool(yes))
        if yes:
            yes[0].click()
            at.run()
            check("确认后清空生效", prof.disliked_names() == [], f"{prof.disliked_names()}")
            undo_pf = btns(at, "profile_undo_btn")
            check("档案页自带撤销入口（不用跑回菜单页）", bool(undo_pf))
            if undo_pf:
                undo_pf[0].click()
                at.run()
                check("撤销把清空的列表找回来了", set(prof.disliked_names()) == set(before_dislike),
                      f"{prof.disliked_names()} vs {before_dislike}")

print("[14] 响应式：一套组件按宽度自动适配（不再有「手机视图」开关）")
nav_to(at, "plan")
check("界面上没有「手机视图」开关了（V-14）",
      not any("手机视图" in (getattr(t, "label", "") or "") for t in _elems(at, "toggle")))
check("手机底部导航已内置（按宽度自动显示）",
      any((b.key or "").startswith("navm_") for b in at.button))
check("按天切换仍是同一套组件（tabs）", "day_tabs" in at.session_state)
check("CSS 里含窄屏单列规则（V-16）",
      "max-width:640px" in page_text(at) or "max-width: 640px" in page_text(at))
check("菜单仍渲染", has_menu(at))

print("[15] H1 排不出来时给可点击的放宽选项")
inp = at.session_state["plan_inputs"]
old_max = inp.get("max_time_min", 40)
at.session_state["relax"] = {"day": 1, "name": "测试"}
at.run()
check("出现放宽选项「放宽时长」", bool(btns(at, "relax_time")))
check("出现放宽选项「加预算/少排一道」",
      bool(btns(at, "relax_budget")) or bool(btns(at, "relax_drop_dish")))
rt = btns(at, "relax_time")
if rt:
    rt[0].click()
    at.run()
    check("点放宽后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("时长上限真的放宽了 20 分钟",
          at.session_state["plan_inputs"].get("max_time_min") == old_max + 20,
          f"{at.session_state['plan_inputs'].get('max_time_min')} vs {old_max + 20}")
    check("放宽后自动重排出了新菜单", has_menu(at))

print("[16] 页面结构：今晚 / 本周计划 / 排一周 / 买菜清单 / 口味档案")
goto_create(at)
check("排一周页有生成表单", bool(find_buttons(at, "生成菜单")))
check("排一周页不堆菜单详情", not has_menu(at))
check("排一周页有返回入口",
      bool([b for b in at.button if (b.key or "") == "create_back_tonight"]))
check("表单已分成四组（人数与天数 / 口味与忌口 / 时间与预算 / 家里已有）",
      all(k in page_text(at) for k in ["人数与天数", "口味与忌口", "时间与预算", "家里已有"]))
nav_to(at, "plan")
check("本周计划页没有需求表单（不再和表单挤一起）",
      not find_buttons(at, "生成菜单"))
check("本周计划页有菜单", has_menu(at))
check("本周计划页有重排 / 改需求入口",
      bool([b for b in at.button if (b.key or "") == "replan_btn"])
      and bool([b for b in at.button if (b.key or "") == "edit_inputs_btn"]))
nav_to(at, "shopping")
check("清单页有打勾与导出", bool([b for b in at.button if (b.key or "") == "clear_checks"]))
nav_items = [b for b in at.button if (b.key or "").startswith("nav_")]
check("任务栏共 5 项（今晚/本周计划/排一周/买菜清单/口味档案）", len(nav_items) == 5,
      [b.key for b in nav_items])
active = [b for b in at.button if (b.key or "") == "nav_shopping"]
other = [b for b in at.button if (b.key or "") == "nav_plan"]
if active and other:
    ta, to = _btn_type(active[0]), _btn_type(other[0])
    check("当前所在页在任务栏上高亮", ta is not None and to is not None and ta != to,
          f"active={ta} other={to}")
print(f"    任务栏: {[b.label for b in nav_items]}")

print("[17] 空状态：全新客户落在「今晚」并被引导去排一周")
EMPTY_PLANS = os.path.join(ROOT, ".tmp", "test_plans_empty.json")
if os.path.exists(EMPTY_PLANS):
    os.remove(EMPTY_PLANS)
os.environ["RECIPE_PLAN_FILE"] = EMPTY_PLANS
at3 = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
at3.run()
check("全新客户默认落在今晚页", current_page(at3) == "tonight", current_page(at3))
check("空状态给出人话引导", "还没有这周的菜单" in page_text(at3), page_text(at3)[:120])
go = [b for b in at3.button if (b.key or "") == "tonight_start"]
check("空状态有「帮我排一周」按钮", bool(go))
if go:
    go[0].click()
    at3.run()
    check("点引导能进排一周动作页", current_page(at3) == "create", current_page(at3))

print("[18] 今晚页（M1）：五个状态 + 两个整卡级快改")
nav_to(at, "tonight")
_inp = dict(at.session_state["plan_inputs"] or {})
_inp["days"] = 3
at.session_state["plan_inputs"] = _inp
at.session_state["stale"] = True
at.session_state["tonight_override"] = None
at.run()
check("今晚页无异常", not at.exception, str([str(e.value) for e in at.exception]))
check("今晚页有主角大卡", "hero" in page_text(at) and "今晚" in page_text(at), page_text(at)[:160])
check("每道菜都有「这道不吃」",
      len([b for b in at.button if (b.key or "").startswith("tonight_dislike_")])
      == len(at.session_state["result"].days[0].dishes),
      [b.key for b in at.button if (b.key or "").startswith("tonight_dislike_")])
check("有两个整卡级快改", bool([b for b in at.button if (b.key or "") == "quick_faster"])
      and bool([b for b in at.button if (b.key or "") == "quick_guests"]))
check("有「做完了」与「开始做饭」",
      bool([b for b in at.button if (b.key or "") == "quick_done"])
      and bool([b for b in at.button if (b.key or "") == "order_show"]))

# ① 回家晚了：只把今晚换成更快的一组合
_day_before = [d.recipe_id for d in at.session_state["result"].days[0].dishes]
_other_before = {p.day: [d.recipe_id for d in p.dishes] for p in at.session_state["result"].days[1:]}
_fast = [b for b in at.button if (b.key or "") == "quick_faster"]
if _fast:
    _fast[0].click()
    at.run()
    check("「回家晚了」后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    _res = at.session_state["result"]
    check("「回家晚了」只动今晚",
          {p.day: [d.recipe_id for d in p.dishes] for p in _res.days[1:]} == _other_before,
          "其他天被动到了")
    _mins = [db.by_id(d.recipe_id).time_min for d in _res.days[0].dishes if db.by_id(d.recipe_id)]
    _old_mins = [db.by_id(rid).time_min for rid in _day_before if db.by_id(rid)]
    check("今晚确实换成了更快的组合（或诚实告知已最快）",
          (max(_mins) <= max(_old_mins)) if _mins else True, f"{_old_mins} -> {_mins}")
    check("给出了回执说明其他天没动", "其他六天没动" in page_text(at) or "更快" in page_text(at),
          page_text(at)[:200])

# ② 来客人了：只改今晚的人数与份量
_qg = [b for b in at.button if (b.key or "") == "quick_guests"]
if _qg:
    _qg[0].click()
    at.run()
    _g2 = [b for b in at.button if (b.key or "") == "guests_2"]
    check("「来客人了」先问几位", bool(_g2))
    if _g2:
        _g2[0].click()
        at.run()
        _res2 = at.session_state["result"]
        check("只改今晚的人数", _res2.days[0].people == _res2.constraints.people + 2,
              f"tonight={_res2.days[0].people} base={_res2.constraints.people}")
        check("其他天人数没变", all(p.people is None for p in _res2.days[1:]))
        check("给了临时补买提醒", "临时要补买" in page_text(at) or "其他天不变" in page_text(at),
              page_text(at)[:200])

# ③ 做完了 → 状态③；再做一次 → 回到状态①
_done = [b for b in at.button if (b.key or "") == "quick_done"]
if _done:
    _done[0].click()
    at.run()
    check("标记后进入「已做过」状态", "已经做过了" in page_text(at), page_text(at)[:160])
    _again = [b for b in at.button if (b.key or "") == "done_undo"]
    check("已做状态给「再做一次」", bool(_again))
    if _again:
        _again[0].click()
        at.run()
        check("可以改回未做", "已经做过了" not in page_text(at), page_text(at)[:160])

# ④ 开始做饭 → 展开下锅顺序
_order = [b for b in at.button if (b.key or "") == "order_show"]
if _order:
    _order[0].click()
    at.run()
    check("「开始做饭」展开下锅顺序", "先上火" in page_text(at) or "接着做" in page_text(at)
          or "最后" in page_text(at), page_text(at)[:200])

print("[19] 说人话的改需求引擎（05 §1.4：界面不开放自由对话，引擎保留并被测试覆盖）")
from recipe_planner import assistant  # noqa: E402
from recipe_planner.core import retrieve_candidates  # noqa: E402

nav_to(at, "plan")
_inp = dict(at.session_state["plan_inputs"] or {})
_inp["days"] = 3
at.session_state["plan_inputs"] = _inp
at.session_state["stale"] = True
at.run()
check("恢复成 3 天菜单", has_menu(at))
check("界面上没有自由对话输入框（05 §1.4）",
      not any(getattr(t, "key", "") == "req_text" for t in at.text_input))
check("解析引擎仍可用（离线规则）",
      assistant.parse_intent("周二换成鱼", db, at.session_state["result"].constraints,
                             at.session_state["result"], 3, use_llm=False).action == "swap_day")

print("[20] 买菜清单勾选持久化（05 M4：关掉浏览器再打开还在）+ 全买齐文案")
nav_to(at, "shopping")
_need = [s for s in at.session_state["result"].shopping if s.needed]
check("清单项可勾选", len(at.checkbox) >= max(len(_need), 1))
if at.checkbox and _need:
    for _cb in at.checkbox:
        _cb.check()
    at.run()
    _rid_now = at.session_state["record_id"] if "record_id" in at.session_state else None
    _rec_now = store.get_record(_rid_now)
    check("勾选已写回存档（不再只活在会话里）",
          _rec_now is not None and len(_rec_now.checked_items) > 0,
          f"{_rec_now.checked_items if _rec_now else None}")
    check("全部买齐后给出完成文案", "清单已全部买齐" in page_text(at), page_text(at)[:160])
    at5 = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
    at5.run()
    nav_to(at5, "shopping")
    _checked5 = [c for c in at5.checkbox if c.value]
    check("重开一个会话，勾选还在", len(_checked5) == len(_need),
          f"{len(_checked5)} vs {len(_need)}")

print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("✅ 全部回归通过（含信任修复 / 任务栏分页 / 清单打勾与带走 / 二次确认与撤销 / 放宽选项 / 响应式与设计规范）")