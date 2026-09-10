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

print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("✅ 两个问题均已修复并通过自动化验证")
