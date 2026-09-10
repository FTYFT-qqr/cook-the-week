"""Recipe-Planner · Streamlit Demo（MVP → 正式版 第一步）

两个界面：
1) 🍽️ 菜单规划：填需求 → 生成每日菜单 → 逐道 ❤️/🚫/🔄 反馈 → 买菜清单（可打勾/可带走）
2) ❤️ 我的口味档案：独立管理喜欢 / 不喜欢，带二次确认与多步撤销

本版围绕客户体验的六件事（对应 docs/MVP到正式版-体验改进建议.md 的第一步）：
- A1 方案持久化 + 回访：关掉页面、明天再来，这一周还是我的那一周
- F2/F3 清单能打勾、能复制 / 导出 CSV / 可打印（带到菜市场去）
- D1/D2 技术指标收进「开发者视角」，前排换成钱 / 时间 / 忌口 / 命中喜好 + 整周总览
- H1 排不出来时，给可点击的「放宽条件」而不是一句"候选已用完"
- E3 破坏性操作二次确认 + 多步撤销（两个页面都能撤）
- C1/G1 生成过程有阶段反馈、可取消；手机视图可单手操作

运行：streamlit run app.py
"""
from __future__ import annotations

import time

import streamlit as st

from recipe_planner import profile as prof
from recipe_planner import reporting as rep
from recipe_planner import store
from recipe_planner import ui_state as ui
from recipe_planner.core import refresh_result, swap_dish
from recipe_planner.db import load_db
from recipe_planner.models import (
    ALLERGENS,
    DISPLAY_CATEGORIES,
    GOALS,
    MEAL,
    SPICE_LEVELS,
    TASTE_TAGS,
    UserConstraints,
)
from recipe_planner.progress import PlanJob

st.set_page_config(page_title="一周食谱规划 Agent", page_icon="🍳", layout="wide")

# ---------------------------------------------------------------- 样式
st.markdown(
    """
    <style>
    .dish-card { background:#fff; border:1px solid #e8e8e8; border-left:4px solid #ff9f43;
                 border-radius:10px; padding:12px 14px; margin:2px 0; }
    .dish-card.loved { border-left-color:#ec4899; background:#fdf2f8; }
    .dish-card.hated { border-left-color:#6b7280; background:#f9fafb; }
    .dish-name { font-size:1.05rem; font-weight:700; color:#1f1f1f; }
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
    .muted { color:#6b7280; font-size:0.8rem; font-weight:400; }
    /* 概览卡片：flex-wrap，手机上自动一列，桌面上一行 */
    .sum-wrap { display:flex; flex-wrap:wrap; gap:10px; margin:6px 0 2px 0; }
    .sum-card { flex:1 1 190px; background:#fff; border:1px solid #e8e8e8; border-radius:12px;
                padding:10px 14px; }
    .sum-card.ok   { border-left:5px solid #22c55e; }
    .sum-card.warn { border-left:5px solid #f59e0b; }
    .sum-card.bad  { border-left:5px solid #ef4444; }
    .sum-title { color:#6b7280; font-size:0.82rem; }
    .sum-value { font-size:1.35rem; font-weight:700; color:#111827; margin:2px 0; }
    .sum-sub { color:#6b7280; font-size:0.76rem; line-height:1.3; }
    /* 整周总览：一屏看全，手机上自动换行 */
    .day-wrap { display:flex; flex-wrap:wrap; gap:8px; }
    .day-row { flex:1 1 205px; background:#fff; border:1px solid #ececec; border-radius:10px;
               padding:8px 10px; }
    .day-row .day-name { font-weight:700; font-size:0.9rem; }
    .day-row .day-dishes { font-size:0.88rem; color:#374151; margin:3px 0; }
    .day-row .day-meta { font-size:0.78rem; color:#6b7280; }
    /* 手机视图：把手持场景要点的东西做大 */
    div[data-testid="stButton"] button { min-height:2.4rem; font-size:0.95rem; }
    /* 左栏任务栏：整行大按钮，当前页高亮（不再用单选圆圈） */
    section[data-testid="stSidebar"] div[data-testid="stButton"] button {
        min-height:3.2rem; font-size:1.02rem; font-weight:600; justify-content:flex-start;
        padding-left:0.9rem; border-radius:10px;
    }
    section[data-testid="stSidebar"] div[data-testid="stButton"] { margin-bottom:2px; }
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

# ---------------------------------------------------------------- 会话状态
ui.init_defaults()
for _k, _v in dict(
    people=2, days=3, spice="不辣", max_time=40, goal="随便", budget=0.0,
    allergens=[], taste=[], pantry="", dishes_per_day=2,
).items():
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("start_date", store.next_monday())
st.session_state.setdefault("plan_start", None)
st.session_state.setdefault("form_open", False)


def _sync_widgets_from_inputs(inp: dict) -> None:
    """把约束写回表单控件（必须在控件创建之前调用）。"""
    st.session_state["people"] = int(inp.get("people", 2))
    st.session_state["days"] = int(inp.get("days", 3))
    st.session_state["dishes_per_day"] = int(inp.get("dishes_per_day", 2))
    st.session_state["allergens"] = list(inp.get("allergens") or [])
    st.session_state["spice"] = inp.get("spice", "不辣")
    st.session_state["taste"] = list(inp.get("taste_tags") or [])
    st.session_state["goal"] = inp.get("goal", "随便")
    st.session_state["max_time"] = int(inp.get("max_time_min", 40))
    st.session_state["budget"] = float(inp.get("budget_per_person_day") or 0.0)
    st.session_state["pantry"] = "，".join(inp.get("pantry_items") or [])
    st.session_state["start_date"] = store.normalize_start(inp.get("start_date"))


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


def _name_of(rid: str) -> str:
    r = db.by_id(rid)
    return r.name if r else rid


def _load_record(rec: store.PlanRecord) -> None:
    """切到某一版存档：结果、需求、控件值一起换过去。"""
    inp = store.inputs_from_constraints(rec.result.constraints, rec.start_date)
    st.session_state["result"] = rec.result
    st.session_state["plan_inputs"] = inp
    st.session_state["pending_sync"] = inp
    st.session_state["record_id"] = rec.id
    st.session_state["plan_start"] = rec.start_date
    st.session_state["stale"] = False
    st.session_state["job"] = None
    st.session_state["relax"] = None
    st.session_state["check_epoch"] += 1


def _commit_plan() -> None:
    """改动后写回存档 —— 客户刷新、明早再打开看到的都是改过的那一版。"""
    rid = st.session_state.get("record_id")
    result = st.session_state.get("result")
    if rid and result is not None:
        store.update_result(rid, result)


# 首次打开：磁盘上有存档就直接呈现「我这一周」，而不是又一张空表单
if st.session_state["plan_inputs"] is None and st.session_state["result"] is None:
    _rec = store.latest_record()
    if _rec is not None:
        _load_record(_rec)          # 会把控件值挂到 pending_sync
        st.session_state["revisit"] = _rec.id

# 按钮回调里不能直接改控件值（控件已实例化）→ 统一挂起，到脚本开头再生效
if st.session_state.get("pending_sync"):
    _sync_widgets_from_inputs(st.session_state.pop("pending_sync"))

# ---------------------------------------------------------------- 任务栏导航
# 客户反馈：左栏的界面选项太小、还是单选圈圈。改成整行大按钮的任务栏。
NAV_ITEMS = [
    ("demand", "📝 需求 & 生成"),
    ("menu", "🍽️ 本周菜单"),
    ("shopping", "🛒 买菜清单"),
    ("profile", "❤️ 口味档案"),
]
NAV_LABEL = dict(NAV_ITEMS)
if st.session_state.get("page") is None:
    # 有存档就直接落在「本周菜单」；没有就先让客户填需求
    st.session_state["page"] = "menu" if st.session_state.get("result") is not None else "demand"


def goto(key: str) -> None:
    st.session_state["page"] = key
    st.rerun()


def nav_button(key: str, prefix: str) -> bool:
    return st.button(NAV_LABEL[key], key=f"{prefix}{key}", use_container_width=True,
                     type="primary" if key == st.session_state["page"] else "secondary")


def nav_taskbar(prefix: str = "nav_", horizontal: bool = False) -> None:
    """任务栏式导航：整行大按钮，当前所在页高亮。"""
    if horizontal:
        for col, (key, _label) in zip(st.columns(len(NAV_ITEMS)), NAV_ITEMS):
            with col:
                if nav_button(key, prefix):
                    goto(key)
    else:
        for key, _label in NAV_ITEMS:
            if nav_button(key, prefix):
                goto(key)


# ---------------------------------------------------------------- 侧边栏
with st.sidebar:
    st.title("🍳 食谱规划 Agent")
    nav_taskbar("nav_")
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
    _cur = store.get_record(st.session_state.get("record_id"))
    if _cur is not None:
        st.caption(f"📌 当前方案：**{_cur.label}**（{_cur.created_at} 生成）")
    st.divider()
    st.toggle("📱 手机视图（大字、单列、按钮更大）", key="mobile_view")
    st.caption("口味档案与方案都只保存在这台机器上，不会上传。")
    if _cur is not None:
        with st.expander("🗂️ 本机保存的方案"):
            for line in store.archive_summary():
                st.caption("• " + line)

MOBILE = bool(st.session_state.get("mobile_view"))
if MOBILE:
    # 手机上一只手拿菜一只手划屏幕：按钮做大一点；侧栏要点汉堡才出来，所以在正文顶部再放一条任务栏
    st.markdown(
        "<style>div[data-testid='stButton'] button{min-height:3.1rem;font-size:1.03rem;}"
        "div[data-testid='stCheckbox'] label p{font-size:1rem;}</style>",
        unsafe_allow_html=True,
    )
    nav_taskbar("navm_", horizontal=True)


# ================================================================ 页面 1：菜单规划
def _notice_block(result) -> None:
    """上一步操作的确认（rerun 之后仍可见）+ 多步撤销 + 改动历史。"""
    notice = ui.get_notice()
    if not notice:
        return
    ok_kinds = {"swap", "dislike", "like", "save", "undo", "relax"}
    nc1, nc2 = st.columns([6, 1])
    with nc1:
        (st.success if notice.get("kind") in ok_kinds else st.info)(notice["text"])
    with nc2:
        depth = ui.undo_depth()
        if depth and st.button(f"↩️ 撤销({depth})", key="undo_btn", use_container_width=True,
                               help="可以连续点，最多回退 5 步"):
            entry = ui.pop_undo()
            if entry:
                if entry.get("days"):
                    result.days = [p.model_copy(deep=True) for p in entry["days"]]
                    refresh_result(result, db)
                    _commit_plan()
                if entry.get("profile") is not None:
                    prof.save_profile(entry["profile"])
                ui.set_notice("undo", f"↩️ 已撤销：{entry['text']}")
                ui.push_history(f"↩️ 撤销了「{entry['text']}」")
                st.session_state["stale"] = False
            st.rerun()


def _relax_options(c: UserConstraints, issues=(), swap_failed: bool = False) -> None:
    """H1：排不出来 / 换不动的时候，直接给可点击的下一步，而不是把活甩给客户。"""
    codes = {i.code for i in issues}
    opts: list[tuple[str, str, object]] = []
    new_max = min(90, c.max_time_min + 20)
    if new_max > c.max_time_min and (swap_failed or codes & {"over_time", "shortage", "time"}):
        opts.append(("time", f"⏱️ 时长上限放宽到 {new_max} 分钟", new_max))
    if c.budget_per_person_day and (swap_failed or "over_budget" in codes):
        new_budget = round(c.budget_per_person_day + 20, 2)
        opts.append(("budget", f"💰 预算加到 ¥{new_budget:.0f}/人/天", new_budget))
    day_no = (st.session_state.get("relax") or {}).get("day")
    if (swap_failed or "shortage" in codes) and day_no:
        opts.append(("drop_dish", f"🍽️ 第 {day_no} 天少排一道菜（腾出预算）", day_no))
    if not opts:
        return
    st.caption("点一下就能放宽，然后自动重排一版：")
    cols = st.columns(len(opts))
    for col, (kind, label, value) in zip(cols, opts):
        with col:
            if st.button(label, key=f"relax_{kind}", use_container_width=True):
                if kind == "drop_dish":
                    _drop_a_dish(int(value))
                else:
                    inp = dict(st.session_state["plan_inputs"] or {})
                    if kind == "time":
                        inp["max_time_min"] = int(value)
                        note = f"⏱️ 单菜时长上限已放宽到 {int(value)} 分钟，正在重排…"
                    else:
                        inp["budget_per_person_day"] = float(value)
                        note = f"💰 预算已放宽到 ¥{float(value):.0f}/人/天，正在重排…"
                    st.session_state["plan_inputs"] = inp
                    st.session_state["pending_sync"] = inp
                    st.session_state["relax"] = None
                    st.session_state["job"] = None
                    st.session_state["stale"] = True
                    ui.set_notice("relax", note)
                    ui.push_history(note)
                st.rerun()


def _drop_a_dish(day_no: int) -> None:
    result = st.session_state.get("result")
    if result is None:
        return
    day = next((p for p in result.days if p.day == day_no), None)
    if day is None or len(day.dishes) <= 1:
        return
    prev_days = [p.model_copy(deep=True) for p in result.days]
    dropped = _name_of(day.dishes[-1].recipe_id)
    day.dishes = day.dishes[:-1]
    refresh_result(result, db)
    _commit_plan()
    st.session_state["relax"] = None
    st.session_state["stale"] = False
    ui.set_notice("swap", f"🍽️ 第 {day_no} 天已去掉「{dropped}」，其余各天未改动。")
    ui.push_undo(f"第 {day_no} 天去掉「{dropped}」",
                 days=prev_days, record_id=st.session_state.get("record_id"))
    ui.push_history(f"🍽️ 第 {day_no} 天去掉「{dropped}」")


def _empty_state(msg: str, key: str) -> None:
    st.info(msg)
    if st.button("📝 去填需求", key=key, type="primary"):
        goto("demand")


# ================================================================ 页面 1：需求 & 生成
def render_demand() -> None:
    st.title("📝 需求 & 生成")
    st.caption("填完点最下面的「🍽️ 生成菜单」。排好后会自动跳到「🍽️ 本周菜单」，"
               "买菜清单在「🛒 买菜清单」页。")

    if st.session_state["result"] is not None:
        rec = store.get_record(st.session_state.get("record_id"))
        cur_label = store.week_label(st.session_state.get("plan_start") or st.session_state["start_date"])
        c1, c2 = st.columns([3, 1])
        with c1:
            st.info(f"📌 当前已有方案：**{cur_label}**"
                    + (f"（{rec.created_at} 生成）" if rec is not None else "")
                    + "。改完需求再生成会另存为新的一版，旧版可以在菜单页一键找回。")
        with c2:
            if st.button("🍽️ 去看本周菜单", use_container_width=True):
                goto("menu")

    s1, s2 = st.columns([3, 1])
    with s1:
        sc = st.selectbox("选择场景后点右侧按钮自动填入表单", list(SCENARIOS),
                          label_visibility="collapsed", key="scenario_pick")
    with s2:
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

        col_d, col_e, col_f = st.columns(3)
        with col_d:
            allergens = st.multiselect("🚫 过敏原 / 忌口（硬排除）", ALLERGENS, key="allergens")
        with col_e:
            taste = st.multiselect("😋 口味偏好（尽量满足）", TASTE_TAGS, key="taste")
        with col_f:
            start_date_pick = st.date_input("📅 这一周从哪天开始（默认下周一）", key="start_date")

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
            start_date=str(start_date_pick),
        )
        st.session_state["stale"] = True
        st.session_state["job"] = None
        st.session_state["relax"] = None
        st.session_state["notice"] = None
        st.session_state["form_open"] = False
        st.session_state["page"] = "menu"     # 生成后自动去看菜单
        st.rerun()

    st.caption("💡 喜欢 / 不喜欢的菜在「❤️ 口味档案」里随时能改，排菜时会自动优先或排除。")


# ================================================================ 页面 2：本周菜单
def _ensure_plan():
    """按需重排（首次 / 需求变了 / 口味变了 / 放宽了条件）；等待过程有阶段反馈、可停止。"""
    inp = st.session_state["plan_inputs"]
    if inp is None:
        return None
    if st.session_state["result"] is not None and not st.session_state["stale"]:
        return st.session_state["result"]

    job = st.session_state.get("job")
    if job is None:
        job = PlanJob(build_constraints(inp), db).start()
        st.session_state["job"] = job
    if not job.done:
        with st.status(job.stage, expanded=True):
            st.write("顺序是：**挑菜谱 → 搭配一周 → 校验忌口/预算/时间 → 汇总清单**，"
                     "通常 3–5 秒。")
            st.progress(job.progress)
            if st.button("⏹️ 停止，我要改一下需求", key="cancel_job", use_container_width=True):
                job.cancel()
                st.session_state["job"] = None
                st.session_state["stale"] = False
                ui.set_notice("info", "⏹️ 已停止这次生成。需求都还在，改完再点「生成菜单」就行。")
                goto("demand")
        time.sleep(0.35)
        st.rerun()

    st.session_state["job"] = None
    if job.result is None:
        st.error("⚠️ 这次没能排出菜单（已经试过确定性兜底）。下面点一下放宽条件再试：")
        _relax_options(build_constraints(inp), [], swap_failed=True)
        st.session_state["stale"] = False
        st.stop()

    prev_rec = store.latest_record()
    result = job.result
    st.session_state["result"] = result
    st.session_state["stale"] = False
    st.session_state["check_epoch"] += 1
    rec = store.save_plan(result, start_date=inp.get("start_date"),
                          change_note="重新排了一版" if prev_rec else "首次生成")
    st.session_state["record_id"] = rec.id
    st.session_state["plan_start"] = rec.start_date
    st.session_state["revisit"] = None
    text = f"✅ 菜单已保存为「{rec.label}」，关掉页面明天再打开还在。"
    if prev_rec is not None:
        text += f" 上一版「{prev_rec.label}」可以用下面的「↩️ 回到上一版」找回。"
    ui.set_notice("save", text)
    ui.push_history(f"🆕 生成了「{rec.label}」的菜单")
    return result


def render_menu() -> None:
    st.title("🍽️ 本周菜单")
    if st.session_state["plan_inputs"] is None:
        _empty_state("还没有菜单。先去填一下需求（大约 20 秒），我会排出这一周并给出买菜清单。",
                     "goto_demand_m")
        return

    # ---- 回访：打开就是「你有一份 8/12–8/18 的菜单」
    if st.session_state.get("revisit"):
        rec = store.get_record(st.session_state["revisit"])
        if rec is not None:
            st.info(f"📌 你有一份 **{rec.label}** 的晚餐菜单（{rec.created_at} 生成），已经帮你打开了。")
            b1, b2, _ = st.columns([1, 1, 2])
            with b1:
                if st.button("👍 就用这份", use_container_width=True):
                    st.session_state["revisit"] = None
                    st.rerun()
            with b2:
                if st.button("🔁 重新排一份", use_container_width=True):
                    st.session_state["revisit"] = None
                    goto("demand")

    result = _ensure_plan()
    if result is None:
        _empty_state("还没有菜单。先去填一下需求。", "goto_demand_m2")
        return

    c = result.constraints
    start_date = st.session_state.get("plan_start") or st.session_state["start_date"]
    summary = rep.plan_summary(result, db, start_date)
    label = store.week_label(start_date)

    liked_now = prof.liked_names(KNOWN_NAMES)
    hated_now = prof.disliked_names(KNOWN_NAMES)

    st.divider()

    # ---- 忌口安全：硬失败必须一眼看到（关系到过敏）
    if summary.allergen_issues:
        st.error("🚨 下面的菜单里出现了你设置的忌口/过敏原，请先看这一条：")
        for i in summary.allergen_issues:
            st.error(f"• {i.message}")
    elif c.allergens:
        st.success("✅ 已避开你设置的忌口：" + "、".join(c.allergens)
                   + "　｜　请以食品包装配料表为准，下厨前再确认一次。")

    # ---- 人话概览（工程指标收进开发者视角）
    cost_sub = f"按 {c.people} 人 × {c.days} 天折算，实际以当地物价为准"
    if summary.budget_total:
        cost_card = ("bad" if summary.over_budget > 0 else "ok",
                     f"¥{summary.total_cost:.0f} <span class='muted'>/ 你设的 ¥{summary.budget_total:.0f}</span>",
                     (f"超出预算约 ¥{summary.over_budget:.0f}" if summary.over_budget > 0 else cost_sub))
    else:
        cost_card = ("", f"¥{summary.total_cost:.0f}", cost_sub + "（未设预算）")
    cards = [
        ("💰 这一周大概花", cost_card[1], cost_card[2], cost_card[0]),
        ("⏱️ 最费时的一天",
         (f"第 {summary.hardest_day} 天 约 {summary.hardest_minutes} 分钟" if summary.hardest_day else "—"),
         "按顺序做完全部菜的时间", "warn" if summary.hardest_minutes >= 60 else ""),
        ("❤️ 你收藏的菜", f"{summary.liked_hit} 道", f"本周一共 {summary.dishes} 道菜",
         "ok" if summary.liked_hit else ""),
    ]
    if c.goal != "随便":
        cards.append((f"🎯 契合「{c.goal}」", f"{summary.goal_hit} 道",
                      "按菜品标签做的方向性推荐，不等于营养计算", ""))
    cards.append(("🛡️ 忌口检查", "0 处冲突" if not summary.hard_issues else f"{len(summary.hard_issues)} 处待处理",
                  "已避开你设置的忌口；下厨前请看配料表再确认一次" if not summary.allergen_issues else "有过敏原冲突，见上方红字",
                  "ok" if not summary.hard_issues else "bad"))
    st.markdown(
        "<div class='sum-wrap'>" + "".join(
            f"<div class='sum-card {tone}'><div class='sum-title'>{t}</div>"
            f"<div class='sum-value'>{v}</div><div class='sum-sub'>{s}</div></div>"
            for t, v, s, tone in cards
        ) + "</div>",
        unsafe_allow_html=True,
    )

    if not result.final:
        st.warning("⚠️ 有几条约束没能同时满足，具体如下（可以直接放宽）：")
        for i in summary.hard_issues:
            st.warning(f"• {i.message}")

    # ---- 操作回执 + 撤销 + 回到上一版 + 改动历史
    _notice_block(result)
    prev = store.previous_record(st.session_state.get("record_id"))
    row_btns = st.columns([1, 1, 3])
    with row_btns[0]:
        if prev is not None and st.button(f"↩️ 回到上一版（{prev.label}）", key="restore_prev_btn",
                                          use_container_width=True,
                                          help="切回上一版的菜单与需求，当前这版仍在存档里"):
            _load_record(prev)
            ui.set_notice("info", f"↩️ 已切回「{prev.label}」那一版菜单（当前版仍在存档里）。")
            ui.push_history(f"↩️ 切回上一版：{prev.label}")
            st.rerun()
    with row_btns[1]:
        if st.button("🔄 重排一版", key="replan_btn", use_container_width=True,
                     help="按当前口味与需求重新排一份（会存成新的一版）"):
            st.session_state["stale"] = True
            st.session_state["job"] = None
            st.session_state["form_open"] = False
            st.rerun()
    with row_btns[2]:
        st.caption(f"当前方案：**{label}**　｜　口味档案有改动时菜单会自动重排。")
        if ui.history():
            with st.expander("🕘 改动历史（最近的改动都在这里）"):
                for h in ui.history():
                    st.caption("• " + h)

    # ---- 整周总览：一屏看全，不用一个个 tab 点
    st.subheader("🗓️ 整周总览")
    st.markdown(
        "<div class='day-wrap'>" + "".join(
            f"<div class='day-row'><div class='day-name'>第 {r.day} 天 "
            f"<span class='muted'>{r.weekday} {r.date_label}</span></div>"
            f"<div class='day-dishes'>{'、'.join(r.dishes) or '（未排）'}</div>"
            f"<div class='day-meta'>⏱ {r.minutes} 分钟 · 约 ¥{r.cost:.0f}</div></div>"
            for r in summary.rows
        ) + "</div>",
        unsafe_allow_html=True,
    )

    # ---- 每日菜单 + 逐道反馈
    pending = None
    st.subheader("🍳 每日详情")
    st.caption("**🔄 换一道** = 只换今晚这道（不动口味偏好）；**❤️ 喜欢** = 记住并以后多安排；"
               "**🚫 不喜欢** = 换掉并记住，以后不再出现。")
    labels = [f"第 {i} 天" for i in range(1, len(result.days) + 1)]

    def render_day(plan, day_no: int):
        picked = None
        for dish in plan.dishes:
            r = db.by_id(dish.recipe_id)
            if r is None:
                continue
            is_loved = r.name in liked_now
            is_hated = r.name in hated_now
            chips = [f"<span class='chip'>{r.category}</span>",
                     f"<span class='chip'>{r.spice_level}</span>",
                     f"<span class='chip'>⏱ {r.time_min}min</span>",
                     f"<span class='chip'>{r.difficulty}</span>",
                     f"<span class='chip'>¥{r.cost_yuan}/2人份</span>"]
            chips += [f"<span class='chip chip-green'>{t}</span>" for t in r.goal_tags]
            chips += [f"<span class='chip chip-red'>{a}</span>" for a in r.allergens]
            if is_loved:
                chips.insert(0, "<span class='chip chip-pink'>❤️ 已收藏</span>")

            st.markdown(
                f"<div class='dish-card{' loved' if is_loved else (' hated' if is_hated else '')}'>"
                f"<div class='dish-name'>{r.name}</div>"
                f"<div class='dish-meta'>{''.join(chips)}</div>"
                f"<div class='dish-reason'>💡 {dish.reason or '—'}</div>"
                f"</div>", unsafe_allow_html=True)

            btns = [
                ("swap", "🔄 换一道", f"只换今晚这道，不影响口味偏好"),
                ("like", "✅ 已喜欢" if is_loved else "❤️ 喜欢", "合口味：以后多安排这道菜"),
                ("dislike", "⛔ 已排除" if is_hated else "🚫 不喜欢", "不合口味：换掉并记住"),
            ]
            if MOBILE:
                for kind, label_, help_ in btns:
                    if st.button(label_, key=f"{kind if kind != 'dislike' else 'hate'}"
                                           f"_{day_no}_{dish.recipe_id}",
                                 use_container_width=True, help=help_):
                        picked = (kind, day_no, dish.recipe_id)
            else:
                row = st.columns([1, 1, 1])
                for col, (kind, label_, help_) in zip(row, btns):
                    with col:
                        key = f"{kind if kind != 'dislike' else 'hate'}_{day_no}_{dish.recipe_id}"
                        if st.button(label_, key=key, use_container_width=True, help=help_):
                            picked = (kind, day_no, dish.recipe_id)

        minutes = rep.day_minutes(plan, db)
        cost = rep.day_cost(plan, db, c.people)
        order, has_slow = rep.cook_order(plan, db)
        st.caption(f"⏱ 这天合计约 {minutes} 分钟 · 预计 ¥{cost:.0f}"
                   + ("　｜　有汤/炖菜可以先上火，实际用时更短" if has_slow else ""))
        if order:
            with st.expander("🍳 下锅顺序建议（先做哪道、最后做哪道）"):
                for line in order:
                    st.markdown(f"- {line}")
        return picked

    if MOBILE:
        sel_key = "day_select"
        if st.session_state.get(sel_key) not in labels:
            st.session_state[sel_key] = labels[0]
        sel = st.selectbox("看第几天", labels, key=sel_key)
        idx = labels.index(sel)
        with st.container(border=True):
            pending = render_day(result.days[idx], idx + 1) or pending
    else:
        _cur = st.session_state.get("day_tabs")
        if _cur is not None and _cur not in labels:
            st.session_state["day_tabs"] = labels[0]  # 天数变少导致越界 → 纠正到第 1 天
        day_tabs = st.tabs(labels, key="day_tabs", on_change="rerun")
        for tab, plan in zip(day_tabs, result.days):
            with tab:
                pending = render_day(plan, plan.day) or pending

    # ---- 处理反馈：只动这一天，不整周重排（客户不会"点一道，全周都变"）
    if pending:
        kind, day_no, rid = pending
        name = _name_of(rid)
        prev_days = [p.model_copy(deep=True) for p in result.days]
        prev_profile = prof.load_profile()
        undo_profile = None
        relax_failed = None
        text = ""

        if kind == "swap":
            new_days, new_recipe = swap_dish(result.days, day_no, rid, db, c)
            if new_recipe:
                result.days = new_days
                refresh_result(result, db)
                text = f"🔄 已把第 {day_no} 天的「{name}」换成「{new_recipe.name}」（口味偏好未改动）"
            else:
                text = (f"⚠️ 没有能替换「{name}」的菜了。下面点一下放宽条件，我马上重排一版。")
                relax_failed = {"day": day_no, "name": name}
        elif kind == "like":
            prof.set_feedback(name, "like", KNOWN_NAMES)
            undo_profile = prev_profile
            text = f"❤️ 已记住你喜欢「{name}」，以后会优先安排（本次菜单不变）"
        else:  # dislike
            prof.set_feedback(name, "dislike", KNOWN_NAMES)
            undo_profile = prev_profile
            new_days, new_recipe = swap_dish(result.days, day_no, rid, db, c)
            if new_recipe:
                result.days = new_days
                refresh_result(result, db)
                text = f"🚫 已记住不喜欢「{name}」，第 {day_no} 天换成「{new_recipe.name}」，以后不再出现"
            else:
                text = f"🚫 已记住不喜欢「{name}」（本次没有可替换的菜，其他天未改动）"
                relax_failed = {"day": day_no, "name": name}

        ui.set_notice(kind, text)
        ui.push_undo(text, days=prev_days, profile=undo_profile,
                     record_id=st.session_state.get("record_id"))
        ui.push_history(text)
        st.session_state["relax"] = relax_failed
        st.session_state["stale"] = False  # 不触发整周重排
        _commit_plan()
        st.rerun()

    # ---- 换不动的时候：给可点击的放宽选项
    if st.session_state.get("relax"):
        _relax_options(c, result.issues, swap_failed=True)

    # 买菜清单已独立成页（到店后的场景），见 render_shopping()
    st.caption("🛒 买菜清单在左侧「🛒 买菜清单」页：可以打勾、导出 CSV、复制文本、打印。")

    # ---- 开发者视角：工程指标收进折叠区（想给面试官看随时展开）
    with st.expander("🔧 开发者视角（候选数 / 修正轮数 / LLM / 耗时 / 执行轨迹）"):
        meta = st.columns(5)
        meta[0].metric("候选菜谱", f"{result.candidate_count} 道")
        meta[1].metric("修正轮数", f"{result.repairs_used} 次")
        meta[2].metric("LLM 排菜", "✅ 是" if result.llm_used else "⬜ 否(兜底)")
        meta[3].metric("耗时", f"{result.latency_sec:.1f}s")
        meta[4].metric("❤️ 命中", f"{summary.liked_hit} 道")
        if result.llm_error:
            st.caption(f"LLM 未启用原因：{result.llm_error}")
        st.markdown("**确定性校验报告**")
        if result.issues:
            for i in result.issues:
                st.markdown(f"- {'❌' if i.level == 'error' else '⚠️'} {i.message}")
        else:
            st.success("无任何校验问题 ✅")
        st.markdown("**执行轨迹**")
        for t in result.trace:
            st.markdown(f"- `{t}`")
        st.caption(f"覆盖菜谱 {result.candidate_count} 道 · 单次耗时 {result.latency_sec:.1f}s · "
                   f"存档 id {st.session_state.get('record_id')}")
        if not result.llm_used and result.llm_error:
            st.caption("提示：智能排菜没启用时会自动改用确定性排菜，忌口与预算一样会被校验。")

    if liked_now or hated_now:
        st.markdown(
            f"<div class='pref-bar'>当前口味档案：❤️ {('、'.join(liked_now) or '—')}"
            f" ｜ 🚫 {('、'.join(hated_now) or '—')}</div>", unsafe_allow_html=True)


# ================================================================ 页面 3：买菜清单
def render_shopping() -> None:
    """到店后的场景：一屏一类、点一下打勾、能带走。"""
    st.title("🛒 买菜清单")
    if st.session_state["plan_inputs"] is None:
        _empty_state("还没有菜单，所以也还没有清单。先去填一下需求生成一份。", "goto_demand_s")
        return

    result = _ensure_plan()
    if result is None:
        _empty_state("还没有菜单，所以也还没有清单。先去填一下需求。", "goto_demand_s2")
        return

    c = result.constraints
    start_date = st.session_state.get("plan_start") or st.session_state["start_date"]
    label = store.week_label(start_date)
    st.caption(f"{label} 这一周要买的东西 · 已按 **{c.people} 人**份量折算（菜谱为 2 人份基准）；"
               "🏠 标记的是家里已有、无需购买。　**买一样勾一样，剩下的最后汇总。**")

    need = [s for s in result.shopping if s.needed]
    have = [s for s in result.shopping if not s.needed]
    epoch = st.session_state["check_epoch"]
    checked: list[str] = []

    def shop_row(it) -> None:
        if st.checkbox(f"{it.name}　**{it.amount}**", key=f"chk_{epoch}_{it.name}"):
            checked.append(it.name)
        st.caption(f"　↳ 用于：{'、'.join(it.for_recipes[:3])}")

    if not need:
        st.warning("暂无需要采购的食材（家里库存都覆盖了）。")
    elif MOBILE:
        with st.container(border=True):
            for it in need:
                shop_row(it)
    else:
        cols = st.columns(3)
        for idx, cat in enumerate(DISPLAY_CATEGORIES):
            items = [s for s in need if s.category == cat]
            if not items:
                continue
            with cols[idx % 3]:
                st.markdown(f"**{cat}**")
                for it in items:
                    shop_row(it)

    if need:
        st.progress(len(checked) / len(need))
        st.caption(f"已买 **{len(checked)} / {len(need)}** 项")
        e1, e2, e3, e4 = st.columns([1, 1, 1, 1])
        with e1:
            if st.button("🧹 清除勾选", key="clear_checks", use_container_width=True):
                st.session_state["check_epoch"] = epoch + 1  # 换一代表 → 勾选全部作废
                st.rerun()
        with e2:
            st.download_button(
                "⬇️ 导出 CSV", data=rep.shopping_csv(result, set(checked)),
                file_name=f"买菜清单_{label}.csv", mime="text/csv",
                use_container_width=True, key="csv_dl",
                help="Excel / 手机表格都能打开（已按已买勾选标注）",
            )
        with e3:
            with st.expander("📋 复制文本", expanded=False):
                st.caption("点右上角图标复制，直接发微信 / 存备忘录")
                st.code(rep.shopping_text(result, set(checked), label), language=None)
        with e4:
            with st.expander("🖨️ 可打印视图", expanded=False):
                st.caption("整周菜单 + 清单，Ctrl+P 打印或另存 PDF 贴冰箱")
                st.code(rep.printable_text(result, db, start_date, set(checked)), language=None)

    if have:
        st.caption("🏠 已有库存覆盖： " + "、".join(s.name for s in have[:20]))
    st.caption("💡 想换某道菜或反馈口味 → 去「🍽️ 本周菜单」；想改需求或改人数 → 去「📝 需求 & 生成」。")


# ================================================================ 页面 2：口味档案
def guarded_button(label: str, key: str, prompt: str, help_: str = "") -> bool:
    """破坏性操作二次确认：第一次点击只是「举起来」，确认后才真的执行。"""
    if ui.armed() == key:
        st.warning(prompt)
        y, n = st.columns(2)
        with y:
            if st.button("✅ 确认执行", key=f"yes_{key}", type="primary", use_container_width=True):
                ui.disarm()
                return True
        with n:
            if st.button("取消", key=f"no_{key}", use_container_width=True):
                ui.disarm()
                st.rerun()
        return False
    if st.button(label, key=key, use_container_width=True, help=help_):
        ui.arm(key)
        st.rerun()
    return False


def render_profile() -> None:
    st.title("❤️ 我的口味档案")
    st.caption("喜欢和不喜欢的菜各有一个独立列表，随时能加、能移、能删。"
               "改动后菜单页会自动按新口味重排；清空这类操作需要再确认一次。")

    liked = prof.liked_names(KNOWN_NAMES)
    disliked = prof.disliked_names(KNOWN_NAMES)
    unrated = [r for r in db.recipes if r.name not in liked and r.name not in disliked]

    m = st.columns(4)
    m[0].metric("菜谱库", f"{len(db.recipes)} 道")
    m[1].metric("❤️ 喜欢", f"{len(liked)} 道")
    m[2].metric("🚫 不喜欢", f"{len(disliked)} 道")
    m[3].metric("未表态", f"{len(unrated)} 道")

    # 撤销入口就在这一页（客户不会为了撤销再跑回菜单页）
    notice = ui.get_notice()
    if notice:
        n1, n2 = st.columns([5, 1])
        with n1:
            st.info(notice["text"])
        with n2:
            if ui.undo_depth() and st.button(f"↩️ 撤销({ui.undo_depth()})", key="profile_undo_btn",
                                             use_container_width=True,
                                             help="可以连续点，最多回退 5 步"):
                entry = ui.pop_undo()
                if entry and entry.get("profile") is not None:
                    prof.save_profile(entry["profile"])
                    st.session_state["stale"] = True
                    ui.set_notice("undo", f"↩️ 已撤销：{entry['text']}")
                    ui.push_history(f"↩️ 撤销了「{entry['text']}」")
                st.rerun()
    if ui.history():
        with st.expander("🕘 改动历史"):
            for h in ui.history():
                st.caption("• " + h)

    def chips_html(r) -> str:
        if r is None:
            return ""
        return (f"<span class='chip'>{r.category}</span><span class='chip'>{r.spice_level}</span>"
                f"<span class='chip'>⏱ {r.time_min}min</span><span class='chip'>¥{r.cost_yuan}</span>")

    def recipe_of(name: str):
        rid = NAME2ID.get(name)
        return db.by_id(rid) if rid else None

    def record_change(text: str, prev_profile: dict) -> None:
        ui.push_undo(text, profile=prev_profile)
        ui.push_history(text)
        ui.set_notice("info", text)
        st.session_state["stale"] = True

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
            if liked and guarded_button("🧹 清空「喜欢」列表", "clr_like_btn",
                                        f"确认要清空「喜欢」里的 {len(liked)} 道菜吗？清空后可以点上面的「撤销」找回。"):
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
            if disliked and guarded_button("🧹 清空「不喜欢」列表", "clr_hate_btn",
                                           f"确认要清空「不喜欢」里的 {len(disliked)} 道菜吗？清空后它们会重新出现在菜单里（可撤销）。"):
                action = ("", "clear_dislike")

        if action:
            name, act = action
            prev_profile = prof.load_profile()
            prof.set_feedback(name, act, KNOWN_NAMES)
            if act == "clear_like":
                text = "🧹 已清空「喜欢」列表（可撤销）"
            elif act == "clear_dislike":
                text = "🧹 已清空「不喜欢」列表（可撤销）"
            elif act == "remove":
                text = f"✖ 已把「{name}」恢复为未表态"
            elif act == "like":
                text = f"❤️ 已把「{name}」移入「喜欢」"
            else:
                text = f"🚫 已把「{name}」移入「不喜欢」"
            record_change(text, prev_profile)
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
            prev_profile = prof.load_profile()
            prof.bulk_feedback(picks, act, KNOWN_NAMES)
            record_change(f"➕ 批量加入「{'喜欢' if add_like else '不喜欢'}」：{'、'.join(picks)}",
                          prev_profile)
            st.rerun()

        st.divider()
        c1, _ = st.columns([1, 3])
        with c1:
            if guarded_button("🗑️ 清空全部档案", "clear_all_btn",
                              f"确认要清空全部档案吗？「喜欢」{len(liked)} 道、「不喜欢」{len(disliked)} 道"
                              "都会一起清掉（清空后可以点「撤销」找回，但别关页面）。"):
                prev_profile = prof.load_profile()
                prof.clear_all()
                record_change("🗑️ 已清空全部档案（可撤销）", prev_profile)
                st.rerun()
        st.caption("档案只保存在这台机器上，不会上传；误清空可以点上面的「撤销」找回。")

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
            prev_profile = prof.load_profile()
            prof.set_feedback(name, action, KNOWN_NAMES)
            record_change(f"{'❤️ 喜欢' if action == 'like' else '🚫 不喜欢'}：{name}", prev_profile)
            st.rerun()


# ================================================================ 路由（任务栏）
_PAGE_RENDERERS = {
    "demand": render_demand,
    "menu": render_menu,
    "shopping": render_shopping,
    "profile": render_profile,
}
_PAGE_RENDERERS.get(st.session_state["page"], render_menu)()
