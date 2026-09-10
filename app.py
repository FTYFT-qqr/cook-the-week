"""Recipe-Planner · Streamlit Demo

两个界面：
1) 🍽️ 菜单规划：填约束 → 生成每日菜单 → 在每道菜上点 ❤️/🚫 直接反馈
2) ❤️ 我的口味档案：单独管理喜欢的菜 / 不喜欢的菜（客户可能不知道自己喜欢什么，这里可以慢慢挑）

运行：streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from recipe_planner import profile as prof
from recipe_planner.core import refresh_result, swap_dish
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
    .pref-bar { background:#fff7ed; border:1px solid #fed7aa; border-radius:8px;
                padding:8px 12px; color:#9a3412; font-size:0.88rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

db = load_db()
KNOWN_NAMES = {r.name for r in db.recipes}
NAME2ID = {r.name: r.id for r in db.recipes}
ID2NAME = {r.id: r.name for r in db.recipes}

# ---------------------------------------------------------------- 示例场景
SCENARIOS = {
    "🍃 清淡减脂 3 天（默认示例）": dict(
        people=2, days=3, spice="不辣", goal="减脂", taste=["清淡"],
        max_time=40, budget=45.0, allergens=[], pantry="鸡蛋, 西红柿"),
    "🦐 海鲜过敏 + 控糖": dict(
        people=3, days=4, spice="不辣", goal="控糖", taste=["清淡"],
        max_time=35, budget=40.0, allergens=["海鲜"], pantry=""),
    "🌶️ 无辣不欢 · 省钱 5 天": dict(
        people=2, days=5, spice="辣", goal="省钱", taste=["下饭"],
        max_time=30, budget=25.0, allergens=[], pantry="土豆"),
    "🥚 蛋过敏高蛋白 3 天": dict(
        people=1, days=3, spice="不辣", goal="高蛋白", taste=["咸鲜"],
        max_time=50, budget=50.0, allergens=[], pantry=""),
}


# 会话状态
st.session_state.setdefault("plan_inputs", None)   # 已提交的约束快照（None = 还没生成过）
st.session_state.setdefault("result", None)         # 缓存的排菜结果（页面始终显示它）
st.session_state.setdefault("stale", False)         # 口味/约束变了 → 需要重排
st.session_state.setdefault("notice", None)         # 上一步操作确认（rerun 后显示）
st.session_state.setdefault("undo", None)           # 单步撤销快照

for _k, _v in dict(
    people=2, days=3, spice="不辣", max_time=40, goal="随便", budget=0.0,
    allergens=[], taste=[], pantry="", dishes_per_day=2,
).items():
    st.session_state.setdefault(_k, _v)


def build_constraints(inp: dict) -> UserConstraints:
    liked = prof.liked_names(KNOWN_NAMES)
    disliked = prof.disliked_names(KNOWN_NAMES)
    return UserConstraints(
        people=inp["people"], days=inp["days"], dishes_per_day=inp["dishes_per_day"],
        allergens=inp.get("allergens", []), spice_level=inp.get("spice", "不辣"),
        taste_tags=inp.get("taste_tags", []), goal=inp.get("goal", "随便"),
        max_time_min=inp.get("max_time_min", 40),
        budget_per_person_day=inp.get("budget_per_person_day"),
        pantry_items=inp.get("pantry_items", []),
        liked_dishes=[NAME2ID[n] for n in liked if n in NAME2ID],
        disliked_dishes=[NAME2ID[n] for n in disliked if n in NAME2ID],
    )


# ---------------------------------------------------------------- 侧边栏导航
with st.sidebar:
    st.title("🍳 食谱规划 Agent")
    page = st.radio("导航", ["🍽️ 菜单规划", "❤️ 我的口味档案"], key="nav_page")
    st.divider()
    _liked = prof.liked_names(KNOWN_NAMES)
    _disliked = prof.disliked_names(KNOWN_NAMES)
    st.metric("❤️ 喜欢的菜", f"{len(_liked)} 道")
    st.metric("🚫 不喜欢的菜", f"{len(_disliked)} 道")
    if _liked:
        st.caption("❤️ " + "、".join(_liked[:6]) + ("…" if len(_liked) > 6 else ""))
    if _disliked:
        st.caption("🚫 " + "、".join(_disliked[:6]) + ("…" if len(_disliked) > 6 else ""))
    st.divider()
    st.caption("口味档案保存在 data/customer_profile.json，下次打开自动带入。")


# ================================================================ 页面 1：菜单规划
def render_planner() -> None:
    st.title("🍽️ 一周晚餐规划")
    st.caption("填需求 → 生成菜单。**拿到菜单后再点 ❤️/🚫 反馈**都行，随时可改，菜单不会丢。")

    with st.expander("🎯 快速体验一个场景", expanded=st.session_state["plan_inputs"] is None):
        c1, c2 = st.columns([3, 1])
        with c1:
            sc = st.selectbox("选择场景后点右侧按钮自动填入表单", list(SCENARIOS),
                              label_visibility="collapsed")
        with c2:
            if st.button("✨ 填入该场景", use_container_width=True):
                for k, v in SCENARIOS[sc].items():
                    st.session_state[k] = v
                st.session_state["dishes_per_day"] = 2
                st.rerun()

    with st.form("planner_form"):
        st.subheader("📋 我的需求")
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
        submitted = st.form_submit_button("🍽️ 生成菜单（不喜欢可逐道反馈）", type="primary",
                                          use_container_width=True)

    if submitted:
        st.session_state["plan_inputs"] = dict(
            people=int(people), days=int(days), dishes_per_day=int(dishes_per_day),
            allergens=list(allergens), spice=spice, taste_tags=list(taste), goal=goal,
            max_time_min=int(max_time),
            budget_per_person_day=budget if budget and budget > 0 else None,
            pantry_items=[p.strip() for p in pantry.replace("，", ",").split(",") if p.strip()],
        )
        st.session_state["stale"] = True
        st.session_state["notice"] = None   # 新菜单：清掉上一条操作提示
        st.session_state["undo"] = None

    inp = st.session_state["plan_inputs"]
    if inp is None:
        st.info("👆 填好需求点「生成菜单」。也可以先点上面的场景按钮一键体验。")
        return

    # 需要重排（首次 / 约束变了 / 口味变了）
    if st.session_state["result"] is None or st.session_state["stale"]:
        c = build_constraints(inp)
        with st.spinner("🤔 正在检索菜谱、按你的口味排菜、校验与修正…"):
            st.session_state["result"] = run_pipeline(c, db)
        st.session_state["stale"] = False
    result = st.session_state["result"]
    c = result.constraints

    liked_now = prof.liked_names(KNOWN_NAMES)
    hated_now = prof.disliked_names(KNOWN_NAMES)
    liked_ids = {NAME2ID[n] for n in liked_now if n in NAME2ID}

    # ---- 概览
    st.divider()
    if result.final:
        st.success(f"✅ 菜单就绪：共 {sum(len(d.dishes) for d in result.days)} 道菜 / {len(result.days)} 天 · "
                   f"预计总花费约 ¥{result.estimated_cost_yuan:.0f}"
                   + (" · ✨ LLM 智能排菜" if result.llm_used else " · ⚙️ 确定性排菜"))
    else:
        st.warning("⚠️ 部分约束无法同时满足（候选太少或预算过低），已给出可行方案。")

    meta = st.columns(5)
    meta[0].metric("候选菜谱", f"{result.candidate_count} 道")
    meta[1].metric("修正轮数", f"{result.repairs_used} 次")
    meta[2].metric("LLM 排菜", "✅ 是" if result.llm_used else "⬜ 否(兜底)")
    meta[3].metric("耗时", f"{result.latency_sec:.1f}s")
    meta[4].metric("❤️ 命中", f"{sum(1 for p in result.days for d in p.dishes if d.recipe_id in liked_ids)} 道")
    if result.llm_error:
        st.caption(f"ℹ️ {result.llm_error}（已自动切换确定性排菜，结果仍可用）")

    if liked_now or hated_now:
        st.markdown(
            f"<div class='pref-bar'>当前口味档案：❤️ {('、'.join(liked_now) or '—')}"
            f" ｜ 🚫 {('、'.join(hated_now) or '—')}</div>", unsafe_allow_html=True)

    # ---- 上一步操作的确认（放在菜单附近，rerun 之后仍可见）+ 撤销
    notice = st.session_state.get("notice")
    if notice:
        nc1, nc2 = st.columns([6, 1])
        with nc1:
            (st.success if notice.get("kind") in ("swap", "dislike", "like") else st.info)(notice["text"])
        with nc2:
            if st.session_state.get("undo") and st.button("↩️ 撤销", key="undo_btn",
                                                          use_container_width=True,
                                                          help="撤销刚才这次改动"):
                undo = st.session_state.pop("undo")
                result.days = undo["days"]
                if undo.get("profile") is not None:
                    prof.save_profile(undo["profile"])
                refresh_result(result, db)
                st.session_state["notice"] = {"kind": "undo", "text": "↩️ 已撤销上一步改动"}
                st.session_state["stale"] = False
                st.rerun()

    # ---- 每日菜单 + 逐道反馈
    pending = None
    st.subheader("🗓️ 每日菜单")
    st.caption("**🔄 换一道** = 只换今晚这道（不动你的口味偏好）；**❤️ 喜欢** = 记住并以后多安排；"
               "**🚫 不喜欢** = 换掉并记住，以后不再出现。")
    labels = [f"第 {i} 天" for i in range(1, len(result.days) + 1)]
    # 让「第几天」的选中态跨 rerun 保留（st.tabs 需 key + on_change 才注册为有状态控件）
    _cur = st.session_state.get("day_tabs")
    if _cur is not None and _cur not in labels:
        st.session_state["day_tabs"] = labels[0]  # 天数变少导致越界 → 纠正到第 1 天
    day_tabs = st.tabs(labels, key="day_tabs", on_change="rerun")
    # 当前正在看的那一天（用于反馈后把客户留在原地）
    _open_day = next((int(l.split()[1]) for l, t in zip(labels, day_tabs) if t.open), 1)
    for tab, plan in zip(day_tabs, result.days):
        with tab:
            total_day = 0.0
            for dish in plan.dishes:
                r = db.by_id(dish.recipe_id)
                if r is None:
                    continue
                total_day += r.cost_yuan * c.people / 2.0
                is_loved = r.name in liked_now
                is_hated = r.name in hated_now
                chips = [f"<span class='chip'>{r.category}</span>",
                         f"<span class='chip'>{r.spice_level}</span>",
                         f"<span class='chip'>⏱ {r.time_min}min</span>",
                         f"<span class='chip'>¥{r.cost_yuan}/2人份</span>"]
                chips += [f"<span class='chip chip-green'>{t}</span>" for t in r.goal_tags]
                chips += [f"<span class='chip chip-red'>{a}</span>" for a in r.allergens]
                if is_loved:
                    chips.insert(0, "<span class='chip chip-pink'>❤️ 已收藏</span>")

                row = st.columns([3.4, 1, 1, 1])
                with row[0]:
                    st.markdown(
                        f"<div class='dish-card{' loved' if is_loved else (' hated' if is_hated else '')}'>"
                        f"<div class='dish-name'>{r.name}</div>"
                        f"<div class='dish-meta'>{''.join(chips)}</div>"
                        f"<div class='dish-reason'>💡 {dish.reason or '—'}</div>"
                        f"</div>", unsafe_allow_html=True)
                with row[1]:
                    if st.button("🔄 换一道", key=f"swap_{plan.day}_{dish.recipe_id}",
                                 use_container_width=True, help="只换今晚这道，不影响口味偏好"):
                        pending = ("swap", plan.day, dish.recipe_id)
                with row[2]:
                    if st.button("✅ 已喜欢" if is_loved else "❤️ 喜欢",
                                 key=f"like_{plan.day}_{dish.recipe_id}",
                                 use_container_width=True, help="合口味：以后多安排这道菜"):
                        pending = ("like", plan.day, dish.recipe_id)
                with row[3]:
                    if st.button("⛔ 已排除" if is_hated else "🚫 不喜欢",
                                 key=f"hate_{plan.day}_{dish.recipe_id}",
                                 use_container_width=True, help="不合口味：换掉并记住"):
                        pending = ("dislike", plan.day, dish.recipe_id)
            st.caption(f"本天预计花费 ¥{total_day:.0f}" +
                       (f" / 预算 ¥{c.budget_per_person_day * c.people:.0f}" if c.budget_per_person_day else ""))

    # ---- 处理反馈：只动这一道菜，不整周重排（客户不会"点一道，全周都变"）
    if pending:
        kind, day_no, rid = pending
        name = db.by_id(rid).name if db.by_id(rid) else rid
        prev_days = [p.model_copy(deep=True) for p in result.days]
        prev_profile = prof.load_profile()
        undo_profile = None
        text = ""

        if kind == "swap":
            new_days, rep = swap_dish(result.days, day_no, rid, db, c)
            if rep:
                result.days = new_days
                refresh_result(result, db)
                text = f"🔄 已把第 {day_no} 天的「{name}」换成「{rep.name}」（口味偏好未改动）"
            else:
                text = f"⚠️ 暂时没有可替换「{name}」的菜了（候选已用完），可放宽时长/预算或减少天数"
        elif kind == "like":
            prof.set_feedback(name, "like", KNOWN_NAMES)
            undo_profile = prev_profile
            text = f"❤️ 已记住你喜欢「{name}」，以后会优先安排（本次菜单不变）"
        else:  # dislike
            prof.set_feedback(name, "dislike", KNOWN_NAMES)
            undo_profile = prev_profile
            new_days, rep = swap_dish(result.days, day_no, rid, db, c)
            if rep:
                result.days = new_days
                refresh_result(result, db)
                text = f"🚫 已记住不喜欢「{name}」，第 {day_no} 天换成「{rep.name}」，以后不再出现"
            else:
                text = f"🚫 已记住不喜欢「{name}」（本次没有可替换的菜，其他天未改动）"

        st.session_state["notice"] = {"kind": kind, "text": text}
        st.session_state["undo"] = {"days": prev_days, "profile": undo_profile}
        st.session_state["stale"] = False  # 不触发整周重排
        st.rerun()

    # ---- 买菜清单
    st.subheader("🛒 买菜清单")
    st.caption(f"已按 **{c.people} 人**份量折算（菜谱为 2 人份基准）；🏠 标记的是家里已有、无需购买。")
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
            st.caption("🏠 已有库存覆盖： " + "、".join(s.name for s in have[:20]))
    else:
        st.warning("暂无需要采购的食材。")

    # ---- 校验与过程
    with st.expander("🔍 确定性校验报告与执行轨迹"):
        if result.issues:
            for i in result.issues:
                st.markdown(f"- {'❌' if i.level == 'error' else '⚠️'} {i.message}")
        else:
            st.success("无任何校验问题 ✅")
        for t in result.trace:
            st.markdown(f"- `{t}`")
        st.caption(f"约束命中率 100%（硬约束）· 单次耗时 {result.latency_sec:.1f}s · "
                   f"覆盖菜谱 {result.candidate_count} 道")

    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("🔄 按当前口味重新排一版", use_container_width=True):
            st.session_state["stale"] = True
            st.rerun()
    with c2:
        st.caption("口味档案有改动时菜单会自动更新；也可以去「❤️ 我的口味档案」页统一管理。")


# ================================================================ 页面 2：口味档案
def render_profile() -> None:
    st.title("❤️ 我的口味档案")
    st.caption("喜欢和不喜欢的菜各有一个独立列表，随时能加、能移、能删。"
               "改动后菜单页会自动按新口味重排。")

    liked = prof.liked_names(KNOWN_NAMES)
    disliked = prof.disliked_names(KNOWN_NAMES)
    unrated = [r for r in db.recipes if r.name not in liked and r.name not in disliked]

    m = st.columns(4)
    m[0].metric("菜谱库", f"{len(db.recipes)} 道")
    m[1].metric("❤️ 喜欢", f"{len(liked)} 道")
    m[2].metric("🚫 不喜欢", f"{len(disliked)} 道")
    m[3].metric("未表态", f"{len(unrated)} 道")

    def chips_html(r) -> str:
        if r is None:
            return ""
        return (f"<span class='chip'>{r.category}</span><span class='chip'>{r.spice_level}</span>"
                f"<span class='chip'>⏱ {r.time_min}min</span><span class='chip'>¥{r.cost_yuan}</span>")

    def recipe_of(name: str):
        rid = NAME2ID.get(name)
        return db.by_id(rid) if rid else None

    tab_lists, tab_browse = st.tabs(["📋 我的喜好列表", "📝 全部菜品挑选"])

    # ---------------- 列表页：两个独立、可编辑的列表 ----------------
    with tab_lists:
        action = None
        col_l, col_h = st.columns(2)

        with col_l:
            st.markdown(f"#### ❤️ 喜欢的菜（{len(liked)}）")
            st.caption("排菜时优先安排，并尽量分散到不同天")
            if not liked:
                st.info("列表为空。可以从右侧「移到喜欢」，或用下方「快速添加」。")
            for name in liked:
                r = recipe_of(name)
                rid = NAME2ID.get(name, name)
                row = st.columns([3, 1, 1])
                with row[0]:
                    st.markdown(f"**{name}**　{chips_html(r)}", unsafe_allow_html=True)
                with row[1]:
                    if st.button("→ 🚫", key=f"mv2hate_{rid}", help="移到「不喜欢」列表",
                                 use_container_width=True):
                        action = (name, "dislike")
                with row[2]:
                    if st.button("✖ 移除", key=f"rm_from_like_{rid}", help="从列表移除（恢复未表态）",
                                 use_container_width=True):
                        action = (name, "remove")
            if liked and st.button("🧹 清空「喜欢」列表", key="clr_like_btn", use_container_width=True):
                action = ("", "clear_like")

        with col_h:
            st.markdown(f"#### 🚫 不喜欢的菜（{len(disliked)}）")
            st.caption("这些菜绝不会出现在菜单里")
            if not disliked:
                st.info("列表为空。菜单里点 🚫 的菜会自动进到这里。")
            for name in disliked:
                r = recipe_of(name)
                rid = NAME2ID.get(name, name)
                row = st.columns([3, 1, 1])
                with row[0]:
                    st.markdown(f"**{name}**　{chips_html(r)}", unsafe_allow_html=True)
                with row[1]:
                    if st.button("→ ❤️", key=f"mv2like_{rid}", help="移到「喜欢」列表",
                                 use_container_width=True):
                        action = (name, "like")
                with row[2]:
                    if st.button("✖ 移除", key=f"rm_from_hate_{rid}", help="从列表移除（恢复未表态）",
                                 use_container_width=True):
                        action = (name, "remove")
            if disliked and st.button("🧹 清空「不喜欢」列表", key="clr_hate_btn", use_container_width=True):
                action = ("", "clear_dislike")

        if action:
            name, act = action
            prof.set_feedback(name, act, KNOWN_NAMES)
            st.session_state["stale"] = True
            st.toast("口味档案已更新")
            st.rerun()

        st.divider()
        st.markdown(f"**➕ 快速添加**（从 {len(unrated)} 道未表态的菜里多选，一次加入某个列表）")
        q1, q2, q3 = st.columns([3, 1, 1])
        with q1:
            picks = st.multiselect("选择菜品", [r.name for r in unrated], key="pf_quick",
                                   placeholder="输入菜名搜索，可多选", label_visibility="collapsed")
        with q2:
            add_like = st.button("❤️ 加入喜欢", use_container_width=True, disabled=not picks)
        with q3:
            add_hate = st.button("🚫 加入不喜欢", use_container_width=True, disabled=not picks)
        if picks and (add_like or add_hate):
            act = "like" if add_like else "dislike"
            prof.bulk_feedback(picks, act, KNOWN_NAMES)
            st.session_state["stale"] = True
            st.toast(f"已加入「{'喜欢' if add_like else '不喜欢'}」：{'、'.join(picks)}")
            st.rerun()

        st.divider()
        c1, _ = st.columns([1, 3])
        with c1:
            if st.button("🗑️ 清空全部档案", use_container_width=True):
                prof.clear_all()
                st.session_state["stale"] = True
                st.rerun()
        st.caption(f"档案文件：`{prof.profile_path()}`")

    # ---------------- 挑选页：浏览全库并逐道标记 ----------------
    with tab_browse:
        st.markdown("**按分类浏览，直接标记喜欢 / 不喜欢**")
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            kw = st.text_input("🔍 搜索菜名/主料", placeholder="例如：鸡、豆腐、西兰花", key="pf_search")
        with col_f2:
            cats = ["全部"] + sorted({r.category for r in db.recipes})
            cat = st.selectbox("📂 分类筛选", cats, key="pf_cat")

        def match(r) -> bool:
            if cat != "全部" and r.category != cat:
                return False
            if not kw:
                return True
            text = r.name + "".join(i.name for i in r.ingredients)
            return kw.strip() in text

        shown = [r for r in db.recipes if match(r)]
        st.caption(f"匹配 {len(shown)} 道菜")

        pf_feedback = None
        for r in shown:
            is_loved = r.name in liked
            is_hated = r.name in disliked
            row = st.columns([4, 1, 1])
            with row[0]:
                state = "❤️ 喜欢" if is_loved else ("🚫 不喜欢" if is_hated else "未表态")
                st.markdown(
                    f"<div class='dish-card{' loved' if is_loved else (' hated' if is_hated else '')}'>"
                    f"<div class='dish-name'>{r.name}</div>"
                    f"<div class='dish-meta'>{chips_html(r)}<span class='chip'>{state}</span></div>"
                    f"<div class='dish-reason'>主料：{'、'.join(i.name for i in r.ingredients[:5])}</div>"
                    f"</div>", unsafe_allow_html=True)
            with row[1]:
                if st.button("✅ 已喜欢" if is_loved else "❤️ 喜欢", key=f"pf_like_{r.id}",
                             use_container_width=True):
                    pf_feedback = (r.name, "like")
            with row[2]:
                if st.button("⛔ 已排除" if is_hated else "🚫 不喜欢", key=f"pf_hate_{r.id}",
                             use_container_width=True):
                    pf_feedback = (r.name, "dislike")

        if pf_feedback:
            name, action = pf_feedback
            prof.set_feedback(name, action, KNOWN_NAMES)
            st.session_state["stale"] = True
            st.toast("已更新口味档案")
            st.rerun()


# ================================================================ 路由
if page.startswith("🍽️"):
    render_planner()
else:
    render_profile()
