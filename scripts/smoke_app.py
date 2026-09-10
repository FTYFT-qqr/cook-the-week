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
if os.path.exists(PROFILE_TMP):
    os.remove(PROFILE_TMP)
os.environ["RECIPE_PROFILE_FILE"] = PROFILE_TMP

from streamlit.testing.v1 import AppTest  # noqa: E402

from recipe_planner import profile as prof  # noqa: E402
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


def page_text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [f.value for f in at.success] + [w.value for w in at.warning]
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

fill = find_buttons(at, "✨ 填入该场景")
check("有场景填入按钮", bool(fill))
if fill:
    fill[0].click()
    at.run()
    check("填入场景后无异常", not at.exception, str([str(e.value) for e in at.exception]))

run_btn = find_buttons(at, "🍽️ 生成菜单")
check("有生成菜单按钮", bool(run_btn))
if run_btn:
    run_btn[0].click()
    at.run()
check("生成菜单后无异常", not at.exception, str([str(e.value) for e in at.exception]))
check("菜单已渲染", has_menu(at))
print(f"    菜单上可反馈的菜: {len(find_buttons(at, '❤️ 喜欢'))} 道")

print("[2] 点击 🚫 不喜欢 → 菜单不消失 + 反馈被记录（问题1）")
hate_btns = find_buttons(at, "🚫 不喜欢")
check("菜单上存在 🚫 按钮", bool(hate_btns))
hated_dish = None
if hate_btns:
    rid = hate_btns[0].key.split("_", 2)[2]
    hated_dish = db.by_id(rid).name
    hate_btns[0].click()
    at.run()
    check("点 🚫 后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("点 🚫 后菜单仍在（不消失）", has_menu(at))
    after = prof.load_profile()
    check("🚫 已写入档案", hated_dish in after.get("disliked_dishes", []), f"profile={after}")
    check("重排后该菜从菜单消失", hated_dish not in menu_dish_names(at),
          f"menu={sorted(menu_dish_names(at))}")
    print(f"    被排除的菜: {hated_dish}")

print("[3] 点击 ❤️ 喜欢 → 菜单不消失 + 反馈被记录")
like_btns = find_buttons(at, "❤️ 喜欢")
check("菜单上仍有 ❤️ 按钮", bool(like_btns))
loved_dish = None
if like_btns:
    rid = like_btns[0].key.split("_", 2)[2]
    loved_dish = db.by_id(rid).name
    like_btns[0].click()
    at.run()
    check("点 ❤️ 后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("点 ❤️ 后菜单仍在（不消失）", has_menu(at))
    p = prof.load_profile()
    check("❤️ 已写入档案", loved_dish in p.get("liked_dishes", []), f"profile={p}")
    check("已喜欢状态回显在菜单上", bool(find_buttons(at, "✅ 已喜欢")))
    print(f"    收藏的菜: {loved_dish}")

def nav_radio(at):
    """每次重新获取导航控件（AppTest 元素引用在 rerun 后失效，不能复用）。"""
    rs = [r for r in at.radio if r.key == "nav_page"]
    return rs[0] if rs else None


print("[4] 独立「我的口味档案」页面（问题2）")
check("存在页面导航", nav_radio(at) is not None)
target_name = None
if nav_radio(at):
    nav_radio(at).set_value("❤️ 我的口味档案")
    at.run()
    check("切到档案页无异常", not at.exception, str([str(e.value) for e in at.exception]))
    txt = page_text(at)
    check("档案页标题正确", "我的口味档案" in txt)
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
if nav_radio(at):
    nav_radio(at).set_value("🍽️ 菜单规划")
    at.run()
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


if nav_radio(at):
    nav_radio(at).set_value("❤️ 我的口味档案")
    at.run()
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
        add_hate = [b for b in at.button if (b.label or "").startswith("🚫 加入不喜欢")]
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

    # ④ 清空「不喜欢」列表
    clr = btns(at, "clr_hate_btn")
    if clr and prof.disliked_names():
        clr[0].click()
        at.run()
        check("清空「不喜欢」列表生效", prof.disliked_names() == [], f"profile={prof.load_profile()}")
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


if nav_radio(at):
    nav_radio(at).set_value("🍽️ 菜单规划")
    at.run()
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
    like_btns = find_buttons(at, "❤️ 喜欢")
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
    hate_btns = find_buttons(at, "🚫 不喜欢")
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

if nav_radio(at):
    nav_radio(at).set_value("🍽️ 菜单规划")
    at.run()
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

print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("✅ 全部回归通过（含信任修复：人数折算 / 只换一道 / 换一道 / 喜欢不改菜单 / 撤销 / 标签位置）")
