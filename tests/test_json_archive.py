"""原子写 +「读失败不静默」（docs/11 §4.1 P0-2）。

这一组测试守的是**用户数据**：方案存档（`data/saved_plans.json`）与口味档案
（`data/customer_profile.json`）。两种曾经真实存在的坏行为：

1. `path.write_text(...)` 先截断再写 —— 写到一半出事，原来的数据直接没了；
2. `except Exception: return []` / `return {}` —— "读不出来"被当成"本来就没有"，
   紧接着的一次保存拿空基覆盖历史，而且界面上没有任何提示。

测试写法上的一个坑：`STORAGE=db`（默认）时 `store.load_records` / `prof.load_profile`
会被 `db_store` / `db_profile` 的**同名实现覆盖**（docs/08 §7）。所以这里不用顶层
`import`，而是按当时的环境把模块**重新加载一份**（`_fresh`）—— 只有那样才测得到 JSON 实现。
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest

from jsonmod import bind_json_events
from recipe_planner.infra import jsonfile
from recipe_planner.models import (ChosenDish, DayPlan, PlanResult, ShoppingItem,
                                   UserConstraints)

ROOT = Path(__file__).resolve().parent.parent
# 刻意不用 pytest 的 tmp_path：本机沙箱令牌连 mkdtemp 建出来的目录都列不了（见 07 踩坑 #8）
TMP = ROOT / ".tmp" / "json_archive"
TMP.mkdir(parents=True, exist_ok=True)


def _p(name: str) -> Path:
    p = TMP / f"{uuid.uuid4().hex[:6]}_{name}"
    if p.exists():
        p.unlink()
    return p


def _fresh(module_file: str, monkeypatch, **env):
    """按**当前**环境重新加载一份模块（这样 STORAGE=json 的分支才生效）。

    **必须连 `recipe_planner.events` 一起换成 JSON 版**（docs/07 踩坑 #44）：
    `store.py` / `profile.py` 里的 `events` 否则还是数据库版，
    记事件会写到真库上（`tests/jsonmod.py` 讲清了为什么）。
    """
    monkeypatch.setenv("STORAGE", "json")
    monkeypatch.setenv("USE_API", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    bind_json_events(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        f"_jsonmod_{module_file.replace('.', '_')}_{uuid.uuid4().hex[:6]}",
        ROOT / "recipe_planner" / module_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _result(days: int = 2) -> PlanResult:
    return PlanResult(
        constraints=UserConstraints(people=2, days=days, dishes_per_day=1),
        candidate_count=1,
        days=[DayPlan(day=d, meal="晚餐", dishes=[ChosenDish(recipe_id="a", reason="快手")])
              for d in range(1, days + 1)],
        shopping=[ShoppingItem(name="番茄", category="蔬菜", amount="2个", for_recipes=["番茄炒蛋"])],
        estimated_cost_yuan=16.0)


# ---------------------------------------------------------------- 写：原子

def test_写完不留临时文件_内容能原样读回():
    p = _p("atomic.json")
    jsonfile.write_json(p, {"version": 1, "plans": [{"id": "x"}]})
    assert json.loads(p.read_text(encoding="utf-8")) == {"version": 1, "plans": [{"id": "x"}]}
    assert not list(p.parent.glob(f".{p.name}.tmp-*")), "临时文件必须清掉"


def test_写失败时原文件一个字都不改(monkeypatch):
    """原子写的全部意义：目标文件**要么是旧的、要么是新的**，不能是半截的。"""
    p = _p("keep_old.json")
    jsonfile.write_json(p, {"a": 1})
    real_replace = jsonfile.os.replace

    def boom(src, dst):
        if Path(dst) == p:
            raise OSError("模拟：换名那一刻磁盘满了")
        return real_replace(src, dst)

    monkeypatch.setattr(jsonfile.os, "replace", boom)
    with pytest.raises(OSError):
        jsonfile.write_json(p, {"a": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}, "旧内容必须完好"
    assert not list(p.parent.glob(f".{p.name}.tmp-*")), "临时文件要清掉"


# ---------------------------------------------------------------- 读：不存在 vs 读不出来

def test_没有文件就是真的没有(monkeypatch):
    p = _p("missing.json")
    store = _fresh("store.py", monkeypatch, RECIPE_PLAN_FILE=p)
    assert store.load_records() == []


def test_存档读不出来要报错_并且原样留档(monkeypatch):
    p = _p("plans.json")
    p.write_text("{ 这不是合法 json", encoding="utf-8")
    store = _fresh("store.py", monkeypatch, RECIPE_PLAN_FILE=p)

    with pytest.raises(jsonfile.ArchiveBroken) as ei:
        store.load_records()
    assert ei.value.backup is not None and ei.value.backup.exists()
    assert ei.value.backup.read_text(encoding="utf-8") == "{ 这不是合法 json", "原始字节一个都不许丢"
    assert not p.exists(), "坏文件已经改名留档，不会占着原路径"
    assert "留档" in str(ei.value), "报错要把「去哪找」说清楚"


def test_报错之后坏内容不会被下一次保存覆盖(monkeypatch):
    """曾经的事故：读成空存档 → 下一次保存把历史全覆盖。现在坏内容留在留档文件里。"""
    p = _p("plans_broken.json")
    p.write_text("{ 这不是合法 json", encoding="utf-8")
    store = _fresh("store.py", monkeypatch, RECIPE_PLAN_FILE=p)

    with pytest.raises(jsonfile.ArchiveBroken) as ei:
        store.load_records()
    backup = ei.value.backup

    store.save_plan(_result(), start_date="2026-09-14", change_note="出了事之后又排了一版")
    assert backup.read_text(encoding="utf-8") == "{ 这不是合法 json", "留档不许被覆盖"
    assert len(store.load_records()) == 1


def test_单条坏掉也报错而不是静默跳过(monkeypatch):
    """静默跳过 = 下一次保存顺手把那一份方案删了。宁可报错。"""
    p = _p("plans_badrow.json")
    p.write_text('{"version":1,"plans":[{"id":"x"}]}', encoding="utf-8")
    store = _fresh("store.py", monkeypatch, RECIPE_PLAN_FILE=p)
    with pytest.raises(jsonfile.ArchiveBroken):
        store.load_records()


def test_一份方案存进去能原样读回来(monkeypatch):
    p = _p("roundtrip.json")
    store = _fresh("store.py", monkeypatch, RECIPE_PLAN_FILE=p)
    res = _result(days=3)
    rec = store.save_plan(res, start_date="2026-09-14", change_note="首次生成")

    got = store.get_record(rec.id)
    assert got is not None
    assert [[d.recipe_id for d in day.dishes] for day in got.result.days] == \
           [[d.recipe_id for d in day.dishes] for day in res.days]
    assert got.change_note == "首次生成"


# ---------------------------------------------------------------- 档案同理

def test_档案读不出来要报错并留档(monkeypatch):
    p = _p("customer_profile.json")
    p.write_text("{ 也坏了", encoding="utf-8")
    prof = _fresh("profile.py", monkeypatch, RECIPE_PROFILE_FILE=p)

    with pytest.raises(jsonfile.ArchiveBroken) as ei:
        prof.load_profile()
    assert ei.value.backup.read_text(encoding="utf-8") == "{ 也坏了"


def test_档案读不出来时不会把喜欢清空(monkeypatch):
    """`set_feedback` 是"先读、再写"：读失败就必须停下来，不能拿空档案写回去。"""
    p = _p("profile_keep.json")
    p.write_text('{"customer_name":"默认客户","liked_dishes":["番茄炒蛋"]', encoding="utf-8")
    prof = _fresh("profile.py", monkeypatch, RECIPE_PROFILE_FILE=p)

    with pytest.raises(jsonfile.ArchiveBroken) as ei:
        prof.set_feedback("清炒时蔬", "like", {"番茄炒蛋", "清炒时蔬"})
    assert "番茄炒蛋" in ei.value.backup.read_text(encoding="utf-8"), "原来的喜欢还在留档里"


def test_档案不存在时是空档案而不是报错(monkeypatch):
    p = _p("profile_new.json")
    prof = _fresh("profile.py", monkeypatch, RECIPE_PROFILE_FILE=p)
    assert prof.load_profile() == {}
    prof.set_feedback("番茄炒蛋", "like", {"番茄炒蛋"})
    assert prof.liked_names({"番茄炒蛋"}) == ["番茄炒蛋"]
