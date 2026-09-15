"""「吃过」与轮换（docs/12 §4 阶段二 2.6）：档案页「最近的吃法」+ 菜单上那句"好久没吃"。

这一组守四件事：

1. **只有"吃过"算吃过**：点「做完了」或打分算，「被排进菜单」（`select`）**不算** ——
   拿算法自己排的菜当"你常吃"的证据，是自我实现（越排越像爱吃，其实是还没下锅）；
2. **轮换只破平手**：上限必须小于 2 分（用户明确表态的最小差距是 好吃 +10 / 喜欢 +8），
   "好久没吃"永远不能盖过用户自己说过的话；
3. **零回归**：没有吃过记录（`dish_last_seen` 为空）时 `recipe_score` 与老行为一字不差 ——
   这是既有几百条测试不回归的前提，和 `dish_weights` 是同一条纪律；
4. **两种后端都要记**：「做完了」不改菜单，推不出差异事件，只能在 `set_done` 里显式记一条 ——
   所以 JSON 与数据库两条路都要验（docs/11 R5：两个后端分叉过不止一次）。
"""
from __future__ import annotations

import importlib.util
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from jsonmod import bind_json_events
from recipe_planner import events as ev
from recipe_planner import preference as pf
from recipe_planner.core import recipe_score, retrieve_candidates
from recipe_planner.models import (ChosenDish, DayPlan, PlanResult, Recipe, RecipeDB,
                                   UserConstraints)

TODAY = date(2026, 9, 15)
ROOT = Path(__file__).resolve().parent.parent


def _e(recipe_id: str, action: str, days_ago: int, meal: str = "晚餐") -> dict:
    """造一条事件（`days_ago` 天前的晚上）。"""
    when = datetime.combine(TODAY - timedelta(days=days_ago), time(20, 0))
    return {"recipe_id": recipe_id, "action": action, "meal": meal, "day_no": 1,
            "created_at": when.isoformat(timespec="seconds")}


def _db() -> RecipeDB:
    return RecipeDB(recipes=[
        Recipe(id="a", name="甲菜", category="热菜", time_min=20, cost_yuan=10,
               taste_tags=["咸鲜"]),
        Recipe(id="b", name="乙菜", category="热菜", time_min=20, cost_yuan=10,
               taste_tags=["咸鲜"]),
    ])


# ---------------------------------------------------------------- 什么算"吃过"

def test_排进菜单不算吃过():
    """`select` 只说明"排了"，`swap_out`/`like` 更不是"吃了" —— 都不进"吃过"。"""
    events = [_e("a", ev.SELECT, 1), _e("a", ev.SWAP_OUT, 2), _e("a", ev.LIKE, 3)]
    assert pf.eaten_events(events) == []
    assert pf.last_eaten(events, today=TODAY) == {}


def test_做完了与打分都算吃过():
    events = [_e("a", ev.DONE, 10), _e("b", ev.RATE_GOOD, 4), _e("a", ev.RATE_OK, 3)]
    assert pf.eaten_events(events) == [events[0], events[1], events[2]]
    assert pf.last_eaten(events, today=TODAY) == {"a": 3, "b": 4}


def test_下次不做也算吃过():
    """「下次不做」是**做完之后**打的（说明这顿做了、只是不想再做）——
    不认它会让"这道我做过、但不想再做"的菜凭空从来没吃过。"""
    assert pf.last_eaten([_e("a", ev.RATE_NEVER, 6)], today=TODAY) == {"a": 6}


def test_上次吃不看时间窗():
    """权重只看近 90 天，但"上次吃"恰恰要看很久以前 —— 被窗口截掉，
    「好久没吃」就只在有近期记录时才存在（那是最该提醒的时候反而没有）。"""
    assert pf.last_eaten([_e("a", ev.DONE, 400)], today=TODAY) == {"a": 400}


def test_坏时间戳不会把整批记录弄丢():
    events = [{"recipe_id": "a", "action": ev.DONE, "created_at": "不是时间"},
              _e("a", ev.DONE, 8), _e("b", ev.DONE, 9)]
    assert pf.last_eaten(events, today=TODAY) == {"a": 8, "b": 9}


# ---------------------------------------------------------------- 轮换：只破平手

def test_轮换加分阶梯():
    assert pf.rotation_bonus(None) == 0.0
    assert pf.rotation_bonus(3) == 0.0            # 刚吃过，别连着排
    assert pf.rotation_bonus(pf.FRESH_DAYS) == pf.ROTATION_MAX / 2
    assert pf.rotation_bonus(pf.STALE_DAYS) == pf.ROTATION_MAX


def test_轮换永远翻不过用户明确表态():
    """**这条是设计约束，不是巧合**：好吃 +10 必须仍然压得住"喜欢 +8 + 好久没吃"。"""
    assert pf.ROTATION_MAX < 2.0
    c = UserConstraints(people=2, days=3, dish_weights={"b": 8.0},
                        dish_last_seen={"a": 400})
    assert recipe_score(_db().recipes[0], c) == pf.ROTATION_MAX
    assert recipe_score(_db().recipes[1], c) == 8.0


def test_没有吃过记录时score一字不变():
    c = UserConstraints(people=2, days=3, liked_dishes=["a"])
    assert recipe_score(_db().recipes[0], c) == 8.0
    assert recipe_score(_db().recipes[1], c) == 0.0


def test_没吃过的菜不会被当成好久没吃():
    """只有出现过在"吃过表"里的菜才轮到 —— 新菜不是"该吃了"，它是"还没试过"。"""
    c = UserConstraints(people=2, days=3, dish_last_seen={"b": 40})
    got = [r.id for r in retrieve_candidates(_db(), c, "晚餐")]
    assert got == ["b", "a"], got


def test_轮换能让好久没吃的菜往前站():
    c = UserConstraints(people=2, days=3, max_time_min=40, dish_last_seen={"b": 45})
    assert [r.id for r in retrieve_candidates(_db(), c, "晚餐")] == ["b", "a"]


# ---------------------------------------------------------------- 档案页三堆

def _hist(n: int, last: int, recent: int) -> dict:
    """一条吃过记录（`eating_history` 的形状）。三堆只看 n / last / recent。"""
    return {"n": n, "last_days": last, "recent": recent, "stability": {}}


def test_三堆归类():
    history = {
        "常吃": _hist(3, 3, 2),
        "好久没吃": _hist(5, 45, 0),
        "新面孔": _hist(1, 5, 1),
        "两周一次的": _hist(4, 14, 1),      # 稳定但不算"好久"，也不算"刚开始"
    }
    got = pf.buckets(history)
    assert got["recent"] == ["常吃"]
    assert got["new"] == ["新面孔"]
    assert got["stale"] == ["好久没吃"]
    assert "两周一次的" not in got["recent"] + got["new"] + got["stale"]


def test_不喜欢的菜不进好久没吃():
    """用户明确说过不要的菜，再提醒"你好久没吃它了"就是跟用户顶嘴。"""
    history = {"不爱的": _hist(3, 90, 0), "爱的": _hist(3, 90, 0)}
    got = pf.buckets(history, exclude={"不爱的"})
    assert got["stale"] == ["爱的"]


def test_常吃的排前面按次数():
    history = {"少": _hist(2, 1, 2), "多": _hist(3, 2, 3)}
    assert pf.buckets(history)["recent"] == ["多", "少"]


def test_吃够三次才给习惯偶尔():
    """样本 <3 次不硬下结论（`preference` 模块开头 ① 的样本量约束）。"""
    regular = pf.eating_history(
        [_e("a", ev.DONE, d) for d in (0, 7, 14, 21)], today=TODAY)["a"]
    assert regular["n"] == 4 and regular["stability"]["label"] == "习惯"
    assert regular["stability"]["interval_days"] == 7.0

    rare = pf.eating_history([_e("b", ev.DONE, 3)], today=TODAY)["b"]
    assert rare["stability"]["label"] == "刚开始" and rare["stability"]["cv"] is None


def test_计划信号一次读全量且两个都在():
    events = [_e("a", ev.DONE, 200), _e("b", ev.LIKE, 2)]
    got = pf.planning_signals(events=events, profile={"liked_dishes": ["甲菜"]},
                              by_name={"甲菜": "a"}, today=TODAY)
    assert got["dish_last_seen"] == {"a": 200}          # 200 天前的"吃过"仍然在
    # 权重只认近 90 天：200 天前的 done 本来是 0 分，b 的"喜欢"是 8 分
    assert got["dish_weights"] == {"b": 8.0}


# ---------------------------------------------------------------- 两种后端：标记做完要落事件

def _fresh(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "recipe_planner" / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_标记做完写死一条done事件而不是靠菜单差异():
    """`done_events` 是纯函数：一顿里每道菜一条。"""
    slot = DayPlan(day=2, meal="晚餐",
                   dishes=[ChosenDish(recipe_id="a"), ChosenDish(recipe_id="b")])
    got = ev.done_events(slot, day=2, source="做完了")
    assert [(e["recipe_id"], e["action"], e["meal"], e["day_no"]) for e in got] == [
        ("a", ev.DONE, "晚餐", 2), ("b", ev.DONE, "晚餐", 2)]
    assert ev.done_events(None) == []


def test_json后端标记做完会写事件(monkeypatch):
    """JSON 后端走真流程：存方案 → 标记做完 → 事件文件里出现 done。"""
    tmp = ROOT / ".tmp" / f"done_{uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE", "json")
    monkeypatch.setenv("RECIPE_PLAN_FILE", str(tmp / "plans.json"))
    monkeypatch.setenv("RECIPE_EVENTS_FILE", str(tmp / "events.json"))
    monkeypatch.setenv("RECIPE_PROFILE_FILE", str(tmp / "profile.json"))
    # `store.py` 里 `from recipe_planner import events` —— 必须把**包属性**也换成 JSON 版：
    # 只改 `sys.modules` 没用（`recipe_planner.events` 早就导过了，包上挂着的是数据库版），
    # 那样 fresh-load 出来的 store 会拿数据库版去写**真库**（`tests/jsonmod.py` 讲清了原因）。
    json_ev = bind_json_events(monkeypatch)
    store = _fresh("store.py", "store_json")
    assert store.events is json_ev, "store 必须用 JSON 版 events，否则会写到真库上"

    result = PlanResult(constraints=UserConstraints(people=2, days=1, meals=["晚餐"]),
                        candidate_count=2, final=True,
                        days=[DayPlan(day=1, meal="晚餐",
                                      dishes=[ChosenDish(recipe_id="r01")])])
    rec = store.save_plan(result, "2026-09-14")
    assert store.set_done(rec.id, 1, True, "晚餐") is not None
    rows = store.events.load_events()
    assert [(r["recipe_id"], r["action"]) for r in rows] == [("r01", ev.DONE)], rows

    # 取消标记**不写反向事件**：饭已经吃过了，取消只是"标错了"
    store.set_done(rec.id, 1, False, "晚餐")
    assert [r["action"] for r in store.events.load_events()] == [ev.DONE]
    assert (tmp / "events.json").exists()


def test_数据库后端标记做完会写事件():
    """数据库后端走真流程（`PlanRepo.set_done` → `dish_event` 表）。"""
    import asyncio

    from recipe_planner.storage import db_events, engine, migrate, sync_bridge
    from recipe_planner.storage.repositories import PlanRepo, RecipeRepo

    tmp = ROOT / ".tmp" / "pytest"
    tmp.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{(tmp / f'done_{uuid4().hex[:8]}.db').as_posix()}"
    migrate.ensure_schema(url)
    old_url, old_storage = os.environ.get("DATABASE_URL"), os.environ.get("STORAGE")
    os.environ["DATABASE_URL"], os.environ["STORAGE"] = url, "db"
    engine.reset_engine()
    try:
        sync_bridge.run(RecipeRepo.upsert_many(_db().recipes))
        result = PlanResult(constraints=UserConstraints(people=2, days=1, meals=["晚餐"]),
                            candidate_count=2, final=True,
                            days=[DayPlan(day=1, meal="晚餐",
                                          dishes=[ChosenDish(recipe_id="a"),
                                                  ChosenDish(recipe_id="b")])])
        rec = sync_bridge.run(PlanRepo.save_plan(result, "2026-09-14"))
        assert db_events.load_events() == []            # 排菜本身不产生"吃过"
        assert sync_bridge.run(PlanRepo.set_done(rec.id, 1, True)) is not None
        rows = db_events.load_events()
        assert sorted((e["recipe_id"], e["action"], e["meal"]) for e in rows) == [
            ("a", ev.DONE, "晚餐"), ("b", ev.DONE, "晚餐")], rows
        # 与 JSON 后端同一条语义：取消标记不留反向事件
        sync_bridge.run(PlanRepo.set_done(rec.id, 1, False))
        assert [e["action"] for e in db_events.load_events()] == [ev.DONE, ev.DONE]
    finally:
        engine.reset_engine()
        if old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_url
        if old_storage is None:
            os.environ.pop("STORAGE", None)
        else:
            os.environ["STORAGE"] = old_storage
        assert asyncio.iscoroutinefunction(db_events._record)
