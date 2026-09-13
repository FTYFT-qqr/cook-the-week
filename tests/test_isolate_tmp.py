"""临时脚本的隔离守卫（`scripts/isolate_tmp.py`）。

这个文件是**两次数据事故**换来的，所以它自己也得有人看着：
一次是诊断脚本写进了真实库，一次是写进了真实 JSON 存档（把迁移基线里最老的一版方案挤掉了）。
守卫要是悄悄失效，下次就又是"用户的方案少了一份"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import isolate_tmp  # noqa: E402


@pytest.fixture()
def clean_env(monkeypatch):
    """把一个干净的环境给用例用（跑测试时环境里可能已经带着上一轮的值）。"""
    for key in ("STORAGE", "DATABASE_URL", "RECIPE_PLAN_FILE", "RECIPE_PROFILE_FILE"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_没隔离时会被拦下来(clean_env):
    """三个落点一个都没设 → 全部报出来（这正是第⑦步那次事故的样子）。"""
    bad = isolate_tmp.unsafe()
    assert any("DATABASE_URL" in b for b in bad), bad
    assert any("RECIPE_PLAN_FILE" in b for b in bad), bad
    assert any("RECIPE_PROFILE_FILE" in b for b in bad), bad


def test_只设了_storage_仍然会被拦下来(clean_env):
    """第③步的错法：以为设了 STORAGE=json 就安全了。"""
    clean_env.setenv("STORAGE", "json")
    bad = isolate_tmp.unsafe()
    assert any("RECIPE_PLAN_FILE" in b for b in bad), bad


def test_指向真实目录会被拦下来(clean_env):
    clean_env.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(ROOT / 'data' / 'app.db').as_posix()}")
    clean_env.setenv("RECIPE_PLAN_FILE", str(ROOT / "data" / "saved_plans.json"))
    clean_env.setenv("RECIPE_PROFILE_FILE", str(ROOT / "data" / "customer_profile.json"))
    bad = isolate_tmp.unsafe()
    assert len(bad) == 3, bad
    with pytest.raises(RuntimeError):
        isolate_tmp.assert_safe()


def test_isolate_之后三个落点都在_tmp(clean_env):
    env = isolate_tmp.isolate(tag="unittest")
    assert isolate_tmp.unsafe() == [], isolate_tmp.unsafe()
    for key in ("DATABASE_URL", "RECIPE_PLAN_FILE", "RECIPE_PROFILE_FILE"):
        # 用 posix 形式比：helper 返回的是本机原生的 Windows 路径（app 要的就是原生路径）
        assert "/.tmp/" in Path(env[key].replace("\\", "/")).as_posix(), (key, env[key])
    assert env["STORAGE"] in ("json", "db")
    # 确定性路径：临时脚本不该真的去调模型
    assert os.environ["DEEPSEEK_API_KEY"] == ""


def test_真实数据文件没被测试碰过():
    """跑完这一组用例，真实存档还是三份、还是那几个 id（只读检查）。"""
    import json

    raw = json.loads((ROOT / "data" / "saved_plans.json").read_text(encoding="utf-8"))
    plans = raw.get("plans", []) if isinstance(raw, dict) else raw
    assert len(plans) <= 3, "JSON 存档最多 3 份（store.MAX_PLANS）"
    assert all(p.get("id") and p.get("result") for p in plans), "存档里的方案不能被截断"
