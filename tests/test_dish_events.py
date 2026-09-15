"""偏好事件 + 时间衰减权重（docs/12 §4 阶段二）。

这一组守两件事：

1. **事件从"状态差异"推导**（不是在每个按钮里手写）：菜单类事件只有一个入口
   （`store.update_result` / `PlanRepo.update_result`），档案类事件只有一个入口
   （`save_profile`）—— 所以这里先把两个纯函数（`menu_events` / `profile_events`）
   逐个动作验清楚，再接后端。
2. **权重的两条验收**（docs/12 §4 阶段二）：
   · "同一条『喜欢』，7 天前与 90 天前的影响力**明显不同**"；
   · "换掉的菜短期内**不再立刻回来**"。
   另外守住"没有事件时 `recipe_score` 一个字都不变"（`dish_weights` 为空 → 老行为），
   这条是既有 350 条测试不回归的前提。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from recipe_planner import events as ev
from recipe_planner import preference as pf
from recipe_planner.core import recipe_score, retrieve_candidates
from recipe_planner.models import (ChosenDish, DayPlan, PlanResult, Recipe, RecipeDB,
                                   UserConstraints)

TODAY = date(2026, 9, 15)


def _result(spec: dict[tuple[int, str], list[str]], locked: list[str] | None = None,
            skipped: list[tuple[int, str]] = ()) -> PlanResult:
    c = UserConstraints(people=2, days=3, meals=["早餐", "晚餐"], must_include_recipes=locked or [])
    days = []
    for (day, meal), ids in sorted(spec.items()):
        days.append(DayPlan(day=day, meal=meal, skipped=(day, meal) in skipped,
                            dishes=[ChosenDish(recipe_id=i) for i in ids]))
    return PlanResult(constraints=c, candidate_count=9, days=days, final=True)


# ---------------------------------------------------------------- 纯函数：菜单事件

def test_换一道推出换掉与换进来两条事件():
    before = _result({(1, "晚餐"): ["r01", "r02"], (2, "晚餐"): ["r03"]})
    after = _result({(1, "晚餐"): ["r01", "r05"], (2, "晚餐"): ["r03"]})
    got = ev.menu_events(before, after)
    assert sorted((e["recipe_id"], e["action"]) for e in got) == [
        ("r02", ev.SWAP_OUT), ("r05", ev.SELECT)]
    assert all(e["meal"] == "晚餐" and e["day_no"] == 1 for e in got)


def test_没变的菜不产生事件():
    """否则每次改动都会刷一堆噪声，权重会被"被排进菜单"这件事自己喂大。"""
    same = _result({(1, "晚餐"): ["r01", "r02"], (2, "晚餐"): ["r03"]})
    assert ev.menu_events(same, same) == []


def test_某顿不做饭记成skip而不是换掉():
    """语义不同：「换掉」是"这道不行"，「不做饭」是"这顿不吃了"。"""
    before = _result({(1, "晚餐"): ["r01", "r02"]})
    after = _result({(1, "晚餐"): []}, skipped=[(1, "晚餐")])
    got = ev.menu_events(before, after)
    assert sorted(e["action"] for e in got) == [ev.SKIP, ev.SKIP]


def test_定住与取消定住也是事件():
    """「定住」不在菜里，在 `must_include_recipes` 里 —— 很容易漏掉。"""
    base = {(1, "晚餐"): ["r01"]}
    got = ev.menu_events(_result(base), _result(base, locked=["r09"]))
    assert [(e["recipe_id"], e["action"]) for e in got] == [("r09", ev.LOCK)]
    back = ev.menu_events(_result(base, locked=["r09"]), _result(base))
    assert [(e["recipe_id"], e["action"]) for e in back] == [("r09", ev.UNLOCK)]


# ---------------------------------------------------------------- 纯函数：档案事件

BY_NAME = {"番茄炒蛋": "r01", "清炒时蔬": "r02", "红烧排骨": "r03"}


def test_档案差异推出喜欢与不喜欢():
    before = {"liked_dishes": ["番茄炒蛋"], "disliked_dishes": []}
    after = {"liked_dishes": ["番茄炒蛋", "清炒时蔬"], "disliked_dishes": ["红烧排骨"]}
    got = ev.profile_events(before, after, by_name=BY_NAME)
    assert sorted((e["recipe_id"], e["action"]) for e in got) == [
        ("r02", ev.LIKE), ("r03", ev.DISLIKE)]


def test_取消喜欢与从不喜欢里挪回来():
    got = ev.profile_events({"liked_dishes": ["番茄炒蛋"], "disliked_dishes": ["红烧排骨"]},
                            {"liked_dishes": [], "disliked_dishes": []}, by_name=BY_NAME)
    assert sorted((e["recipe_id"], e["action"]) for e in got) == [
        ("r01", ev.UNLIKE), ("r03", ev.UNDISLIKE)]


def test_打分只在分数变化时记一次():
    before = {"ratings": {"番茄炒蛋": {"score": 1, "date": "09/10"}}}
    after = {"ratings": {"番茄炒蛋": {"score": 2, "date": "09/15"},
                         "清炒时蔬": {"score": 0, "date": "09/15"}}}
    got = ev.profile_events(before, after, by_name=BY_NAME)
    assert sorted((e["recipe_id"], e["action"]) for e in got) == [
        ("r01", ev.RATE_GOOD), ("r02", ev.RATE_NEVER)]
    # 分数没变就没有事件
    assert ev.profile_events(after, after, by_name=BY_NAME) == []


def test_翻不出id的菜被跳过而不是写脏数据():
    got = ev.profile_events({}, {"liked_dishes": ["库里的假菜"]}, by_name=BY_NAME)
    assert got == []


# ---------------------------------------------------------------- 衰减与权重

def test_衰减的三个台阶():
    assert [pf.decay(d) for d in (0, 7, 8, 30, 31, 90, 91)] == [1.0, 1.0, 0.5, 0.5, 0.2, 0.2, 0.0]


def _ev(rid: str, action: str, days_ago: int) -> dict:
    when = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time())
    return {"recipe_id": rid, "action": action, "created_at": when.isoformat()}


def test_同一条喜欢7天前与90天前的影响力明显不同():
    """**docs/12 §4 阶段二的第一条验收。**"""
    fresh = pf.weights_from_events([_ev("r01", ev.LIKE, 1)], today=TODAY)
    old = pf.weights_from_events([_ev("r01", ev.LIKE, 90)], today=TODAY)
    assert fresh["r01"] == pytest.approx(8.0)          # 刚发生的"喜欢" ≈ 老的静态 +8
    assert old["r01"] == pytest.approx(1.6)            # 90 天前只剩 1/5
    assert fresh["r01"] > old["r01"] * 4


def test_超过90天的事件不再影响权重():
    assert pf.weights_from_events([_ev("r01", ev.LIKE, 120)], today=TODAY) == {}


def test_换掉的菜短期内是负分():
    """**docs/12 §4 阶段二的第二条验收**：换掉过 → 短时间内别再排到它。"""
    got = pf.weights_from_events([_ev("r02", ev.SWAP_OUT, 2)], today=TODAY)
    assert got["r02"] < 0


def test_定住比喜欢更重():
    lock = pf.weights_from_events([_ev("r09", ev.LOCK, 1)], today=TODAY)["r09"]
    like = pf.weights_from_events([_ev("r09", ev.LIKE, 1)], today=TODAY)["r09"]
    assert lock > like


def test_权重会被夹在上下限里():
    events = [_ev("r01", ev.DISLIKE, 1) for _ in range(10)]
    assert pf.weights_from_events(events, today=TODAY)["r01"] >= -8.0


def test_老档案里没有事件的菜会被补一份():
    """新功能上线那一刻，用户此前的喜好在权重里不能等于不存在。"""
    profile = {"liked_dishes": ["番茄炒蛋"], "history": {"番茄炒蛋": {"since": "09/15"}}}
    got = pf.weights_from_profile(profile, BY_NAME, covered=set(), today=TODAY)
    assert got["r01"] == pytest.approx(8.0)


def test_已经有事件的菜不再吃老档案那份():
    """否则同一份喜欢会被算两遍（事件 + 档案补位）。"""
    profile = {"liked_dishes": ["番茄炒蛋"], "history": {"番茄炒蛋": {"since": "09/15"}}}
    got = pf.weights_from_profile(profile, BY_NAME, covered={"r01"}, today=TODAY)
    assert got == {}


def test_档案里的MMDD按最近一次解释():
    """`09/20` 在今天（09/15）之后 → 当成去年那天，不掉进未来。"""
    assert pf._parse_md("09/20", TODAY) == date(2025, 9, 20)
    assert pf._parse_md("09/10", TODAY) == date(2026, 9, 10)
    assert pf._parse_md("", TODAY) is None and pf._parse_md("坏了", TODAY) is None


def test_稳定度只算不用():
    habit = [_ev("r01", ev.SELECT, d) for d in (1, 8, 15, 22)]
    label = pf.stability(habit, "r01")
    assert label["label"] == "习惯" and label["cv"] is not None
    assert pf.stability([_ev("r01", ev.SELECT, 1)], "r01")["label"] == "刚开始"


# ---------------------------------------------------------------- 接进排序入口

def _db() -> RecipeDB:
    return RecipeDB(recipes=[
        Recipe(id="a", name="甲菜", category="热菜", time_min=20, cost_yuan=10,
               taste_tags=["咸鲜"]),
        Recipe(id="b", name="乙菜", category="热菜", time_min=20, cost_yuan=10,
               taste_tags=["咸鲜"]),
    ])


def test_没有事件时recipe_score与老行为一模一样():
    """**零回归的前提**：`dish_weights` 为空字典时，"喜欢 +8"必须原样保留。"""
    c = UserConstraints(people=2, days=3, liked_dishes=["a"])
    assert recipe_score(_db().recipes[0], c) == 8.0
    assert recipe_score(_db().recipes[1], c) == 0.0


def test_有权重时用权重而不是静态加分():
    c = UserConstraints(people=2, days=3, liked_dishes=["a"], dish_weights={"b": 9.0})
    assert recipe_score(_db().recipes[0], c) == 0.0        # 老字段被权重接管，不再 +8
    assert recipe_score(_db().recipes[1], c) == 9.0


def test_候选排序真的跟着权重走():
    c = UserConstraints(people=2, days=3, max_time_min=40, dish_weights={"b": 6.0, "a": -2.0})
    got = [r.id for r in retrieve_candidates(_db(), c, "晚餐")]
    assert got == ["b", "a"], got


# ---------------------------------------------------------------- 两种后端都要写事件

def _fresh_env(monkeypatch, tmp: Path):
    """给 JSON 后端指一个临时事件文件 + 临时档案文件。"""
    monkeypatch.setenv("STORAGE", "json")
    monkeypatch.setenv("RECIPE_EVENTS_FILE", str(tmp / "events.json"))
    monkeypatch.setenv("RECIPE_PROFILE_FILE", str(tmp / "profile.json"))


def test_json后端的事件读写(monkeypatch, tmp_path=None):
    tmp = Path(__file__).resolve().parent.parent / ".tmp" / f"ev_{uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    # **`STORAGE=json` 必须设**（`_fresh_env`）：只设事件文件路径的话，
    # fresh-load 出来的 `events.py` 末尾那段"数据库后端覆盖 JSON 实现"照样生效 ——
    # 这个测试就变成在读**真库**（以前它能过，只是因为真库里恰好一行事件都没有）。
    _fresh_env(monkeypatch, tmp)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"ev_json_{uuid4().hex[:6]}",
        Path(__file__).resolve().parent.parent / "recipe_planner" / "events.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.load_events() == []
    assert mod.record([{"recipe_id": "r01", "action": mod.LIKE}]) == 1
    rows = mod.load_events()
    assert len(rows) == 1 and rows[0]["recipe_id"] == "r01" and rows[0]["created_at"]
    assert len(mod.recent(days=1)) == 1
    mod.clear()
    assert mod.load_events() == []


def test_数据库后端的菜单事件与档案事件都会落库():
    """两个后端都要写（docs/12 §4 阶段二 2.2），这条在真库上验。"""
    import asyncio

    from recipe_planner.storage import db_events, engine, migrate, sync_bridge
    from recipe_planner.storage.repositories import PlanRepo, ProfileRepo, RecipeRepo

    tmp = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"
    tmp.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{(tmp / f'ev_{uuid4().hex[:8]}.db').as_posix()}"
    migrate.ensure_schema(url)
    old_url, old_storage = os.environ.get("DATABASE_URL"), os.environ.get("STORAGE")
    os.environ["DATABASE_URL"], os.environ["STORAGE"] = url, "db"
    engine.reset_engine()
    try:
        db = _db()
        sync_bridge.run(RecipeRepo.upsert_many(db.recipes))

        # ① 菜单事件：改动前后一比就有
        rec = sync_bridge.run(PlanRepo.save_plan(_result({(1, "晚餐"): ["a"]}), "2026-09-14"))
        after = _result({(1, "晚餐"): ["b"]})
        sync_bridge.run(PlanRepo.update_result(rec.id, after))
        rows = db_events.load_events()
        assert sorted((e["recipe_id"], e["action"]) for e in rows) == [
            ("a", ev.SWAP_OUT), ("b", ev.SELECT)], rows

        # ② 档案事件：写档案就有
        sync_bridge.run(ProfileRepo.save_profile({"liked_dishes": ["甲菜"], "history": {}}))
        rows = db_events.load_events()
        assert any(e["recipe_id"] == "a" and e["action"] == ev.LIKE for e in rows), rows
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
