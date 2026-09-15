"""测试里"按 JSON 后端重新加载一个模块"的**唯一正确姿势**（docs/07 踩坑 #44）。

## 为什么需要它

`store.py` / `profile.py` 顶层写着 `from recipe_planner import events`，
而 `events.py` 在 `STORAGE=db`（默认）时会把末尾几个函数**换成数据库实现**。
于是"按 `STORAGE=json` fresh-load 一个 store / profile"这种测试里：
模块自己的函数是 JSON 版，**里面的 `events` 还是数据库版** ——
记一次事件就写到 `DATABASE_URL` 指向的库上（本地跑就是**真库**）。

`from X import Y` 取的是**包属性**而不是 `sys.modules`，所以只换 `sys.modules` 没用；
两个都要换。这不是洁癖：`dish_event` 的 `plan_id` 有外键兜着，`plan_id=None` 的
**档案类事件**没人兜 —— 真会落进真库（`tests/test_json_archive.py` 里那几处
只是恰好用了库里没有的菜名，才没写进去）。
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent


def _load_json_events() -> Any:
    spec = importlib.util.spec_from_file_location(
        f"ev_json_{uuid4().hex[:6]}", ROOT / "recipe_planner" / "events.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bind_json_events(monkeypatch) -> Any:
    """把 `recipe_planner.events` 换成**JSON 版**（测试结束自动还原）。"""
    mod = _load_json_events()
    monkeypatch.setattr(importlib.import_module("recipe_planner"), "events", mod)
    monkeypatch.setitem(sys.modules, "recipe_planner.events", mod)
    return mod


@contextlib.contextmanager
def json_events_bound():
    """同上，但手工进出（给"自己管 os.environ、没有 monkeypatch"的老测试用）。"""
    pkg = importlib.import_module("recipe_planner")
    old_attr, old_mod = getattr(pkg, "events", None), sys.modules.get("recipe_planner.events")
    mod = _load_json_events()
    pkg.events = mod
    sys.modules["recipe_planner.events"] = mod
    try:
        yield mod
    finally:
        if old_attr is not None:
            pkg.events = old_attr
        if old_mod is not None:
            sys.modules["recipe_planner.events"] = old_mod


def json_load(module_file: str, monkeypatch, **env) -> Any:
    """按"JSON 后端"重新加载 `recipe_planner/<module_file>`，并把 events 也换成 JSON 版。

    `env`：额外的环境变量（例如 `RECIPE_PLAN_FILE`、`RECIPE_EVENTS_FILE`）。
    """
    monkeypatch.setenv("STORAGE", "json")
    monkeypatch.setenv("USE_API", "0")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    bind_json_events(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        f"_jsonmod_{module_file.replace('.', '_')}_{uuid4().hex[:6]}",
        ROOT / "recipe_planner" / module_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
