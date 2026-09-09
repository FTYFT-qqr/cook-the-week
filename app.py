"""Recipe-Planner · Streamlit Demo（前端可读性优先）

运行：streamlit run app.py

交互流程：
1) 先填约束（或选示例场景）点「先生成一版菜单」→ 得到每日菜单；
2) 客户可能不知道自己喜欢什么 —— 直接在生成的菜单上对每道菜点 ❤️ 喜欢 / 🚫 不喜欢；
3) 点击后立即按新偏好重新规划，并把偏好写入客户档案（data/customer_profile.json），
   下次打开自动带入 —— “越用越懂你”。
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from recipe_planner.db import load_db
from recipe_planner.graph import run_pipeline
from recipe_planner.models import (
    ALLERGENS,
    DISPLAY_CATEGORIES,
    GOALS,
    MEAL,
    SPICE_LEVELS,
    TASTE_TAGS,
    UserConstraints,
)

st.set_page_config(page_title="一周食谱规划 Agent", page_icon="🍳", layout="wide")

# ---------------------------------------------------------------- 样式
st.markdown(
    """
    <style>
    .dish-card { background:#fff; border:1px solid #e8e8e8; border-left:4px solid #ff9f43;
                 border-radius:10px; padding:12px 14px; margin:2px 0; }
    .dish-card.loved { border-left-color:#ec4899; background:#fdf2f8; }
    .dish-card.hated { border-left-color:#6b7280; background:#f9fafb; }
    .dish-name { font-size:1.02rem; font-weight:700; color:#1f1f1f; }
    .dish-meta { color:#6b7280; font-size:0.8rem; margin-top:2px; }
    .dish-reason { color:#374151; font-size:0.88rem; margin-top:6px; background:#f8f9fa;
                   border-radius:6px; padding:6px 8px; }
    .chip { display:inline-block; background:#eef2ff; color:#4338ca; border-radius:999px;
            padding:1px 8px; margin-right:4px; font-size:0.75rem; }
    .chip-red { background:#fee2e2; color:#b91c1c; }
    .chip-green { background:#dcfce7; color:#15803d; }
    .chip-pink { background:#fce7f3; color:#be185d; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- 数据 & 档案
db = load_db()
ID2NAME = {r.id: r.name for r in db.recipes}
PROFILE_FILE = Path(__file__).resolve().parent / "data" / "customer_profile.json"


def load_profile() -> dict:
    if PROFILE_FILE.exists():
        try:
            return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_profile(profile: dict) -> None:
    PROFILE_FILE.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def liked_names() -> list[str]:
    p = load_profile()
    known = set(ID2NAME.values())
    return [n for n in p.get("liked_dishes", []) if n in known]


def disliked_names() -> list[str]:
    p = load_profile()
    known = set(ID2NAME.values())
    return [n for n in p.get("disliked_dishes", []) if n in known]


def toggle_feedback(recipe_id: str, action: str) -> None:
    """把对某道菜的反馈写入客户档案（名字存储，互斥：like 与 dislike 不同时存在）。"""
    p = load_profile()
    name = ID2NAME.get(recipe_id, recipe_id)
    liked = [n for n in p.get("liked_dishes", []) if n in set(ID2NAME.values())]
    disliked = [n for n in p.get("disliked_dishes", []) if n in set(ID2NAME.values())]
    if action == "like":
        if name in liked:
            liked.remove(name)          # 再点一次 = 取消喜欢
        else:
            liked.append(name)
            disliked = [n for n in disliked if n != name]
    elif action == "dislike":
        if name in disliked:
            disliked.remove(name)       # 再点一次 = 取消不喜欢
        else:
            disliked.append(name)
            liked = [n for n in liked if n != name]
    p["liked_dishes"] = liked
    p["disliked_dishes"] = disliked
    p["customer_name"] = p.get("customer_name", "默认客户")
    save_profile(p)


# ---------------------------------------------------------------- 示例场景
SCENARIOS = {
    "🍃 清淡减脂 3 天（默认示例）": dict(
        people=2, days=3, spice="不辣", goal="减脂", taste=["清淡"],
        max_time=40, budget=45.0, allergens=[], pantry=["鸡蛋", "西红柿"]),
    "🦐 海鲜过敏 + 控糖": dict(
        people=3, days=4, spice="不辣", goal="控糖", taste=["清淡"],
        max_time=35, budget=40.0, allergens=["海鲜"], pantry=[]),
    "🌶️ 无辣不欢 · 省钱 5 天": dict(
        people=2, days=5, spice="辣", goal="省钱", taste=["下饭"],
        max_time=30, budget=25.0, allergens=[], pantry=["土豆"]),
    "🥚 蛋过敏高蛋白 3 天": dict(
        people=1, days=3, spice="不辣", goal="高蛋白", taste=["咸鲜"],
        max_time=50, budget=50.0, allergens=["蛋"], pantry=[]),
}


def apply_scenario(s: dict) -> None:
    for k, v in s.items():
        st.session_state[k] = v
    # pantry 在表单里是文本框（字符串），场景里给的是列表 → 转成逗号串
    st.session_state["pantry"] = ", ".join(s.get("pantry", []))
    st.session_state["dishes_per_day"] = 2


def default_inputs() -> dict:
    s = SCENARIOS["🍃 清淡减脂 3 天（默认示例）"]
    return dict(
        people=s["people"], days=s["days"], dishes_per_day=2,
        allergens=s["allergens"], spice=s["spice"], taste_tags=s["taste"], goal=s["goal"],
        max_time_min=s["max_time"], budget_per_person_day=s["budget"],
        pantry_items=s["pantry"],
    )


if "plan_inputs" not in st.session_state:
    st.session_state["plan_inputs"] = default_inputs()

# ---------------------------------------------------------------- 顶栏
st.title("🍳 一周食谱规划 Agent")
st.caption("先填几口人、忌口、预算 → 生成一周晚餐。**看完菜单再说喜欢什么**："
           "每道菜上点 ❤️ 或 🚫，系统立刻记住并按你的口味重新安排 —— 越用越懂你。")

col_hero = st.columns([3, 1])
with col_hero[0]:
    sc = st.selectbox("🎯 快速体验一个场景（点击后自动填入下方表单）：", list(SCENARIOS))
with col_hero[1]:
    st.write("")
    st.write("")
    if st.button("✨ 填入该场景", type="secondary", use_container_width=True):
        apply_scenario(SCENARIOS[sc])
        st.rerun()

st.divider()

# ---------------------------------------------------------------- 输入表单
# 统一 widget 初值：key 只在 Session State 中初始化，widget 不再传默认值参数
for _k, _v in dict(
    people=2, days=3, spice="不辣", max_time=40, goal="随便", budget=0.0,
    allergens=[], taste=[], pantry="", dishes_per_day=2,
).items():
    st.session_state.setdefault(_k, _v)

with st.form("planner_form"):
    st.subheader("📋 告诉我你的需求")
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        people = st.number_input("👨‍👩‍👧 几人吃", 1, 10, key="people")
        days = st.slider("📅 排几天（每天 1 顿%s）" % MEAL, 1, 7, key="days")
    with col_b:
        spice = st.radio("🌶️ 能接受的辣度", SPICE_LEVELS, horizontal=True, key="spice")
        max_time = st.slider("⏱️ 单菜耗时上限（分钟）", 10, 90, key="max_time")
    with col_c:
        goal = st.selectbox("🎯 目标", GOALS, key="goal")
        budget = st.number_input("💰 预算（元/人/天，0=不限）", 0.0, 200.0, step=5.0, key="budget")

    col_d, col_e = st.columns(2)
    with col_d:
        allergens = st.multiselect("🚫 过敏原 / 忌口（硬排除）", ALLERGENS, key="allergens")
    with col_e:
        taste = st.multiselect("😋 口味偏好（尽量满足）", TASTE_TAGS, key="taste")

    pantry = st.text_input("🧺 家里已有食材（逗号分隔，会从买菜清单里扣除）",
                           placeholder="例如：鸡蛋, 土豆, 西红柿, 葱姜蒜", key="pantry")
    dishes_per_day = st.select_slider("🍽️ 每顿想几个菜", [1, 2, 3], key="dishes_per_day")
    submitted = st.form_submit_button("🍽️ 先生成一版菜单（不合口味再点菜调整）",
                                      type="primary", use_container_width=True)

if submitted:
    st.session_state["plan_inputs"] = dict(
        people=int(people), days=int(days), dishes_per_day=int(dishes_per_day),
        allergens=list(allergens), spice=spice, taste_tags=list(taste), goal=goal,
        max_time_min=int(max_time),
        budget_per_person_day=budget if budget and budget > 0 else None,
        pantry_items=[p.strip() for p in pantry.replace("，", ",").split(",") if p.strip()],
    )

_feedback_rerun = st.session_state.pop("feedback_rerun", False)
if not (submitted or _feedback_rerun):
    st.info("👆 填好需求，点「先生成一版菜单」。拿到菜单后如果哪道菜不合口味，"
            "直接在那道菜上点 🚫 —— 不用事先想好自己喜欢什么，边看边告诉我就行。")
    st.stop()

# ---------------------------------------------------------------- 组装约束（融合客户档案喜好）
inputs = dict(st.session_state["plan_inputs"])
liked_ids = liked_names()
disliked_ids = disliked_names()
# 名称→id 映射（约束用 id）
name2id = {r.name: r.id for r in db.recipes}
c = UserConstraints(
    people=inputs["people"], days=inputs["days"], dishes_per_day=inputs["dishes_per_day"],
    allergens=inputs.get("allergens", []), spice_level=inputs.get("spice", "不辣"),
    taste_tags=inputs.get("taste_tags", []), goal=inputs.get("goal", "随便"),
    max_time_min=inputs.get("max_time_min", 40),
    budget_per_person_day=inputs.get("budget_per_person_day"),
    pantry_items=inputs.get("pantry_items", []),
    liked_dishes=[name2id[n] for n in liked_ids if n in name2id],
    disliked_dishes=[name2id[n] for n in disliked_ids if n in name2id],
)

with st.spinner("🤔 正在检索菜谱、按你的喜好排菜、校验与修正…"):
    result = run_pipeline(c, db)

# ---------------------------------------------------------------- 结果概览
st.divider()
ok = result.final and not any(i.level == "error" for i in result.issues)
if ok:
    st.success(f"✅ 排菜完成！共 {sum(len(d.dishes) for d in result.days)} 道菜 / {len(result.days)} 天 · "
               f"预计总花费约 ¥{result.estimated_cost_yuan:.0f}"
               + (" · ✨ LLM 智能排菜" if result.llm_used else " · ⚙️ 确定性排菜"))
elif not result.final:
    st.error("⚠️ 部分约束无法同时满足（候选太少或预算过低），已按可行方案输出并给出提示。")
else:
    st.warning("已输出方案，但有软性问题（见下方提示），可接受或调整约束再试。")

meta = st.columns(6)
meta[0].metric("候选菜谱", f"{result.candidate_count} 道")
meta[1].metric("排了几天", f"{len(result.days)} 天")
meta[2].metric("修正轮数", f"{result.repairs_used} 次")
meta[3].metric("LLM 排菜", "✅ 是" if result.llm_used else "⬜ 否(兜底)")
meta[4].metric("耗时", f"{result.latency_sec:.1f}s")
meta[5].metric("❤️ 喜欢命中", f"{sum(1 for p in result.days for d in p.dishes if d.recipe_id in set(name2id[n] for n in liked_ids if n in name2id))} 道")
if result.llm_error:
    st.caption(f"ℹ️ LLM 状态：{result.llm_error}（已自动切换到确定性排菜，结果仍可用）")
st.caption("💬 觉得哪道菜不错？点它右边的 **❤️**（以后会多安排）；不喜欢就点 **🚫**（立即换掉）。"
           "系统会记住你的口味，下次打开自动带入。")

# ---------------------------------------------------------------- 每日菜单 + 反馈按钮
feedback_clicked = None  # (recipe_id, action)
st.subheader("🗓️ 每日菜单")
tabs = st.tabs([f"第 {i} 天" for i in range(1, len(result.days) + 1)])
for tab, plan in zip(tabs, result.days):
    with tab:
        total_day = 0.0
        for dish in plan.dishes:
            r = db.by_id(dish.recipe_id)
            if r is None:
                continue
            total_day += r.cost_yuan * c.people / 2.0
            rname = r.name
            is_loved = rname in liked_ids
            is_hated = rname in disliked_ids
            chips = [f"<span class='chip'>{r.category}</span>",
                     f"<span class='chip'>{r.spice_level}</span>",
                     f"<span class='chip'>⏱ {r.time_min}min</span>",
                     f"<span class='chip'>¥{r.cost_yuan}</span>"]
            chips += [f"<span class='chip chip-green'>{t}</span>" for t in r.goal_tags]
            chips += [f"<span class='chip chip-red'>{a}</span>" for a in r.allergens]
            if is_loved:
                chips.insert(0, "<span class='chip chip-pink'>❤️ 已收藏</span>")

            row = st.columns([4, 1, 1])
            with row[0]:
                st.markdown(
                    f"<div class='dish-card{' loved' if is_loved else (' hated' if is_hated else '')}'>"
                    f"<div class='dish-name'>{rname}</div>"
                    f"<div class='dish-meta'>{''.join(chips)}</div>"
                    f"<div class='dish-reason'>💡 {dish.reason or '—'}</div>"
                    f"</div>", unsafe_allow_html=True)
            with row[1]:
                if st.button("❤️ 喜欢" if not is_loved else "✅ 已喜欢",
                             key=f"like_{plan.day}_{dish.recipe_id}",
                             type="secondary", use_container_width=True,
                             help="合口味：以后多安排这道菜/这类菜"):
                    feedback_clicked = (dish.recipe_id, "like")
            with row[2]:
                if st.button("🚫 不喜欢" if not is_hated else "⛔ 已排除",
                             key=f"hate_{plan.day}_{dish.recipe_id}",
                             type="secondary", use_container_width=True,
                             help="不合口味：立即换掉并记住"):
                    feedback_clicked = (dish.recipe_id, "dislike")
        st.caption(f"本天预计花费 ¥{total_day:.0f}" +
                   (f" / 预算 ¥{c.budget_per_person_day * c.people:.0f}" if c.budget_per_person_day else ""))

# ---------------------------------------------------------------- 反馈后立即重排
if feedback_clicked:
    rid, action = feedback_clicked
    toggle_feedback(rid, action)
    msg = "❤️ 已记住：你喜欢这道菜，正在按新口味重新安排…" if action == "like" else \
          "🚫 已记住：这道菜不会再出现，正在重新安排…"
    st.toast(msg)
    st.session_state["feedback_rerun"] = True
    st.rerun()

# ---------------------------------------------------------------- 买菜清单
st.subheader("🛒 买菜清单")
if result.shopping:
    need = [s for s in result.shopping if s.needed]
    have = [s for s in result.shopping if not s.needed]
    cols = st.columns(3)
    for idx, cat in enumerate(DISPLAY_CATEGORIES):
        items = [s for s in need if s.category == cat]
        if not items:
            continue
        with cols[idx % 3]:
            st.markdown(f"**{cat}**")
            for it in items:
                st.markdown(f"- ✅ {it.name}（{it.amount}）")
                st.caption(f"  ↳ 用于：{'、'.join(it.for_recipes[:3])}")
    if have:
        st.caption("🏠 已有库存覆盖（无需购买）：" + "、".join(s.name for s in have[:20]))
else:
    st.warning("暂无需要采购的食材（可能库存已覆盖或未排出菜）。")

# ---------------------------------------------------------------- 校验报告 & 过程
with st.expander("🔍 查看确定性校验报告与修正过程"):
    if result.issues:
        for i in result.issues:
            icon = "❌" if i.level == "error" else "⚠️"
            st.markdown(f"- {icon} {i.message}")
    else:
        st.success("无任何校验问题 ✅")
    st.markdown("**LangGraph 执行轨迹**")
    for t in result.trace:
        st.markdown(f"- `{t}`")
    st.markdown(f"**客户档案**（data/customer_profile.json）：喜欢 {liked_ids or '—'} ｜ 不喜欢 {disliked_ids or '—'}")
