"""Recipe-Planner · Streamlit 界面（按 05 功能与界面交互设计实现）

四个页面（左栏任务栏导航）+ 一个动作页：
1) 今晚（默认首页，M1）：今晚吃什么 + 每道菜「这道不吃」+ 回家晚了 / 来客人了 + 开始做饭 / 做完了
2) 本周计划（M3）：7 天一览 + 点开某天看详情与逐道反馈 + 以前的方案
3) 买菜清单（M4）：到店模式（一行一样、打勾、已买沉底、勾选持久化）+ 带走清单（CSV / 文本 / A4）
4) 口味档案（M5）：喜欢 / 不喜欢的独立列表 + 来源痕迹 + 导出 / 清空 / 隐私
   另有动作页「排一周」（M2，不占导航）

文档依据：
- docs/05-功能与界面交互设计.md       功能与流程基线（页面结构、交互、文案、八条不变量）
- docs/06-界面设计规范.md             样式取值唯一来源（色板 / 字号 / 间距 / 组件）
- docs/04-界面逐屏标注-第三篇-…md     逐屏像素依据（P-01～P-13）
- docs/03-客户体验与界面改造建议-第二篇.md / docs/02-体验改进建议-第一篇.md  评审来源

四条硬规则（贯穿全站）：
1. 一支暖橙主色，红只用于「危险 / 错误」；
2. 颜色只表达状态，分类/耗时/难度等一律中性灰芯片（一屏色相 ≤ 4）；
3. emoji 退出图标系统（只保留 ❤️ 收藏 / 📌 定住两个符号，且不进按钮文字）；
4. 每行要么填满、要么只放一个全宽控件；一屏只有一个焦点。

运行：streamlit run app.py
"""
from __future__ import annotations

import json
import time

import streamlit as st

from recipe_planner import client as api
from recipe_planner import profile as prof
from recipe_planner import reporting as rep
from recipe_planner import store
from recipe_planner import ui_state as ui
from recipe_planner.core import (cheapest_swap, fastest_day, refresh_result,
                                 restore_day, swap_dish)
from recipe_planner.db import load_db
from recipe_planner.infra.settings import storage_kind as _storage_kind
from recipe_planner.infra.settings import use_api as _use_api
from recipe_planner.models import (
    ALLERGENS,
    BREAKFAST_MAX_TIME_DEFAULT,
    DISPLAY_CATEGORIES,
    DISH_MAX,
    GOALS,
    MEAL,
    MEAL_DISH_DEFAULTS,
    MEALS,
    SPICE_LEVELS,
    TASTE_TAGS,
    UserConstraints,
)

# USE_API（docs/09 P1-7）：数据源与进度来源的开关。
# - 0（默认）：界面直连领域层，排菜在本进程的线程里跑，今晚状态在进程内算；
# - 1：数据读写走 `store`/`profile` 末尾分派到的 HTTP 客户端，排菜交给服务端的任务，
#      进度由服务端推（SSE），今晚状态也由服务端产出。
USE_API = _use_api()

# 多餐方案的持久化（docs/10 第④步）：0002 迁移把餐次落在真正需要它的地方
# （`plan_dish.meal` + `plan_day` 的"哪几顿"两列），JSON 与数据库两种后端现在都支持。
# 老库要先跑一次 `python -m recipe_planner.storage.migrate`（幂等）把那三列加上。
MEALS_AVAILABLE = True

if USE_API:
    PlanJob = api.PlanJob
    tonight_view = api.tonight_view
else:
    from recipe_planner.progress import PlanJob
    from recipe_planner.tonight import tonight_view

st.set_page_config(page_title="晚餐规划 · 一周菜单与买菜清单", page_icon="🍳", layout="wide")

# ---------------------------------------------------------------- 设计 token 与组件样式
st.markdown(
    """
    <style>
    :root{
      --brand:#E8663C; --brand-soft:#FDEEE8; --brand-line:#F1D2C4; --brand-ink:#9A3412;
      --bg:#FAF8F5; --panel:#FFFFFF; --line:#ECE7DF; --soft:#F3F1EC;
      --ink:#1F2937; --ink2:#6B7280; --ink3:#9CA3AF;
      --ok:#2F855A; --ok-soft:#EAF5EF; --warn:#B7791F; --danger:#C53030;
    }
    /* 页面骨架：暖白底、留白成系统（第二篇 4.1/4.2、V-05） */
    .stApp{ background:var(--bg); }
    .block-container{ padding-top:2.4rem; padding-bottom:3.5rem; max-width:1180px; }
    /* 字号阶梯：28/20/17/15/13，中文行高 1.65，不再出现 12px 以下（V-04） */
    h1{ font-size:28px !important; font-weight:700 !important; line-height:1.35 !important;
        color:var(--ink) !important; margin-bottom:.2rem !important; }
    h2,h3{ font-size:20px !important; font-weight:600 !important; line-height:1.4 !important;
           color:var(--ink) !important; margin:1.5rem 0 .4rem !important; }
    h4{ font-size:17px !important; font-weight:600 !important; color:var(--ink) !important;
        margin:1rem 0 .2rem !important; }
    p, li, .stMarkdown{ font-size:15px; line-height:1.65; color:var(--ink); }
    div[data-testid="stCaptionContainer"] p{ font-size:13px !important; line-height:1.55 !important;
        color:var(--ink2) !important; }
    /* 控件：看得见的边界 + 同行等宽（P-06 / P-08） */
    div[data-testid="stTextInput"] input, div[data-testid="stNumberInput"] input,
    div[data-testid="stDateInput"] input, div[data-baseweb="select"] > div{
      border:1px solid var(--line) !important; border-radius:10px !important;
      background:var(--panel) !important;
    }
    div[data-testid="stNumberInput"], div[data-testid="stDateInput"],
    div[data-testid="stTextInput"], div[data-testid="stSelectbox"],
    div[data-testid="stMultiSelect"]{ width:100% !important; }
    div[data-testid="stNumberInput"] input{ width:100% !important; }
    div[data-testid="stWidgetLabel"] p{ font-size:14px !important; font-weight:500;
        color:var(--ink) !important; }
    /* 按钮：主=暖橙实心，次=白底描边（红只留给危险） */
    div[data-testid="stButton"] button{ border-radius:10px; font-size:15px; min-height:2.5rem;
        padding:.35rem .9rem; }
    div[data-testid="stButton"] button[kind="secondary"]{ background:var(--panel);
        border:1px solid var(--line); color:var(--ink); }
    div[data-testid="stButton"] button[kind="secondary"]:hover{ border-color:var(--brand-line);
        color:var(--brand-ink); }
    div[data-testid="stButton"] button[kind="primary"]{ background:var(--brand);
        border:1px solid var(--brand); color:#fff; font-weight:600; }
    div[data-testid="stDownloadButton"] button{ border-radius:10px; border:1px solid var(--line);
        background:var(--panel); color:var(--ink); min-height:2.5rem; }
    /* 侧栏：只做导航（V-13 / P-02） */
    section[data-testid="stSidebar"]{ background:var(--panel); border-right:1px solid var(--line); }
    section[data-testid="stSidebar"] div[data-testid="stButton"] button{
      min-height:3.2rem; font-size:16px; font-weight:600; justify-content:flex-start;
      padding-left:.9rem; margin-bottom:2px; }
    .brand{ font-size:20px; font-weight:700; color:var(--ink); }
    .brand-sub{ font-size:13px; color:var(--ink2); margin:2px 0 16px; }
    /* 芯片：只表达信息，一律中性灰（V-02） */
    .chip{ display:inline-block; background:var(--soft); color:#4B5563; border-radius:6px;
           padding:2px 8px; margin:0 6px 4px 0; font-size:13px; line-height:1.5; }
    .chip-brand{ background:var(--brand-soft); color:var(--brand-ink); }
    /* 菜品卡：等高四段（4.5 / V-06、V-07） */
    .dish-card{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
                padding:14px 16px; margin-bottom:12px; min-height:126px;
                display:flex; flex-direction:column; }
    .dish-card.loved{ border-color:var(--brand-line); background:var(--brand-soft); }
    .dish-card.hated{ background:#F4F3F1; border-color:#E4E1DC; }
    .dish-card.hated .dish-name{ color:var(--ink2); }
    .dish-row{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
    .dish-name{ font-size:17px; font-weight:600; color:var(--ink); }
    .dish-meta{ margin-top:6px; }
    .dish-reason{ margin-top:auto; font-size:15px; line-height:1.6; color:var(--ink2);
                  background:#FBFAF8; border-radius:8px; padding:8px 10px;
                  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
                  overflow:hidden; }
    .state{ font-size:13px; font-weight:600; white-space:nowrap; }
    .state.loved{ color:var(--brand-ink); }
    .state.hated{ color:var(--ink2); }
    /* 今晚大卡：首屏唯一焦点（4.6 / V-08） */
    .hero{ background:var(--panel); border:1px solid var(--brand-line);
           border-left:5px solid var(--brand); border-radius:14px; padding:16px 20px; }
    .hero-kicker{ font-size:13px; font-weight:600; color:var(--brand-ink); letter-spacing:.03em; }
    .hero-dishes{ font-size:22px; font-weight:700; color:var(--ink); margin:6px 0 4px;
                  line-height:1.45; }
    .hero-meta{ font-size:15px; color:var(--ink2); }
    .hero-reason{ margin-top:10px; font-size:15px; line-height:1.6; color:var(--ink2);
                  background:#FBFAF8; border-radius:8px; padding:8px 10px; }
    /* 一行数字（V-09：不要一排骨架卡） */
    .statrow{ display:flex; flex-wrap:wrap; gap:34px; align-items:baseline; margin:8px 0 2px; }
    .stat .n{ font-size:26px; font-weight:700; line-height:1.2; color:var(--ink); }
    .stat .l{ font-size:13px; color:var(--ink2); }
    .stat.ok .n{ color:var(--ok); } .stat.warn .n{ color:var(--warn); }
    .stat.bad .n{ color:var(--danger); }
    /* 整周总览：等宽天行，今天高亮、已过变淡（4.6 / E-01） */
    .day-wrap{ display:flex; flex-direction:column; gap:8px; margin-top:6px; }
    .day-row{ display:flex; align-items:center; gap:14px; background:var(--panel);
              border:1px solid var(--line); border-radius:10px; padding:10px 14px; }
    .day-row.today{ background:var(--brand-soft); border-color:var(--brand-line); }
    .day-row.past{ opacity:.55; }
    .day-when{ flex:0 0 148px; font-size:15px; font-weight:600; color:var(--ink); }
    .day-dishes{ flex:1 1 auto; font-size:15px; color:var(--ink); }
    .day-meta{ flex:0 0 148px; text-align:right; font-size:13px; color:var(--ink2); }
    /* 提示：一行文字，不用彩色横幅（V-08 / 4.6） */
    .line{ font-size:13px; color:var(--ink2); margin:.3rem 0; }
    .line.ok{ color:var(--ok); } .line.bad{ color:var(--danger); font-weight:600; }
    /* 清单：分类条 + 到店模式（4.8 / V-10） */
    .cat-bar{ display:inline-block; background:var(--soft); color:#4B5563; border-radius:8px;
              padding:3px 10px; font-size:13px; font-weight:600; margin:14px 0 4px; }
    /* 骨架屏（4.10） */
    .skeleton{ height:110px; border-radius:12px; margin-bottom:12px;
               background:linear-gradient(90deg,#F1EFEA 25%,#F7F5F1 37%,#F1EFEA 63%);
               background-size:400% 100%; animation:sk 1.4s ease infinite; }
    @keyframes sk{ 0%{background-position:100% 50%} 100%{background-position:0 50%} }
    /* A4 打印版式（4.9 / V-12） */
    .a4{ background:#fff; color:#111; border:1px solid var(--line); border-radius:12px;
         padding:22px 24px; font-size:12pt; line-height:1.5; }
    .a4-title{ font-size:16pt; font-weight:700; }
    .a4-sub{ font-size:10pt; color:#555; margin:2px 0 10px; }
    .a4-title2{ font-size:13pt; font-weight:700; margin:14px 0 6px; }
    .a4-table{ width:100%; border-collapse:collapse; font-size:10.5pt; }
    .a4-table th,.a4-table td{ border:1px solid #999; padding:5px 7px; text-align:left; }
    .a4-table th{ background:#F2F2F2; font-weight:700; }
    .a4-n{ text-align:right; white-space:nowrap; }
    .a4-cols{ columns:2; column-gap:26px; font-size:10.5pt; }
    .a4-item{ margin-bottom:5px; break-inside:avoid; }
    .a4-amt{ color:#555; margin-left:6px; font-size:9.5pt; }
    .a4-foot{ margin-top:12px; padding-top:6px; border-top:1px solid #999; font-size:9.5pt;
              color:#444; }
    @media print{
      /* 打印时隐藏页面上的其它一切，只留 A4 那一块（.a4 由 printable_html 输出） */
      body *{ visibility:hidden !important; }
      .a4, .a4 *{ visibility:visible !important; }
      .a4{ position:absolute; left:0; top:0; width:100%; border:0; padding:0; border-radius:0; }
      @page{ size:A4; margin:14mm; }
    }
    /* 响应式：一套组件只切换列数（4.7 / V-16） */
    .st-key-mobile_nav{ display:none; }
    @media (min-width:641px) and (max-width:1024px){
      div[data-testid="stHorizontalBlock"] > div[data-testid="column"]{
        flex:1 1 45% !important; min-width:45% !important; }
      [class*="st-key-dishacts_"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"],
      .st-key-mobile_nav div[data-testid="stHorizontalBlock"] > div[data-testid="column"]{
        flex:1 1 28% !important; min-width:28% !important; }
    }
    @media (max-width:640px){
      div[data-testid="stHorizontalBlock"]{ flex-wrap:wrap !important; }
      div[data-testid="stHorizontalBlock"] > div[data-testid="column"]{
        flex:1 1 100% !important; min-width:100% !important; }
      [class*="st-key-dishacts_"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"],
      .st-key-mobile_nav div[data-testid="stHorizontalBlock"] > div[data-testid="column"]{
        flex:1 1 30% !important; min-width:30% !important; }
      .block-container{ padding-top:1.4rem; padding-bottom:104px; }
      .day-when{ flex:1 1 100%; } .day-dishes{ flex:1 1 100%; }
      .day-meta{ flex:1 1 100%; text-align:left; }
      .statrow{ gap:18px; }
      .st-key-mobile_nav{ display:block; position:fixed; left:0; right:0; bottom:0; z-index:999;
        background:var(--panel); border-top:1px solid var(--line); padding:8px 10px 4px; }
      div[data-testid="stTabs"] [data-baseweb="tab-list"]{ overflow-x:auto; white-space:nowrap; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

db = load_db()
KNOWN_NAMES = {r.name for r in db.recipes}
NAME2ID = {r.name: r.id for r in db.recipes}

# ---------------------------------------------------------------- 服务端连接守卫
# 服务化之后，界面最可能的故障不是"某个操作失败"，而是"服务端根本没起来"。
# 那种情况下每个请求都会失败，如果放任下去客户看到的是一屏英文异常 ——
# 所以这里先探一次，探不到就用**一页人话**说清怎么办（05 §5.1：失败要给下一步）。
if USE_API:
    api.begin_rerun()          # 一次脚本运行 = 一次缓存周期（客户端不猜 TTL）
    if not api.ping():
        st.title("连不上排菜服务")
        st.markdown(
            "<div class='hero'><div class='hero-kicker'>界面现在走服务端</div>"
            "<div class='hero-dishes'>排菜服务没起来</div>"
            f"<div class='hero-meta'>界面连的是 {api.base_url()}。"
            "在项目目录里执行下面这条命令把服务端启起来，再刷新这一页。</div></div>",
            unsafe_allow_html=True)
        st.code("python -m uvicorn recipe_planner.api.main:app "
                "--host 127.0.0.1 --port 8000", language="powershell")
        st.caption("不想走服务端：把环境变量 USE_API 设成 0 再启动界面，就回到直连模式。")
        if st.button("启好了，重试", key="api_retry", type="primary"):
            st.rerun()
        st.stop()

# ---------------------------------------------------------------- 示例场景（三张场景卡）
SCENES = [
    ("light", "清淡减脂 3 天", "2 人 · 3 天 · 单菜 ≤40 分钟 · 本周 ¥270",
     dict(people=2, days=3, spice="不辣", goal="减脂", taste=["清淡"], skill="随便",
          max_time=40, budget_week=270.0, allergens=[], pantry_list=["鸡蛋", "西红柿"],
          cook_start="18:30")),
    ("allergy", "海鲜过敏 + 控糖", "3 人 · 4 天 · 单菜 ≤35 分钟 · 本周 ¥480",
     dict(people=3, days=4, spice="不辣", goal="控糖", taste=["清淡"], skill="随便",
          max_time=35, budget_week=480.0, allergens=["海鲜"], pantry_list=[],
          cook_start="18:30")),
    ("spicy", "无辣不欢 · 省钱 5 天", "2 人 · 5 天 · 单菜 ≤30 分钟 · 本周 ¥250",
     dict(people=2, days=5, spice="辣", goal="省钱", taste=["下饭"], skill="新手",
          max_time=30, budget_week=250.0, allergens=[], pantry_list=["土豆"],
          cook_start="18:30")),
]

# ---------------------------------------------------------------- 会话状态
ui.init_defaults()
for _k, _v in dict(
    people=2, days=3, spice="不辣", max_time=40, goal="随便", budget_week=0.0,
    allergens=[], taste=[], pantry_list=[], dishes_per_day=2,
    skill="随便", cook_start="18:30",
    # docs/10：默认只做晚餐 —— 与"全天菜单"之前的行为**完全一致**（回归面为零的原因）
    meals=[MEAL],
    breakfast_max_time=BREAKFAST_MAX_TIME_DEFAULT,
    **{f"dishes_{_m}": MEAL_DISH_DEFAULTS[_m] for _m in MEALS},
).items():
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("start_date", store.next_monday())
st.session_state.setdefault("plan_start", None)
st.session_state.setdefault("export_open", False)
st.session_state.setdefault("print_preview", False)
st.session_state.setdefault("notice_toast_id", None)


def _sync_widgets_from_inputs(inp: dict) -> None:
    """把约束写回表单控件（必须在控件创建之前调用）。"""
    _people = int(inp.get("people", 2))
    _days = int(inp.get("days", 3))
    _ppd = inp.get("budget_per_person_day")
    st.session_state["people"] = _people
    st.session_state["days"] = _days
    st.session_state["dishes_per_day"] = int(inp.get("dishes_per_day", 2))
    # 餐次（docs/10）：老存档里没有这两个字段 → 回落到"只做晚餐 + 都用 dishes_per_day"
    st.session_state["meals"] = list(inp.get("meals") or [MEAL])
    _dpm = dict(inp.get("dishes_per_meal") or {})
    for _m in MEALS:
        st.session_state[f"dishes_{_m}"] = int(_dpm.get(_m) or inp.get("dishes_per_day", 2))
    st.session_state["breakfast_max_time"] = int(
        inp.get("breakfast_max_time_min", BREAKFAST_MAX_TIME_DEFAULT))
    st.session_state["allergens"] = list(inp.get("allergens") or [])
    st.session_state["spice"] = inp.get("spice", "不辣")
    st.session_state["taste"] = list(inp.get("taste_tags") or [])
    st.session_state["goal"] = inp.get("goal", "随便")
    st.session_state["skill"] = inp.get("skill", "随便")
    st.session_state["cook_start"] = inp.get("cook_start", "18:30")
    st.session_state["max_time"] = int(inp.get("max_time_min", 40))
    st.session_state["budget_week"] = round(_ppd * _people * _days) if _ppd else 0.0
    st.session_state["pantry_list"] = list(inp.get("pantry_items") or [])
    st.session_state["start_date"] = store.normalize_start(inp.get("start_date"))


def _picked_meals(inp: dict) -> list[str]:
    """这一周吃哪几顿（按 早→午→晚 排序；一个都没勾就回落到只做晚餐）。"""
    picked = set(inp.get("meals") or [])
    return [m for m in MEALS if m in picked] or [MEAL]


def _dishes_per_meal(inp: dict) -> dict[str, int]:
    """每餐几道菜：表单按餐各有一个值，老存档没有就统一用 `dishes_per_day`。"""
    saved = dict(inp.get("dishes_per_meal") or {})
    fallback = int(inp.get("dishes_per_day", 2))
    return {m: int(saved.get(m) or fallback) for m in MEALS}


def _form_meals() -> list[str]:
    """表单里当前勾选的餐次（渲染文案用，例如"每天 N 顿"）。"""
    picked = set(st.session_state.get("meals") or [])
    return [m for m in MEALS if m in picked] or [MEAL]


def build_constraints(inp: dict) -> UserConstraints:
    liked = prof.liked_names(KNOWN_NAMES)
    disliked = prof.disliked_names(KNOWN_NAMES)
    return UserConstraints(
        people=inp["people"], days=inp["days"], dishes_per_day=int(inp["dishes_per_day"]),
        allergens=inp.get("allergens", []), spice_level=inp.get("spice", "不辣"),
        taste_tags=inp.get("taste_tags", []), goal=inp.get("goal", "随便"),
        max_time_min=inp.get("max_time_min", 40),
        skill=inp.get("skill", "随便"),
        cook_start=inp.get("cook_start", ""),
        budget_per_person_day=inp.get("budget_per_person_day"),
        pantry_items=inp.get("pantry_items", []),
        must_include_recipes=list(inp.get("must_include") or []),  # 「定住 / 加一道」的菜
        liked_dishes=[NAME2ID[n] for n in liked if n in NAME2ID],
        disliked_dishes=[NAME2ID[n] for n in disliked if n in NAME2ID],
        meals=_picked_meals(inp),                      # docs/10：吃哪几顿
        dishes_per_meal=_dishes_per_meal(inp),         # 每餐几道菜
        breakfast_max_time_min=int(inp.get("breakfast_max_time", BREAKFAST_MAX_TIME_DEFAULT)),
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
    rid = st.session_state.get("record_id")
    result = st.session_state.get("result")
    if not (rid and result is not None):
        return
    rec = store.update_result(rid, result)
    # 服务化模式下服务端才是权威：清单、花费、忌口冲突、下锅顺序都是它重算的，
    # 所以把会话里这一份换成它刚算好的那一份，免得"界面一套、库里一套"。
    # 本地/DB 模式**不换**：界面手里那一份信息更全（候选数、耗时、trace），换了反而是净损失。
    if USE_API and rec is not None and rec.result is not None:
        st.session_state["result"] = rec.result


# 首次打开：磁盘上有存档就直接呈现「我这一周」，而不是又一张空表单
if st.session_state["plan_inputs"] is None and st.session_state["result"] is None:
    _rec = store.latest_record()
    if _rec is not None:
        _load_record(_rec)
        st.session_state["revisit"] = _rec.id

# 按钮回调里不能直接改控件值（控件已实例化）→ 统一挂起，到脚本开头再生效
if st.session_state.get("pending_sync"):
    _sync_widgets_from_inputs(st.session_state.pop("pending_sync"))

# ---------------------------------------------------------------- 任务栏导航
# 五项：今晚（默认首页）/ 本周计划 / 排一周 / 买菜清单 / 口味档案。
# 「排一周」原本是动作页，按你的要求加回任务栏，作为独立入口常驻（05 §2.2 已同步）。
NAV_ITEMS = [
    ("tonight", "今晚"),
    ("plan", "本周计划"),
    ("create", "排一周"),
    ("shopping", "买菜清单"),
    ("profile", "口味档案"),
]
NAV_LABEL = dict(NAV_ITEMS)
if st.session_state.get("page") is None:
    st.session_state["page"] = "tonight"


def goto(key: str) -> None:
    st.session_state["page"] = key
    st.rerun()


def nav_button(key: str, prefix: str) -> bool:
    return st.button(NAV_LABEL[key], key=f"{prefix}{key}", use_container_width=True,
                     type="primary" if key == st.session_state["page"] else "secondary")


def nav_taskbar(prefix: str = "nav_", horizontal: bool = False) -> None:
    if horizontal:
        for col, (key, _label) in zip(st.columns(len(NAV_ITEMS)), NAV_ITEMS):
            with col:
                if nav_button(key, prefix):
                    goto(key)
    else:
        for key, _label in NAV_ITEMS:
            if nav_button(key, prefix):
                goto(key)


with st.sidebar:
    st.markdown("<div class='brand'>晚餐规划</div>"
                "<div class='brand-sub'>一周菜单 · 买菜清单</div>", unsafe_allow_html=True)
    nav_taskbar("nav_")
    _cur = store.get_record(st.session_state.get("record_id"))
    if _cur is not None:
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        st.caption(f"{_cur.label} · {_cur.created_at[:10]} 生成")

# 手机：底部固定导航（按窗口宽度自动显示，不再要用户手动开开关）
with st.container(key="mobile_nav"):
    nav_taskbar("navm_", horizontal=True)


# ================================================================ 公共小块
def _empty_state(msg: str, key: str) -> None:
    st.markdown(f"<p class='line'>{msg}</p>", unsafe_allow_html=True)
    if st.button("去排一周", key=key, type="primary"):
        goto("create")


def _notice_block(result) -> None:
    """操作回执：一行浅色文字 + 撤销（不再用彩色横幅，第二篇 4.10 / V-08）。"""
    notice = ui.get_notice()
    if not notice:
        return
    if st.session_state.get("notice_toast_id") is not notice:
        st.toast(notice["text"])
        st.session_state["notice_toast_id"] = notice
    c1, c2 = st.columns([5, 1])
    with c1:
        st.markdown(f"<p class='line'>{notice['text']}</p>", unsafe_allow_html=True)
    with c2:
        depth = ui.undo_depth()
        if depth and st.button(f"撤销({depth})", key="undo_btn", use_container_width=True,
                               help="可以连续点，最多回退 5 步"):
            entry = ui.pop_undo()
            if entry:
                if entry.get("days"):
                    result.days = [p.model_copy(deep=True) for p in entry["days"]]
                    refresh_result(result, db)
                    _commit_plan()
                if entry.get("profile") is not None:
                    prof.save_profile(entry["profile"])
                ui.set_notice("undo", f"已撤销：{entry['text']}")
                ui.push_history(f"撤销了「{entry['text']}」")
                st.session_state["stale"] = False
            st.rerun()


def _relax_options(c: UserConstraints, issues=(), swap_failed: bool = False) -> None:
    """H1：排不出来 / 换不动的时候，直接给可点击的下一步。"""
    codes = {i.code for i in issues}
    opts: list[tuple[str, str, object]] = []
    new_max = min(90, c.max_time_min + 20)
    if new_max > c.max_time_min and (swap_failed or codes & {"over_time", "shortage", "time"}):
        opts.append(("time", f"时长上限放宽到 {new_max} 分钟", new_max))
    if c.budget_per_person_day and (swap_failed or "over_budget" in codes):
        new_budget = round(c.budget_per_person_day + 20, 2)
        opts.append(("budget", f"预算加到 ¥{new_budget:.0f}/人·天", new_budget))
    day_no = (st.session_state.get("relax") or {}).get("day")
    if (swap_failed or "shortage" in codes) and day_no:
        opts.append(("drop_dish", f"第 {day_no} 天少排一道菜", day_no))
    if not opts:
        return
    st.markdown("<p class='line'>点一下就放宽，然后自动重排一版：</p>", unsafe_allow_html=True)
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
                        note = f"单菜时长上限已放宽到 {int(value)} 分钟，正在重排…"
                    else:
                        inp["budget_per_person_day"] = float(value)
                        note = f"预算已放宽到 ¥{float(value):.0f}/人·天，正在重排…"
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
    ui.set_notice("swap", f"第 {day_no} 天已去掉「{dropped}」，其余各天未改动。")
    ui.push_undo(f"第 {day_no} 天去掉「{dropped}」",
                 days=prev_days, record_id=st.session_state.get("record_id"))
    ui.push_history(f"第 {day_no} 天去掉「{dropped}」")


# ================================================================ 页面 1：今晚（M1，默认首页）
def _tonight_day_override() -> int | None:
    """「手动切到第 N 天」这个界面状态（只有界面知道，所以留在界面）。"""
    value = st.session_state.get("tonight_override")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _hero(view) -> None:
    """主角卡（今晚页五个状态共用）。

    **文案一个字都不在这里拼** —— kicker / headline / meta / reason 全部来自
    `tonight_view`（本地模式跑在进程内，服务化模式由服务端产出但返回同一个对象）。
    界面只负责摆版式，这样"这一周吃完了""今晚不做饭"这些说法永远只有一份。
    """
    st.markdown(
        f"<div class='hero'><div class='hero-kicker'>{view.kicker}</div>"
        f"<div class='hero-dishes'>{view.headline}</div>"
        + (f"<div class='hero-meta'>{view.meta}</div>" if view.meta else "")
        + (f"<div class='hero-reason'>{view.reason}</div>" if view.reason else "")
        + "</div>", unsafe_allow_html=True)


_RATE_TEXT = {"好吃": "已记住这几道好吃，以后会多安排。",
              "一般": "已记下，下次不会特意多排。",
              "下次不做": "已记住不再做这几道。"}


def _rate_tonight(day_no: int, score: int) -> None:
    """做完了打分（E-07）。

    服务化模式下交给服务端的 `/rate`：它一次事务里同时记「这天做过了」和写档案，
    不会出现"档案写了、标记没写"的半截状态；本地模式仍然是界面自己逐道写档案
    （用的是同一个纯函数规则）。撤销两边都靠档案快照，所以 undo 逻辑不用分叉。
    """
    label_ = {2: "好吃", 1: "一般", 0: "下次不做"}[score]
    rid = st.session_state.get("record_id")
    prev_profile = prof.load_profile()
    if USE_API:
        text = api.rate_day(rid, day_no, score).get("message") or _RATE_TEXT[label_]
    else:
        day_plan = next((p for p in st.session_state["result"].days if p.day == day_no), None)
        for d in (day_plan.dishes if day_plan else []):
            rec_r = db.by_id(d.recipe_id)
            if rec_r is not None:
                prof.rate(rec_r.name, score, KNOWN_NAMES, source="做完了打分")
        text = _RATE_TEXT[label_]
    ui.set_notice("info", text)
    ui.push_undo(text, profile=prev_profile, record_id=rid)
    ui.push_history(text)
    st.session_state["stale"] = True
    st.rerun()


def _extra_shopping(day_plan, db, extra_people: int) -> list[str]:
    """「来客人了」的临时补买清单：只算今晚这几道菜多出来的份量。"""
    out: list[str] = []
    for d in day_plan.dishes:
        r = db.by_id(d.recipe_id)
        if r is None:
            continue
        for ing in r.ingredients:
            if ing.grams and ing.category not in {"调料", "其他"}:
                grams = ing.grams * extra_people / 2.0
                if grams >= 50:
                    out.append(f"{ing.name} 多买约 {grams:.0f} 克")
    return out[:6]


def _quick_faster(day_no: int, meal: str | None = None) -> None:
    """回家晚了：只把这一顿换成最快能做完的组合（不碰其他餐）。"""
    result = st.session_state["result"]
    c = result.constraints
    prev_days = [p.model_copy(deep=True) for p in result.days]
    got = fastest_day(result.days, day_no, db, c, meal=meal)
    if got is None:
        st.session_state["tonight_relax"] = day_no
        ui.set_notice("info", "今晚这几道已经是最快的组合了，想更快可以放宽一点：")
        st.rerun()
    new_plans, recipes = got
    result.days = new_plans
    refresh_result(result, db)
    _commit_plan()
    names = "、".join(r.name for r in recipes)
    minutes = sum(r.time_min for r in recipes)
    text = f"已把今晚换成快手组合：「{names}」，约 {minutes} 分钟就能上桌，其他六天没动。"
    ui.set_notice("swap", text)
    ui.push_undo(text, days=prev_days, record_id=st.session_state.get("record_id"))
    ui.push_history(text)
    st.session_state["tonight_relax"] = None
    st.rerun()


def _quick_guests(day_no: int, extra: int, meal: str | None = None) -> None:
    """来客人了：只改这一顿的人数与份量，并给一条临时补买提醒（不动整周清单）。"""
    result = st.session_state["result"]
    day = result.slot(day_no, meal)          # 不能用 p.day == X（多餐时会取到早餐）
    if day is None:
        return
    prev_days = [p.model_copy(deep=True) for p in result.days]
    base = result.constraints.people
    day.people = base + extra
    extra_items = _extra_shopping(day, db, extra)
    refresh_result(result, db)
    _commit_plan()
    text = (f"今晚按 {base + extra} 人算（多 {extra} 人），其他天不变。"
            + ("临时要补买：" + "、".join(extra_items) + "。" if extra_items else ""))
    ui.set_notice("swap", text)
    ui.push_undo(text, days=prev_days, record_id=st.session_state.get("record_id"))
    ui.push_history(text)
    st.session_state["guests_for"] = None
    st.rerun()


def _mark_done(day_no: int, done: bool = True, meal: str | None = None) -> None:
    """做完了 / 再做一次（M1 状态③，记录写进存档，明天打开还在）。

    docs/10：给了 `meal` 就只记这一顿（做完了午饭不代表晚饭也做完了）。
    """
    rid = st.session_state.get("record_id")
    store.set_done(rid, day_no, done, meal=meal)
    st.session_state["done_days"] = set(store.get_record(rid).done_days) if rid else set()
    text = (f"已把第 {day_no} 天标记成做完了。" if done else f"第 {day_no} 天改回未做。")
    ui.set_notice("info", text)
    ui.push_history(text)
    st.rerun()


def render_tonight() -> None:
    # 只做晚餐时这一页仍然叫「今晚」（与以前一致）；勾了多顿时它就是"今天"
    _inp_meals = _picked_meals(st.session_state.get("plan_inputs") or {})
    st.title("今天" if len(_inp_meals) > 1 else "今晚")
    override = _tonight_day_override()

    # 状态④：还没有这一周的菜单 —— 全站唯一一次把表单摆到首屏（05 M1）。
    # 判断条件是"从没填过需求"（而不是"没有存档"）：填完点生成之后，
    # 正是这一页负责把菜真的排出来（见下面的 `_ensure_plan`）。
    if st.session_state["plan_inputs"] is None:
        _hero(tonight_view(None, db))
        if st.button("帮我排一周", key="tonight_start", type="primary"):
            goto("create")
        return

    result = _ensure_plan()
    if result is None:
        _empty_state("还没有菜单，先去排一周。", "tonight_empty")
        return

    c = result.constraints
    # 这一页该显示什么（状态①–⑤、该看第几天、要说什么话）**只由 tonight_view 一处说了算**。
    # 这里原来还有一份自己的判定，和 `tonight.py` 并存了两步；现在合并成一份 ——
    # 服务化模式下它由服务端算好给过来（docs/08 §5：措辞统一由后端产出）。
    record = store.get_record(st.session_state.get("record_id"))
    view = tonight_view(record, db, day=override)

    st.markdown(f"<p class='line'>方案 {view.week_label} · {c.people} 人"
                + (f" · {view.hint}" if view.hint else "") + "</p>", unsafe_allow_html=True)
    if override:
        if st.button("回到今晚", key="tonight_reset_day", use_container_width=True):
            st.session_state["tonight_override"] = None
            st.rerun()

    if view.state == "no_plan":
        # 走到这里说明存档没了或这一版是空的（例如被删掉、被别的会话删掉）。
        # 不硬往下走（下面要用 result.days[0]），把引导卡给回来 —— 状态永远可见（R7）。
        _hero(view)
        return

    day_plan = next((p for p in result.days if p.day == view.day), result.days[0])
    _hero(view)

    # 状态⑤：这一周已经结束（05 M1 状态⑤）
    if view.state == "week_over":
        w1, w2, _sp = st.columns([1, 1, 3])
        with w1:
            if st.button("照上周", key="tonight_reuse_prev", type="primary",
                         use_container_width=True, help="用上一版的需求重新排一版"):
                prev_rec = store.previous_record(st.session_state.get("record_id"))
                if prev_rec is not None:
                    _load_record(prev_rec)
                st.session_state["stale"] = True
                st.session_state["job"] = None
                ui.set_notice("info", "已照上一版重新排了一版。")
                st.rerun()
        with w2:
            if st.button("重新排", key="tonight_replan", use_container_width=True):
                goto("create")
        nav_to_plan = st.button("看这一周", key="tonight_view_week", use_container_width=True)
        if nav_to_plan:
            goto("plan")
        return

    # 状态②：今天已跳过
    if view.state == "skipped":
        b1, b2, _sp = st.columns([1, 1, 3])
        with b1:
            if st.button("改回来做", key=f"unskip_{view.day}", type="primary",
                         use_container_width=True):
                result.days = restore_day(result.days, view.day, db, c)
                refresh_result(result, db)
                _commit_plan()
                ui.set_notice("swap", f"第 {view.day} 天恢复做饭，其他天没动。")
                st.rerun()
        with b2:
            if st.button("看这一周", key="skipped_to_plan", use_container_width=True):
                goto("plan")
        return

    # 状态③：今天已做过（含 E-07 做完之后打一分）
    if view.state == "done":
        r1, r2, r3, _sp = st.columns([1, 1, 1, 3])
        for col, (label_, score) in zip((r1, r2, r3),
                                        (("好吃", 2), ("一般", 1), ("下次不做", 0))):
            with col:
                if st.button(label_, key=f"rate_{score}", type="primary" if score == 2 else "secondary",
                             use_container_width=True):
                    _rate_tonight(view.day, score)
        b1, b2, _sp2 = st.columns([1, 1, 3])
        with b1:
            nxt = view.day % len(result.days) + 1
            if st.button("看看明天", key="done_next_day", use_container_width=True):
                st.session_state["tonight_override"] = nxt
                st.rerun()
        with b2:
            if st.button("再做一次", key="done_undo", use_container_width=True):
                _mark_done(view.day, False, view.meal)
        return

    # 状态①：今天有安排 —— 英雄卡已经在上面画好了，这里只画每道菜与快改
    if view.state != "planned":       # 兜底：真出了没见过的状态也别白屏（R7：状态永远可见）
        _empty_state("这一页暂时没有可显示的内容，先去排一周。", "tonight_unknown")
        return

    # 每道菜一行：菜名 + 这道不想吃（05 M1-3）
    for dish in day_plan.dishes:
        r = db.by_id(dish.recipe_id)
        if r is None:
            continue
        d1, d2 = st.columns([4, 1])
        with d1:
            st.markdown(f"<p style='margin:.35rem 0'>**{r.name}**　"
                        f"<span class='line'>{r.time_min} 分钟 · {r.difficulty}</span></p>",
                        unsafe_allow_html=True)
        with d2:
            if st.button("这道不吃", key=f"tonight_dislike_{r.id}", use_container_width=True,
                         help="只换这一道，并记进口味档案"):
                prev_days = [p.model_copy(deep=True) for p in result.days]
                prev_profile = prof.load_profile()
                prof.set_feedback(r.name, "dislike", KNOWN_NAMES, source="今晚页")
                new_days, rep_recipe = swap_dish(result.days, view.day, r.id, db, c)
                if rep_recipe:
                    result.days = new_days
                    refresh_result(result, db)
                    _commit_plan()
                    text = (f"已把今晚的「{r.name}」换成「{rep_recipe.name}」，"
                            "并记住你以后不想吃它（其他天没动）。")
                else:
                    text = f"已记住你不想吃「{r.name}」，但今晚暂时没有可替换的菜。"
                ui.set_notice("dislike", text)
                ui.push_undo(text, days=prev_days, profile=prev_profile,
                             record_id=st.session_state.get("record_id"))
                ui.push_history(text)
                st.rerun()

    # 两个整卡级快改（05 §1.2 A）
    if st.session_state.get("guests_for") == view.day:
        st.markdown("<p class='line'>今晚来几位？（只影响今晚的份量）</p>",
                    unsafe_allow_html=True)
        g1, g2, g3, _sp = st.columns([1, 1, 1, 3])
        with g1:
            if st.button("多 2 人", key="guests_2", type="primary", use_container_width=True):
                _quick_guests(view.day, 2, view.meal)
        with g2:
            if st.button("多 4 人", key="guests_4", use_container_width=True):
                _quick_guests(view.day, 4, view.meal)
        with g3:
            if st.button("取消", key="guests_cancel", use_container_width=True):
                st.session_state["guests_for"] = None
                st.rerun()
    else:
        q1, q2, q3, _sp = st.columns([1, 1, 1, 3])
        with q1:
            if st.button("回家晚了", key="quick_faster", use_container_width=True,
                         help="只把这一顿换成最快能做完的组合"):
                _quick_faster(view.day, view.meal)
        with q2:
            if st.button("来客人了", key="quick_guests", use_container_width=True,
                         help="只改今晚的人数与份量，其他天不动"):
                st.session_state["guests_for"] = view.day
                st.rerun()
        with q3:
            if st.button("做完了", key="quick_done", use_container_width=True,
                         help="标记今天做完了，明天打开还记着"):
                _mark_done(view.day, True, view.meal)

    # 开始做饭 → 展开下锅顺序（05 M1-5）
    order, has_slow = rep.cook_order(day_plan, db)
    if order:
        if st.session_state.get("show_order_for") == view.day:
            st.markdown("<p class='line'>照这个顺序来：</p>", unsafe_allow_html=True)
            for line in order:
                st.markdown(f"- {line}")
            if has_slow:
                st.markdown("<p class='line'>有汤/炖菜可以先上火，边炖边做别的。</p>",
                            unsafe_allow_html=True)
            if st.button("收起", key="order_hide", use_container_width=True):
                st.session_state["show_order_for"] = None
                st.rerun()
        elif st.button("开始做饭", key="order_show", type="primary"):
            st.session_state["show_order_for"] = view.day
            st.rerun()

    # 换不动时的放宽选项（R4）
    if st.session_state.get("tonight_relax") == view.day:
        _relax_options(c, result.issues, swap_failed=True)
    elif st.session_state.get("tonight_relax"):
        st.session_state["tonight_relax"] = None

    _notice_block(result)
    st.markdown(f"<p class='line'>明天、后天吃什么 → 「本周计划」；"
                f"买菜 → 「买菜清单」；口味 → 「口味档案」。</p>", unsafe_allow_html=True)


# ================================================================ 页面 3：排一周（M2）
def render_create() -> None:
    st.title("排一周")
    st.caption("填完点生成，我给你一周的菜单和买菜清单。")

    back1, back2, _sp = st.columns([1, 1, 3])
    with back1:
        if st.button("回到今晚", key="create_back_tonight", use_container_width=True):
            goto("tonight")
    with back2:
        if st.session_state["result"] is not None and st.button(
                "看这一周", key="create_back_plan", use_container_width=True):
            goto("plan")

    # 方案状态条：一行中性小字 + 文字链接（P-04）
    if st.session_state["result"] is not None:
        _rec = store.get_record(st.session_state.get("record_id"))
        _label = store.week_label(st.session_state.get("plan_start")
                                  or st.session_state["start_date"])
        _res = st.session_state["result"]
        _sum = rep.plan_summary(_res, db, st.session_state.get("plan_start"))
        _c = _res.constraints
        st.markdown(
            "<p class='line'>已有方案 " + _label
            + (f" · {_rec.created_at[:10]} 生成" if _rec is not None else "")
            + f"　·　本周 ¥{_sum.total_cost:.0f}"
            + (f" / 预算 ¥{_sum.budget_total:.0f}" if _sum.budget_total else "")
            + f"　·　命中喜好 {_sum.liked_hit} 道"
            + "　·　" + ("已避开你的忌口" if _c.allergens else "未设忌口")
            + "；再生成一次会另存为新的一版，旧版在「以前的方案」里。"
            + "</p>",
            unsafe_allow_html=True)

    # 快速开始：三张场景卡（首屏的图形锚点，P-05 / P-13）
    st.markdown("### 快速开始（点一下就用这个场景）")
    for col, (key, title, desc, vals) in zip(st.columns(3), SCENES):
        with col:
            with st.container(border=True, key=f"scene_{key}"):
                st.markdown(f"**{title}**")
                st.caption(desc)
                if st.button("用这个场景", key=f"scene_btn_{key}", use_container_width=True):
                    for k, v in vals.items():
                        st.session_state[k] = v
                    # 场景卡都是"只做晚餐、每顿 2 道"（与 docs/10 之前的表现一致）
                    st.session_state["meals"] = [MEAL]
                    st.session_state["dishes_per_day"] = 2
                    for _m in MEALS:
                        st.session_state[f"dishes_{_m}"] = 2
                    st.rerun()

    st.markdown("### 我的需求")
    with st.form("planner_form"):
        # ① 人数与天数
        st.markdown("#### 人数与天数")
        g1 = st.columns(2)
        with g1[0]:
            people = st.number_input("几人吃", 1, 10, key="people")
        with g1[1]:
            days = st.slider(f"排几天（每天 {len(_form_meals())} 顿）", 1, 7, key="days")

        # ①.5 吃哪几顿（docs/10）：默认只做晚餐 —— 不勾别的就等于以前的行为
        st.markdown("#### 吃哪几顿")
        gm = st.columns(4)
        with gm[0]:
            meals = st.multiselect("这一周吃哪几顿", MEALS, key="meals",
                                   placeholder=f"不选＝只做{MEAL}",
                                   disabled=not MEALS_AVAILABLE)
        _picked_form = [m for m in MEALS if m in set(meals or [])] or [MEAL]
        if len(_picked_form) > 1 and not USE_API and _storage_kind() == "db":
            st.caption("老库要先跑一次迁移（幂等）：`python -m recipe_planner.storage.migrate`")
        for _i, _m in enumerate(_picked_form):
            with gm[_i + 1]:
                st.select_slider(f"{_m}几个菜", list(range(1, DISH_MAX + 1)), key=f"dishes_{_m}")
        if len(_picked_form) > 1:
            _bcol = st.columns(2)
            with _bcol[0]:
                st.slider("早餐单菜耗时上限（分钟）", 5, 30, key="breakfast_max_time")
            with _bcol[1]:
                st.caption("早餐默认只排 1 道快手菜；预算仍是**每人每天**，覆盖当天所有餐。")

        # ② 口味与忌口（四列填满，不留空洞，P-07）
        st.markdown("#### 口味与忌口")
        g2 = st.columns(4)
        with g2[0]:
            spice = st.radio("能接受的辣度", SPICE_LEVELS, horizontal=True, key="spice")
        with g2[1]:
            goal = st.selectbox("目标", GOALS, key="goal")
        with g2[2]:
            allergens = st.multiselect("过敏原 / 忌口（硬排除）", ALLERGENS, key="allergens")
        with g2[3]:
            taste = st.multiselect("口味偏好（尽量满足）", TASTE_TAGS, key="taste")
            st.caption("喜欢 / 不喜欢的菜可在「口味档案」里随时改")   # 就地提示（P-11）

        # ③ 时间与预算 / 厨艺（B1 预算用"本周总预算"、B2 开饭倒推、D4 厨艺）
        st.markdown("#### 时间与预算")
        g3 = st.columns(3)
        with g3[0]:
            max_time = st.slider("单菜耗时上限（分钟）", 10, 90, key="max_time")
        with g3[1]:
            budget_week = st.number_input("本周总预算（元，0=不限）", 0.0, 3000.0,
                                          step=50.0, key="budget_week")
            _ppl, _dys = int(st.session_state["people"]), int(st.session_state["days"])
            if budget_week and budget_week > 0 and _ppl and _dys:
                st.caption(f"≈ 每人每天 ¥{budget_week / (_ppl * _dys):.0f}"
                           f"　·　按 {_ppl} 人 × {_dys} 天折算")
            else:
                st.caption("按周去超市的话，直接填这一周想花多少钱")
        with g3[2]:
            skill = st.selectbox("你的厨艺", ["随便", "新手", "老手"], key="skill",
                                 help="新手会把「较难」的功夫菜排除掉")

        g3b = st.columns(3)
        with g3b[0]:
            start_date_pick = st.date_input("这一周从哪天开始", key="start_date")
        with g3b[1]:
            cook_start = st.text_input("我一般几点开始做饭", key="cook_start",
                                       placeholder="例如 18:30", help="用来倒推「几点能吃上」")
        with g3b[2]:
            st.caption("　")
            st.caption("填了开始时间，今晚页会告诉你大概几点能开饭")

        # ④ 家里已有（B4：标签式输入 + 常用食材快选 + 认没认出的回显）
        st.markdown("#### 家里已有")
        pantry_list = st.multiselect(
            "家里已有食材（会从买菜清单里扣掉）",
            sorted({i.name for r in db.recipes for i in r.ingredients}),
            key="pantry_list", accept_new_options=True,
            placeholder="可以直接选，也可以打字新建（如：鸡蛋、西红柿）",
        )
        if pantry_list:
            _all_ing = {i.name for r in db.recipes for i in r.ingredients}
            _known = [p for p in pantry_list if any(p in n or n in p for n in _all_ing)]
            _unknown = [p for p in pantry_list if p not in _known]
            st.caption("我认出了：" + ("、".join(_known) or "—")
                       + (f"　·　没认出的（不会扣减）：{'、'.join(_unknown)}" if _unknown else ""))

        # 提交区：提示在上、按钮右对齐（P-10）
        st.caption("生成大约 3–5 秒：先挑菜谱，再按你的忌口、预算和时间校验一遍。")
        _sp, sub_col = st.columns([2, 1])
        with sub_col:
            submitted = st.form_submit_button("生成菜单", type="primary", use_container_width=True)

    if submitted:
        _people, _days = int(people), int(days)
        _week = float(budget_week) if budget_week and budget_week > 0 else 0.0
        # 每餐几道菜（docs/10）：表单按餐各一个值；`dishes_per_day` 留成"当天最后一顿"的道数，
        # 它是老字段，导出的"每顿 N 道菜"那句话还在用它
        _dpm = {m: int(st.session_state.get(f"dishes_{m}") or 2) for m in MEALS}
        st.session_state["plan_inputs"] = dict(
            people=_people, days=_days, dishes_per_day=_dpm[_picked_form[-1]],
            meals=list(_picked_form), dishes_per_meal=_dpm,
            breakfast_max_time_min=int(st.session_state.get("breakfast_max_time",
                                                            BREAKFAST_MAX_TIME_DEFAULT)),
            allergens=list(allergens), spice=spice, taste_tags=list(taste), goal=goal,
            skill=skill, cook_start=cook_start,
            max_time_min=int(max_time),
            budget_per_person_day=round(_week / (_people * _days), 2) if _week else None,
            pantry_items=list(pantry_list),
            start_date=str(start_date_pick),
        )
        st.session_state["stale"] = True
        st.session_state["job"] = None
        st.session_state["relax"] = None
        st.session_state["notice"] = None
        # 05 M2：首次排完看「今晚」，已有方案时重排就去「本周计划」看整周
        st.session_state["page"] = "plan" if st.session_state["result"] is not None else "tonight"
        st.rerun()


# ================================================================ 页面 2：本周菜单
def _ensure_plan():
    """按需重排；等待过程给骨架屏 + 阶段反馈，可随时停（C1 / 4.10）。"""
    inp = st.session_state["plan_inputs"]
    if inp is None:
        return None
    if st.session_state["result"] is not None and not st.session_state["stale"]:
        return st.session_state["result"]

    job = st.session_state.get("job")
    if job is None:
        job = PlanJob(build_constraints(inp), db, start_date=inp.get("start_date")).start()
        st.session_state["job"] = job
    if not job.done:
        st.markdown("<div class='skeleton'></div><div class='skeleton'></div>",
                    unsafe_allow_html=True)
        with st.status(job.stage, expanded=True):
            st.write("顺序是：**挑菜谱 → 搭配一周 → 检查忌口和预算 → 算清单**。")
            st.progress(job.progress)
            if st.button("停止", key="cancel_job", use_container_width=True,
                         help="停止这次生成，需求都还在"):
                job.cancel()
                st.session_state["job"] = None
                st.session_state["stale"] = False
                ui.set_notice("info", "已停止这次生成。需求都还在，改完再点「生成菜单」就行。")
                goto("create")
        time.sleep(0.35)
        st.rerun()

    st.session_state["job"] = None
    if job.result is None:
        _server_error = getattr(job, "error", None)      # USE_API=1 时这是服务端给的人话
        st.markdown("<p class='line bad'>这次没能排出菜单。下面点一下放宽条件再试：</p>",
                    unsafe_allow_html=True)
        if _server_error:
            st.caption(f"服务端说：{_server_error}")
        _relax_options(build_constraints(inp), [], swap_failed=True)
        st.session_state["stale"] = False
        st.stop()

    result = job.result
    if getattr(job, "record", None) is not None:
        # USE_API=1：这一版方案是**服务端的排菜任务直接存库**的，界面不该再存第二份；
        # 回执里说的"上一版"是提交任务之前读到的那一份（客户端存下来的）。
        prev_rec, rec = job.previous, job.record
    else:
        prev_rec = store.latest_record()
        rec = store.save_plan(result, start_date=inp.get("start_date"),
                              change_note="重新排了一版" if prev_rec else "首次生成")
    st.session_state["result"] = result
    st.session_state["stale"] = False
    st.session_state["check_epoch"] += 1
    st.session_state["done_days"] = set()
    st.session_state["record_id"] = rec.id
    st.session_state["plan_start"] = rec.start_date
    st.session_state["revisit"] = None
    text = f"已排好 {rec.label} 的 {len(result.days)} 天晚餐，关掉页面明天打开还在。"
    if prev_rec is not None:
        text += f"　这次是另存为新的一版，旧版「{prev_rec.label}」在「以前的方案」里。"
    ui.set_notice("save", text)
    ui.push_history(f"排好了「{rec.label}」")
    return result


def _dish_card(r, dish, is_loved: bool, is_hated: bool) -> str:
    """菜品卡：等高四段，芯片固定 3 个中性灰，状态用文字 + 底色（4.5 / V-06 / V-07）。"""
    state = ("<span class='state loved'>已收藏</span>" if is_loved
             else ("<span class='state hated'>已排除</span>" if is_hated else ""))
    third = r.spice_level if r.spice_level != "不辣" else r.category
    chips = (f"<span class='chip'>{r.time_min} 分钟</span>"
             f"<span class='chip'>{r.difficulty}</span>"
             f"<span class='chip'>{third}</span>")
    return (
        f"<div class='dish-card{' loved' if is_loved else (' hated' if is_hated else '')}'>"
        f"<div class='dish-row'><div class='dish-name'>{r.name}</div>{state}</div>"
        f"<div class='dish-meta'>{chips}</div>"
        f"<div class='dish-reason'>{dish.reason or '家常好味。'}</div>"
        "</div>")


def _dish_actions(plan_day: int, dish, is_loved: bool, is_hated: bool, locked: bool = False):
    """每道菜的操作：换一道 / 定住 / 喜欢 / 不喜欢（E-02 定住，去掉 emoji）。"""
    picked = None
    rid = dish.recipe_id
    with st.container(key=f"dishacts_{plan_day}_{rid}"):
        row = st.columns(4)
        with row[0]:
            if st.button("换一道", key=f"swap_{plan_day}_{rid}", use_container_width=True,
                         help="只换今晚这道，不动口味偏好"):
                picked = ("swap", plan_day, rid)
        with row[1]:
            if st.button("已定住" if locked else "定住", key=f"lock_{plan_day}_{rid}",
                         use_container_width=True,
                         help="定住这道菜：以后重排也会保留它"):
                picked = ("unlock" if locked else "lock", plan_day, rid)
        with row[2]:
            if st.button("已喜欢" if is_loved else "喜欢", key=f"like_{plan_day}_{rid}",
                         use_container_width=True, help="合口味：以后多安排这道菜"):
                picked = ("like", plan_day, rid)
        with row[3]:
            if st.button("已排除" if is_hated else "不喜欢", key=f"hate_{plan_day}_{rid}",
                         use_container_width=True, help="不合口味：换掉并记住"):
                picked = ("dislike", plan_day, rid)
    return picked


def render_plan() -> None:
    st.title("本周计划")
    if st.session_state["plan_inputs"] is None:
        _empty_state("还没有这一周的菜单。排一次就能看整周了。", "goto_demand_m")
        return

    result = _ensure_plan()
    if result is None:
        _empty_state("还没有这一周的菜单。", "goto_demand_m2")
        return

    c = result.constraints
    start_date = st.session_state.get("plan_start") or st.session_state["start_date"]
    summary = rep.plan_summary(result, db, start_date)
    label = store.week_label(start_date)
    liked_now = prof.liked_names(KNOWN_NAMES)
    hated_now = prof.disliked_names(KNOWN_NAMES)
    # 日期逻辑要的是**天数**不是顿数（docs/10）：一天三顿时 len(result.days)=21，
    # 直接传会把"今天是第几天""标签页数量"全算错。
    days_span = max(p.day for p in result.days)
    today_idx = store.today_index(start_date, days_span)

    # ---- 顶部：方案名 + 日期范围 + 重排 / 改需求（05 M3）
    t1, t2, t3, _sp = st.columns([3, 1, 1, 1])
    with t1:
        st.markdown(f"<p class='line'>{label} · {c.people} 人 · 共 {summary.dishes} 道菜"
                    + ("　·　上次排的那一份，已经帮你打开" if st.session_state.get("revisit") else "")
                    + "</p>", unsafe_allow_html=True)
    with t2:
        if st.button("重排一版", key="replan_btn", use_container_width=True,
                     help="按现在的口味与需求重新排一份（整周都会变，会存成新的一版）"):
            st.session_state["stale"] = True
            st.session_state["job"] = None
            st.rerun()
    with t3:
        if st.button("改需求", key="edit_inputs_btn", use_container_width=True,
                     help="回到表单改人数、预算、忌口等"):
            goto("create")
    if st.session_state.get("revisit"):
        b1, b2, _sp2 = st.columns([1, 1, 4])
        with b1:
            if st.button("就用这份", use_container_width=True):
                st.session_state["revisit"] = None
                st.rerun()
        with b2:
            if st.button("重新排一份", use_container_width=True):
                st.session_state["revisit"] = None
                goto("create")

    pending = None

    # ---- 三个整周级动作：哪里能省（E-05）/ 都满意（E-04）/ 分享视图（E-08）
    q1, q2, q3, _spq = st.columns([1, 1, 1, 2])
    with q1:
        if st.button("哪里能省", key="save_money_btn", use_container_width=True,
                     help="挑最贵的一道换成更便宜的，并告诉你这周省了多少"):
            got = cheapest_swap(result.days, db, c)
            if got is None:
                ui.set_notice("info", "这一周已经没有明显更省的换法了。")
                st.rerun()
            new_plans, day_no_s, old_r, new_r, saving = got
            prev_days = [p.model_copy(deep=True) for p in result.days]
            result.days = new_plans
            refresh_result(result, db)
            _commit_plan()
            text = (f"把第 {day_no_s} 天的「{old_r.name}」换成「{new_r.name}」，"
                    f"这周省了约 ¥{saving:.0f}（其他天没动）。")
            ui.set_notice("swap", text)
            ui.push_undo(text, days=prev_days, record_id=st.session_state.get("record_id"))
            ui.push_history(text)
            st.rerun()
    with q2:
        if st.button("都满意", key="all_good_btn", use_container_width=True,
                     help="把这一周的菜都记成「喜欢」，以后多安排；不满意的单独点不喜欢"):
            prev_profile = prof.load_profile()
            n_new = 0
            for p in result.days:
                for d in p.dishes:
                    rec_r = db.by_id(d.recipe_id)
                    if rec_r is not None and rec_r.name not in liked_now:
                        prof.set_feedback(rec_r.name, "like", KNOWN_NAMES, source="本周计划")
                        n_new += 1
            text = f"已把这一周 {n_new} 道没表过态的菜都记成「喜欢」，以后会多安排（可撤销）。"
            ui.set_notice("like", text)
            ui.push_undo(text, profile=prev_profile)
            ui.push_history(text)
            st.rerun()
    with q3:
        st.toggle("分享视图", key="share_view", help="给家人看的干净版：只有日期、菜名、金额")
    if st.session_state.get("share_view"):
        st.code(rep.share_text(result, db, start_date), language=None)
        st.caption("这一屏可以直接截图发给家人；没有按钮、没有技术字样。")
    stat = [
        (f"¥{summary.total_cost:.0f}",
         f"本周花费（预算 ¥{summary.budget_total:.0f}）" if summary.budget_total
         else "本周花费（未设预算）",
         "bad" if summary.over_budget > 0 else ""),
        (f"{summary.liked_hit} 道", "命中你的收藏", "ok" if summary.liked_hit else ""),
        ("0 处" if not summary.hard_issues else f"{len(summary.hard_issues)} 处",
         "忌口 / 过敏冲突", "ok" if not summary.hard_issues else "bad"),
        (f"{summary.hardest_minutes} 分钟", f"最费时的第 {summary.hardest_day} 天",
         "warn" if summary.hardest_minutes >= 60 else ""),
    ]
    if c.goal != "随便":
        stat.append((f"{summary.goal_hit} 道", f"契合「{c.goal}」", ""))
    st.markdown("<div class='statrow'>" + "".join(
        f"<div class='stat {cls}'><div class='n'>{n}</div><div class='l'>{l}</div></div>"
        for n, l, cls in stat) + "</div>", unsafe_allow_html=True)
    st.markdown(f"<p class='line'>结构：{rep.structure_line(result, db)}；"
                "花费按菜谱 2 人份单价折算，实际以当地物价为准</p>", unsafe_allow_html=True)

    # ---- 忌口安全：只在需要时“喊”（4.6-6）
    if summary.allergen_issues:
        st.markdown("<p class='line bad'>菜单里出现了你设置的忌口，请先看这里：</p>",
                    unsafe_allow_html=True)
        for i in summary.allergen_issues:
            st.markdown(f"<p class='line bad'>· {i.message}</p>", unsafe_allow_html=True)
    elif c.allergens:
        st.markdown("<p class='line ok'>已避开你设置的忌口：" + "、".join(c.allergens)
                    + "　·　请以食品包装配料表为准，下厨前再确认一次。</p>",
                    unsafe_allow_html=True)
    if not result.final:
        for i in summary.hard_issues:
            st.markdown(f"<p class='line bad'>· {i.message}</p>", unsafe_allow_html=True)

    _notice_block(result)

    # ---- 整周总览：等宽天行，今天高亮、已过变淡（4.6-4 / E-01）
    st.markdown("### 整周总览")
    rows_html = []
    _multi = c.is_multi_meal()
    for row in summary.rows:
        idx = row.day - 1                      # 按**天**算高亮，不能用行号（多餐时行号会串天）
        cls = "today" if (today_idx is not None and idx == today_idx) else (
            "past" if (today_idx is not None and idx < today_idx) else "")
        mark = ("今晚" if c.active_meals()[-1] == row.meal else "今天") if cls == "today" else (
            "已过" if cls == "past" else "")
        when = f"第 {row.day} 天 {row.weekday} {row.date_label}"
        if _multi:
            when += f"　<b>{row.meal}</b>"
        rows_html.append(
            f"<div class='day-row {cls}'>"
            f"<div class='day-when'>{when}"
            + (f" · {mark}" if mark else "") + "</div>"
            f"<div class='day-dishes'>{'、'.join(row.dishes) or '（未排）'}</div>"
            f"<div class='day-meta'>{row.minutes} 分钟 · ¥{row.cost:.0f}</div></div>")
    st.markdown("<div class='day-wrap'>" + "".join(rows_html) + "</div>", unsafe_allow_html=True)

    # ---- 每日详情：默认折叠，点开看理由与逐道反馈（4.6-5 / E-04）
    with st.expander("每日详情与逐道反馈（换一道 / 喜欢 / 不喜欢）", expanded=False):
        st.caption("换一道 = 只换今晚这道（不动口味偏好）；喜欢 = 以后多安排；"
                   "不喜欢 = 换掉并记住，以后不再出现。")
        # 一天一个标签页，页内再按餐分段（docs/10）：多餐时标签数必须仍然是**天数**
        labels = [f"第 {i} 天" for i in range(1, days_span + 1)]
        _cur_tab = st.session_state.get("day_tabs")
        if _cur_tab is not None and _cur_tab not in labels:
            st.session_state["day_tabs"] = labels[0]
        day_tabs = st.tabs(labels, key="day_tabs", on_change="rerun")
        for tab, day_no in zip(day_tabs, range(1, days_span + 1)):
            with tab:
                idx0 = day_no - 1
                is_past = today_idx is not None and idx0 < today_idx
                for plan_day in result.slots_for(day_no):
                    if _multi:
                        st.markdown(f"<p class='line'><b>{plan_day.meal}</b></p>",
                                    unsafe_allow_html=True)
                    if plan_day.skipped:
                        st.markdown("<p class='line'>这天不做饭（你标记过：不计花费、也不进买菜清单）。</p>",
                                    unsafe_allow_html=True)
                        if not is_past and st.button("改回来做", key=f"tab_unskip_{plan_day.day}_{plan_day.meal}",
                                                     use_container_width=False):
                            result.days = restore_day(result.days, plan_day.day, db, c,
                                                      meal=plan_day.meal)
                            refresh_result(result, db)
                            _commit_plan()
                            ui.set_notice("swap", f"第 {plan_day.day} 天恢复做饭，其他天没动。")
                            st.rerun()
                        continue
                    for dish in plan_day.dishes:
                        r = db.by_id(dish.recipe_id)
                        if r is None:
                            continue
                        st.markdown(_dish_card(r, dish, r.name in liked_now, r.name in hated_now),
                                    unsafe_allow_html=True)
                        if not is_past:
                            picked = _dish_actions(plan_day.day, dish, r.name in liked_now,
                                                   r.name in hated_now,
                                                   dish.recipe_id in set(c.must_include_recipes))
                            pending = picked or pending
                    minutes = rep.day_minutes(plan_day, db)
                    cost = rep.day_cost(plan_day, db, c.people)
                    order, has_slow = rep.cook_order(plan_day, db)
                    st.markdown(
                        f"<p class='line'>这天合计约 {minutes} 分钟 · 预计 ¥{cost:.0f}"
                        + (f"　·　按 {plan_day.people} 人算" if plan_day.people else "")
                        + ("　·　有汤/炖菜可以先上火，实际用时更短" if has_slow else "")
                        + ("　·　（这天已经过去了，只能看不能改）" if is_past else "") + "</p>",
                        unsafe_allow_html=True)
                    if order:
                        with st.expander("下锅顺序"):
                            for line in order:
                                st.markdown(f"- {line}")

    if st.session_state.get("relax"):
        _relax_options(c, result.issues, swap_failed=True)

    # ---- 更多操作：回到上一版 / 重排 / 改动历史 / 开发者视角
    prev = store.previous_record(st.session_state.get("record_id"))
    with st.expander("以前的方案 / 改动历史"):
        o1, o2 = st.columns(2)
        with o1:
            if prev is not None and st.button(f"回到上一版（{prev.label}）", key="restore_prev_btn",
                                              use_container_width=True,
                                              help="切回上一版的菜单与需求，当前版仍在存档里"):
                _load_record(prev)
                ui.set_notice("info", f"已切回「{prev.label}」那一版菜单（当前版仍留着）。")
                ui.push_history(f"切回上一版：{prev.label}")
                st.rerun()
        with o2:
            if prev is not None and st.button("照上一版", key="reuse_prev_btn",
                                              use_container_width=True,
                                              help="用上一版的需求重新排一版（适合这周照上周的样子）"):
                _load_record(prev)
                st.session_state["stale"] = True
                st.session_state["job"] = None
                ui.set_notice("info", "已按上一版的需求重新排了一版，旧的几版都还在存档里。")
                st.rerun()
        st.markdown("**本机保存的方案**")
        for rec_item in store.load_records():
            mark = "（当前）" if rec_item.id == st.session_state.get("record_id") else ""
            r1, r2 = st.columns([4, 1])
            with r1:
                st.markdown(f"<p class='line'>{rec_item.label} · {rec_item.created_at} 生成{mark}</p>",
                            unsafe_allow_html=True)
            with r2:
                if rec_item.id != st.session_state.get("record_id"):
                    if guarded_button("切到这份", f"switch_{rec_item.id}",
                                      f"确认切到「{rec_item.label}」这一版吗？"
                                      "当前这版仍然保留在存档里，随时可以切回来。"):
                        _load_record(rec_item)
                        ui.set_notice("info", f"已切到「{rec_item.label}」。")
                        st.rerun()
                    if guarded_button("删除", f"del_{rec_item.id}",
                                      f"确认删除「{rec_item.label}」这一版吗？删除后无法找回"
                                      "（其它版本不受影响）。"):
                        store.delete_record(rec_item.id)
                        ui.set_notice("info", f"已删除「{rec_item.label}」这一版。")
                        ui.push_history(f"删除了方案「{rec_item.label}」")
                        st.rerun()
        if ui.history():
            st.markdown("**改动历史**")
            for h in ui.history():
                st.markdown(f"<p class='line'>· {h}</p>", unsafe_allow_html=True)

    if liked_now or hated_now:
        st.markdown(f"<p class='line'>口味档案：喜欢 {'、'.join(liked_now) or '—'}"
                    f"　｜　不喜欢 {'、'.join(hated_now) or '—'}</p>", unsafe_allow_html=True)

    # ---- 处理反馈：只动这一顿，不整周重排
    if pending:
        kind, day_no, rid = pending
        # 按钮的 key 里没有餐次（`like_1_r05`），但**一周内菜不重复**，
        # 所以用 rid 能唯一定位到"这一天里的哪一顿"（docs/10）。定位不到就按当天最后一顿。
        _slot = next((p for p in result.days
                      if p.day == day_no and any(d.recipe_id == rid for d in p.dishes)), None)
        _meal = _slot.meal if _slot is not None else None
        name = _name_of(rid)
        prev_days = [p.model_copy(deep=True) for p in result.days]
        prev_profile = prof.load_profile()
        undo_profile = None
        relax_failed = None
        text = ""
        if kind in ("lock", "unlock"):
            locked_now = list(c.must_include_recipes or [])
            if kind == "lock" and rid not in locked_now:
                locked_now.append(rid)
            elif kind == "unlock":
                locked_now = [x for x in locked_now if x != rid]
            c.must_include_recipes = locked_now
            _inp_lock = dict(st.session_state.get("plan_inputs") or {})
            _inp_lock["must_include"] = locked_now
            st.session_state["plan_inputs"] = _inp_lock
            st.session_state["pending_sync"] = _inp_lock
            ui.set_notice("info", f"已定住「{name}」，以后重排会保留它（其他菜不受影响）。"
                          if kind == "lock" else f"已取消定住「{name}」，重排时可以被换掉。")
            ui.push_history(ui.get_notice()["text"])
            st.session_state["stale"] = True
            st.session_state["job"] = None
            st.rerun()

        if kind == "swap":
            new_days, new_recipe = swap_dish(result.days, day_no, rid, db, c, _meal)
            if new_recipe:
                result.days = new_days
                refresh_result(result, db)
                text = f"已把第 {day_no} 天的「{name}」换成「{new_recipe.name}」（口味偏好未改动）"
            else:
                text = f"没有能替换「{name}」的菜了。下面点一下放宽条件，我马上重排一版。"
                relax_failed = {"day": day_no, "name": name}
        elif kind == "like":
            prof.set_feedback(name, "like", KNOWN_NAMES)
            undo_profile = prev_profile
            text = f"已记住你喜欢「{name}」，以后会优先安排（本次菜单不变）"
        else:
            prof.set_feedback(name, "dislike", KNOWN_NAMES)
            undo_profile = prev_profile
            new_days, new_recipe = swap_dish(result.days, day_no, rid, db, c, _meal)
            if new_recipe:
                result.days = new_days
                refresh_result(result, db)
                text = f"已记住不喜欢「{name}」，第 {day_no} 天换成「{new_recipe.name}」，以后不再出现"
            else:
                text = f"已记住不喜欢「{name}」（本次没有可替换的菜，其他天未改动）"
                relax_failed = {"day": day_no, "name": name}
        ui.set_notice(kind, text)
        ui.push_undo(text, days=prev_days, profile=undo_profile,
                     record_id=st.session_state.get("record_id"))
        ui.push_history(text)
        st.session_state["relax"] = relax_failed
        st.session_state["stale"] = False
        _commit_plan()
        st.rerun()


# ================================================================ 页面 3：买菜清单
def render_shopping() -> None:
    st.title("买菜清单")
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
    need = [s for s in result.shopping if s.needed]
    have = [s for s in result.shopping if not s.needed]
    epoch = st.session_state["check_epoch"]
    checked: list[str] = []
    _rec = store.get_record(st.session_state.get("record_id"))
    saved_checked = set(_rec.checked_items) if _rec is not None else set()

    st.markdown(f"<p class='line'>{label} · 已按 {c.people} 人份量折算（菜谱为 2 人份基准）"
                "　·　买一样勾一样，已买的会沉到分类末尾；勾选会记住，明天打开还在。"
                "</p>", unsafe_allow_html=True)

    def _is_checked(name: str) -> bool:
        key = f"chk_{epoch}_{name}"
        if key in st.session_state:
            return bool(st.session_state[key])
        return name in saved_checked      # 关掉浏览器再打开：上周勾的还在

    def shop_row(it) -> None:
        key = f"chk_{epoch}_{it.name}"
        st.session_state.setdefault(key, it.name in saved_checked)
        help_txt = f"用于：{'、'.join(it.for_recipes)}" if it.for_recipes else None
        if st.checkbox(f"{it.name}　{it.amount}", key=key, help=help_txt):
            checked.append(it.name)

    if not need:
        st.markdown("<p class='line'>这一周不需要采购（家里库存都覆盖了）。</p>",
                    unsafe_allow_html=True)
    else:
        cats = [cat for cat in DISPLAY_CATEGORIES if any(s.category == cat for s in need)]
        left, right = st.columns(2)     # 分类流式放入两列，有内容才占位（V-10）
        for i, cat in enumerate(cats):
            items = [s for s in need if s.category == cat]
            todo = [s for s in items if not _is_checked(s.name)]
            done = [s for s in items if _is_checked(s.name)]
            with (left if i % 2 == 0 else right):
                st.markdown(f"<div class='cat-bar'>{cat} · {len(items)} 样</div>",
                            unsafe_allow_html=True)
                for it in todo:
                    shop_row(it)
                if done:
                    with st.container(key=f"done_{epoch}_{cat}"):
                        st.markdown("<div style='opacity:.45'>", unsafe_allow_html=True)
                        for it in done:
                            shop_row(it)
                        st.markdown("</div>", unsafe_allow_html=True)

    if need and set(checked) != saved_checked and st.session_state.get("record_id"):
        # 勾选写回存档：一周边买边勾，关了浏览器再打开还是这个样子（05 M4）
        store.set_checked(st.session_state["record_id"], checked)

    if need:
        st.progress(len(checked) / len(need))
        _rows_now = rep.shopping_rows(result, set(checked))
        _b1, _b2 = rep.split_batches(_rows_now)
        st.toggle("分两次买（周初买耐放的，周中再买青菜鲜肉）", key="two_batches")
        if st.session_state.get("two_batches"):
            with st.container(key="batch_view"):
                st.markdown("<div class='cat-bar'>第一次买 · 耐放的（"
                            f"{len(_b1)} 样）</div>", unsafe_allow_html=True)
                for _row in _b1:
                    st.markdown(f"<p class='line'>{_row['食材']}　{_row['数量']}"
                                + ("　可选" if rep.optional_hint(_row) else "") + "</p>",
                                unsafe_allow_html=True)
                st.markdown("<div class='cat-bar'>第二次买 · 周中更新鲜（"
                            f"{len(_b2)} 样）</div>", unsafe_allow_html=True)
                for _row in _b2:
                    st.markdown(f"<p class='line'>{_row['食材']}　{_row['数量']}"
                                + ("　可选" if rep.optional_hint(_row) else "") + "</p>",
                                unsafe_allow_html=True)
            st.caption("青菜、菌菇、肉和水产放到周中再买更新鲜；只在一道菜里用到的小料标了「可选」，可以先不买。")
        if len(checked) >= len(need):
            st.markdown("<p class='line ok'>清单已全部买齐，可以开始做饭了。</p>",
                        unsafe_allow_html=True)
        else:
            st.markdown(f"<p class='line'>已买 {len(checked)} / {len(need)} 样，"
                        f"还剩 {len(need) - len(checked)} 样</p>", unsafe_allow_html=True)
        b1, b2 = st.columns([1, 2])
        with b1:
            if st.button("清除勾选", key="clear_checks", use_container_width=True):
                if st.session_state.get("record_id"):
                    store.set_checked(st.session_state["record_id"], [])
                st.session_state["check_epoch"] = epoch + 1
                st.rerun()
        with b2:
            if st.button("带走清单", key="export_toggle", type="primary",
                         use_container_width=True):
                st.session_state["export_open"] = not st.session_state.get("export_open", False)
                st.rerun()

    if st.session_state.get("export_open"):
        with st.container(key="export_area"):
            st.markdown("### 带走这份清单")
            e1, e2 = st.columns([1, 1])
            with e1:
                st.download_button(
                    "下载 CSV", data=rep.shopping_csv(result, set(checked)),
                    file_name=f"买菜清单_{label}.csv", mime="text/csv",
                    use_container_width=True, key="csv_dl",
                    help="Excel / 手机表格都能打开（带已买标记，UTF-8 BOM 不乱码）",
                )
            with e2:
                st.download_button(
                    "下载菜单文本", data=rep.printable_text(result, db, start_date, set(checked)),
                    file_name=f"本周菜单_{label}.txt", mime="text/plain",
                    use_container_width=True, key="txt_dl",
                    help="纯文本，直接发微信 / 存备忘录",
                )
            with st.expander("复制文本（发微信 / 备忘录）", expanded=False):
                st.caption("点右上角图标即可复制")
                st.code(rep.shopping_text(result, set(checked), label), language=None)
            st.toggle("打印预览（A4 一页：上半周菜单、下两栏清单）", key="print_preview")
            if st.session_state.get("print_preview"):
                with st.container(key="print_area"):
                    st.markdown(rep.printable_html(result, db, start_date, set(checked)),
                                unsafe_allow_html=True)
                st.caption("按 Ctrl+P 打印（或另存 PDF）——打印时会自动隐藏页面上的其他内容。")

    if have:
        st.markdown("<p class='line'>家里已有、无需购买：" + "、".join(s.name for s in have)
                    + "</p>", unsafe_allow_html=True)


# ================================================================ 页面 4：口味档案
def guarded_button(label: str, key: str, prompt: str, help_: str = "") -> bool:
    """破坏性操作二次确认：第一次点击只是「举起来」，确认后才真的执行。"""
    if ui.armed() == key:
        st.warning(prompt)
        y, n = st.columns(2)
        with y:
            if st.button("确认执行", key=f"yes_{key}", type="primary", use_container_width=True):
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
    st.title("口味档案")
    st.caption("喜欢和不喜欢的菜各有一个独立列表，随时能加、能移、能删。"
               "改动后菜单会按新口味重排；清空这类操作需要再确认一次。"
               "**换一道 = 只换这次，不影响档案；不喜欢 = 以后都不再出现。**")

    liked = prof.liked_names(KNOWN_NAMES)
    disliked = prof.disliked_names(KNOWN_NAMES)
    unrated = [r for r in db.recipes if r.name not in liked and r.name not in disliked]

    st.markdown(
        "<div class='statrow'>"
        f"<div class='stat'><div class='n'>{len(liked)}</div><div class='l'>喜欢</div></div>"
        f"<div class='stat'><div class='n'>{len(disliked)}</div><div class='l'>不喜欢</div></div>"
        f"<div class='stat'><div class='n'>{len(unrated)}</div><div class='l'>未表态</div></div>"
        f"<div class='stat'><div class='n'>{len(db.recipes)}</div><div class='l'>菜谱库</div></div>"
        "</div>", unsafe_allow_html=True)

    notice = ui.get_notice()
    if notice:
        n1, n2 = st.columns([5, 1])
        with n1:
            st.markdown(f"<p class='line'>{notice['text']}</p>", unsafe_allow_html=True)
        with n2:
            if ui.undo_depth() and st.button(f"撤销({ui.undo_depth()})", key="profile_undo_btn",
                                             use_container_width=True,
                                             help="可以连续点，最多回退 5 步"):
                entry = ui.pop_undo()
                if entry and entry.get("profile") is not None:
                    prof.save_profile(entry["profile"])
                    st.session_state["stale"] = True
                    ui.set_notice("undo", f"已撤销：{entry['text']}")
                    ui.push_history(f"撤销了「{entry['text']}」")
                st.rerun()
    if ui.history():
        with st.expander("改动历史"):
            for h in ui.history():
                st.markdown(f"<p class='line'>· {h}</p>", unsafe_allow_html=True)

    def chips_html(r) -> str:
        if r is None:
            return ""
        return (f"<span class='chip'>{r.category}</span><span class='chip'>{r.spice_level}</span>"
                f"<span class='chip'>{r.time_min} 分钟</span>"
                f"<span class='chip'>¥{r.cost_yuan}/2人份</span>")

    def recipe_of(name: str):
        rid = NAME2ID.get(name)
        return db.by_id(rid) if rid else None

    def record_change(text: str, prev_profile: dict) -> None:
        ui.push_undo(text, profile=prev_profile)
        ui.push_history(text)
        ui.set_notice("info", text)
        st.session_state["stale"] = True

    tab_lists, tab_browse = st.tabs(["我的喜好列表", "全部菜品挑选"])

    with tab_lists:
        action = None
        col_l, col_h = st.columns(2)

        with col_l:
            st.markdown(f"#### 喜欢的菜（{len(liked)}）")
            st.caption("排菜时优先安排，并尽量分散到不同天")
            if not liked:
                st.markdown("<p class='line'>列表为空。可以从右侧「移到喜欢」，"
                            "或用下方「快速添加」。</p>", unsafe_allow_html=True)
            for name in liked:
                r = recipe_of(name)
                rid = NAME2ID.get(name, name)
                row = st.columns([3, 1, 1])
                with row[0]:
                    _origin = prof.feedback_origin(name)
                    _origin_txt = (f"<span class='line'>　{_origin.get('since', '')} 在"
                                   f"{_origin.get('source', '')}点的</span>") if _origin else ""
                    st.markdown(f"**{name}**　{chips_html(r)}{_origin_txt}", unsafe_allow_html=True)
                with row[1]:
                    if st.button("移到不喜欢", key=f"mv2hate_{rid}", help="移到「不喜欢」列表",
                                 use_container_width=True):
                        action = (name, "dislike")
                with row[2]:
                    if st.button("移出列表", key=f"rm_from_like_{rid}", help="恢复未表态",
                                 use_container_width=True):
                        action = (name, "remove")
            if liked and guarded_button("清空喜欢列表", "clr_like_btn",
                                        f"确认要清空「喜欢」里的 {len(liked)} 道菜吗？"
                                        "清空后可以点上面的「撤销」找回。"):
                action = ("", "clear_like")

        with col_h:
            st.markdown(f"#### 不喜欢的菜（{len(disliked)}）")
            st.caption("这些菜绝不会出现在菜单里")
            if not disliked:
                st.markdown("<p class='line'>列表为空。菜单里点「不喜欢」的菜会自动进到这里。</p>",
                            unsafe_allow_html=True)
            for name in disliked:
                r = recipe_of(name)
                rid = NAME2ID.get(name, name)
                row = st.columns([3, 1, 1])
                with row[0]:
                    _origin = prof.feedback_origin(name)
                    _origin_txt = (f"<span class='line'>　{_origin.get('since', '')} 在"
                                   f"{_origin.get('source', '')}点的</span>") if _origin else ""
                    st.markdown(f"**{name}**　{chips_html(r)}{_origin_txt}", unsafe_allow_html=True)
                with row[1]:
                    if st.button("移到喜欢", key=f"mv2like_{rid}", help="移到「喜欢」列表",
                                 use_container_width=True):
                        action = (name, "like")
                with row[2]:
                    if st.button("移出列表", key=f"rm_from_hate_{rid}", help="恢复未表态",
                                 use_container_width=True):
                        action = (name, "remove")
            if disliked and guarded_button("清空不喜欢列表", "clr_hate_btn",
                                           f"确认要清空「不喜欢」里的 {len(disliked)} 道菜吗？"
                                           "清空后它们会重新出现在菜单里（可撤销）。"):
                action = ("", "clear_dislike")

        if action:
            name, act = action
            prev_profile = prof.load_profile()
            prof.set_feedback(name, act, KNOWN_NAMES)
            if act == "clear_like":
                text = "已清空「喜欢」列表（可撤销）"
            elif act == "clear_dislike":
                text = "已清空「不喜欢」列表（可撤销）"
            elif act == "remove":
                text = f"已把「{name}」恢复为未表态"
            elif act == "like":
                text = f"已把「{name}」移入「喜欢」"
            else:
                text = f"已把「{name}」移入「不喜欢」"
            record_change(text, prev_profile)
            st.rerun()

        st.markdown(f"**快速添加**（从 {len(unrated)} 道未表态的菜里多选）")
        q1, q2, q3 = st.columns([3, 1, 1])
        with q1:
            picks = st.multiselect("选择菜品", [r.name for r in unrated], key="pf_quick",
                                   placeholder="输入菜名搜索，可多选", label_visibility="collapsed")
        with q2:
            add_like = st.button("加入喜欢", use_container_width=True, disabled=not picks)
        with q3:
            add_hate = st.button("加入不喜欢", use_container_width=True, disabled=not picks)
        if picks and (add_like or add_hate):
            act = "like" if add_like else "dislike"
            prev_profile = prof.load_profile()
            prof.bulk_feedback(picks, act, KNOWN_NAMES)
            record_change(f"批量加入「{'喜欢' if add_like else '不喜欢'}」：{'、'.join(picks)}",
                          prev_profile)
            st.rerun()

        st.divider()
        c1, c2, _sp = st.columns([1, 1, 2])
        with c1:
            st.download_button(
                "导出档案",
                data=json.dumps(prof.load_profile(), ensure_ascii=False, indent=2),
                file_name="我的口味档案.json", mime="application/json",
                use_container_width=True, key="profile_dl",
                help="把喜欢 / 不喜欢的列表导出成文件（换设备时用得上）",
            )
        with c2:
            if guarded_button("清空全部档案", "clear_all_btn",
                              f"确认要清空全部档案吗？「喜欢」{len(liked)} 道、「不喜欢」"
                              f"{len(disliked)} 道都会一起清掉（清空后可以点「撤销」找回）。"):
                prev_profile = prof.load_profile()
                prof.clear_all()
                record_change("已清空全部档案（可撤销）", prev_profile)
                st.rerun()
        st.caption("隐私：档案只保存在这台机器上，不会上传、也不需要账号；"
                   "误清空可以点上面的「撤销」找回。")

    with tab_browse:
        st.markdown("**按分类浏览，直接标记喜欢 / 不喜欢**")
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            kw = st.text_input("搜索菜名 / 主料", placeholder="例如：鸡、豆腐、西兰花",
                               key="pf_search")
        with col_f2:
            cats = ["全部"] + sorted({r.category for r in db.recipes})
            cat = st.selectbox("分类筛选", cats, key="pf_cat")

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
                state = "已喜欢" if is_loved else ("已排除" if is_hated else "")
                st.markdown(
                    f"<div class='dish-card{' loved' if is_loved else (' hated' if is_hated else '')}'>"
                    f"<div class='dish-row'><div class='dish-name'>{r.name}</div>"
                    + (f"<span class='state {'loved' if is_loved else 'hated'}'>{state}</span>"
                       if state else "")
                    + f"</div><div class='dish-meta'>{chips_html(r)}</div>"
                    f"<div class='dish-reason'>主料：{'、'.join(i.name for i in r.ingredients[:5])}"
                    "</div></div>", unsafe_allow_html=True)
            with row[1]:
                if st.button("已喜欢" if is_loved else "喜欢", key=f"pf_like_{r.id}",
                             use_container_width=True):
                    pf_feedback = (r.name, "like")
            with row[2]:
                if st.button("已排除" if is_hated else "不喜欢", key=f"pf_hate_{r.id}",
                             use_container_width=True):
                    pf_feedback = (r.name, "dislike")

        if pf_feedback:
            name, action = pf_feedback
            prev_profile = prof.load_profile()
            prof.set_feedback(name, action, KNOWN_NAMES)
            record_change(f"{'喜欢' if action == 'like' else '不喜欢'}：{name}", prev_profile)
            st.rerun()


# ================================================================ 路由（任务栏）
_PAGE_RENDERERS = {
    "tonight": render_tonight,
    "create": render_create,
    "plan": render_plan,
    "shopping": render_shopping,
    "profile": render_profile,
}
try:
    _PAGE_RENDERERS.get(st.session_state["page"], render_tonight)()
except api.ClientError as exc:
    # 服务端中途出问题（进程被杀、重启、超时）：给一页人话 + 可以点的下一步，
    # 而不是把英文堆栈直接摔在客户脸上（05 §5.1）。开头的连接守卫只管"一开始就连不上"，
    # 这里管"用着用着断了"。
    st.error(exc.message)
    if exc.next_steps:
        st.markdown("可以试试：" + "、".join(str(s.get("label", "")) for s in exc.next_steps
                                             if s.get("label")))
    if st.button("重试", key="api_retry_page", type="primary"):
        st.rerun()
