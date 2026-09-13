"""临时脚本的"隔离环境"：把**三种**存储都指到 `.tmp/`，绝不碰 `data/` 里的真实数据。

## 为什么有这个文件（两次事故换来的）

1. **2026-09-13 第③步**：一个诊断脚本只设了 `STORAGE=json`，没设 `DATABASE_URL`，
   默认走到了真实库 `data/app.db`，造出 2 版方案（plan 3→5、plan_day 9→18…），
   事后按原始计数逐表还原。当时的结论只写了"临时脚本要设 `STORAGE=json` 或改 `DATABASE_URL`"——
   **那条结论不够**。
2. **2026-09-13 第⑦步**：诊断脚本设了 `STORAGE=json`，以为安全了 —— 结果 `app.py` 生成菜单时
   写进了**真实的** `data/saved_plans.json`。那份存档只留最近 3 份（`store.MAX_PLANS`），
   于是把迁移基线里最老的一版方案挤掉了，`verify_migration.py` 立刻变红
   （"JSON 里每一份方案都还在库里"失败）。从 `data/backup/20260913-204312/` 按字节还原。

教训：**"真实数据"不是一个文件，是三个** ——
SQLite（`DATABASE_URL`）、JSON 存档（`RECIPE_PLAN_FILE`）、口味档案（`RECIPE_PROFILE_FILE`）。
任何跑 `app.py` / 领域代码的临时脚本，开头先 `isolate()` 一句，三样一起隔离。

用法：

    import sys; sys.path.insert(0, "scripts")
    from isolate_tmp import isolate
    isolate()                      # 必须在 import app / recipe_planner 之前调用
    from streamlit.testing.v1 import AppTest
    ...
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"

# 真实数据的三个落点（前缀匹配；只要有一个落到这里就是没隔离干净）
REAL_PREFIXES = (str(ROOT / "data") + os.sep, (ROOT / "data").as_posix() + "/")


def isolate(tag: str = "diag", storage: str = "json") -> dict[str, str]:
    """把三种存储全指到 `.tmp/`，返回设好的环境变量（方便脚本打印出来自证）。

    `tag` 用来给临时文件起名（例如 `diag` → `.tmp/diag_plans.json`），
    这样并行跑几个脚本也不会互相踩。
    """
    TMP.mkdir(parents=True, exist_ok=True)
    env = {
        "STORAGE": storage,
        "DATABASE_URL": f"sqlite+aiosqlite:///{(TMP / f'{tag}.db').as_posix()}",
        "RECIPE_PLAN_FILE": str(TMP / f"{tag}_plans.json"),
        "RECIPE_PROFILE_FILE": str(TMP / f"{tag}_profile.json"),
        # 排菜走确定性路径：不联网、不用等模型，也不产生费用
        "DEEPSEEK_API_KEY": "",
        "RATE_LIMIT": "off",
        "AUTH_MODE": "off",
    }
    os.environ.update(env)
    assert_safe()
    return env


def unsafe() -> list[str]:
    """现在还有哪些地方会落到真实 `data/`（空列表 = 隔离干净）。

    **"没设"同样危险**：`RECIPE_PLAN_FILE` / `RECIPE_PROFILE_FILE` 不设，
    `store` / `profile` 就用 `data/` 下的默认文件；`STORAGE=db` 而 `DATABASE_URL` 不设，
    就用 `data/app.db`。所以这里把"没设"也报出来 —— 第⑦步那次事故正是"设了 STORAGE、没设另外两个"。
    """
    bad: list[str] = []
    storage = (os.environ.get("STORAGE") or "db").lower()

    db_url = os.environ.get("DATABASE_URL") or ""
    if db_url:
        if any(p in db_url for p in REAL_PREFIXES):
            bad.append(f"DATABASE_URL={db_url}")
    elif storage == "db":
        bad.append("DATABASE_URL 没设（STORAGE=db 时会写到 data/app.db）")

    for key, default in (("RECIPE_PLAN_FILE", "data/saved_plans.json"),
                         ("RECIPE_PROFILE_FILE", "data/customer_profile.json")):
        value = os.environ.get(key) or ""
        if not value:
            bad.append(f"{key} 没设（默认会写到 {default}）")
        elif any(p in value for p in REAL_PREFIXES):
            bad.append(f"{key}={value}")
    return bad


def assert_safe() -> None:
    """没隔离干净就直接炸掉 —— 宁可脚本跑不起来，也不能再动用户的数据。"""
    bad = unsafe()
    if bad:
        raise RuntimeError(
            "临时脚本会动到真实数据，先调用 isolate() 再继续：" + "；".join(bad))
