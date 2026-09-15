"""排菜任务必须用上**服务端档案里的**喜欢 / 不喜欢（跨后端一致性）。

为什么单独一条：客户端提交排菜任务时会**主动把** `liked_dishes` / `disliked_dishes` 去掉
（`client.create_plan` 的注释写着"服务端自己读档案，不接受界面塞进来的偏好"），
所以服务端必须在**自己这一侧**把档案合进约束 —— 否则服务直连模式下排出来的菜单
既不会避开"不喜欢"的菜，也不会优先"喜欢"的菜，而这两种模式（直连 / 服务化）
本该给出同样的结果（docs/11 的 R5：多后端分叉）。

这条用**真流水线**（不注入假图），所以它测的是"排出来的菜"，不是"接口返回了什么"。
"""
from __future__ import annotations

import pytest

from conftest import wait_job


async def _plan_ids(client, body: dict) -> tuple[list[str], dict]:
    r = await client.post("/api/v1/plans", json=body)
    assert r.status_code == 202, r.text
    done = await wait_job(client, r.json()["job_id"], timeout=60)
    assert done["status"] == "succeeded", done
    detail = (await client.get(f"/api/v1/plans/{done['plan_id']}")).json()
    ids = [d["recipe_id"] for day in detail["days"] for d in day["dishes"]]
    return ids, detail


@pytest.mark.asyncio
async def test_排菜会避开档案里不喜欢的菜(api, client):
    """`tests/api/conftest.py` 的档案里「红烧排骨」（r3）是"不喜欢"。"""
    ids, _detail = await _plan_ids(client, {"days": 3, "dishes_per_day": 2,
                                            "max_time_min": 60, "spice_level": "辣"})
    assert ids, "排出来的菜单不该是空的"
    assert "r3" not in ids, f"被标为「不喜欢」的菜还是被排进来了：{ids}"


@pytest.mark.asyncio
async def test_任务里带着服务端档案翻出来的偏好(api, client):
    """契约断言（不可能空过）：任务行里存的需求必须带上从档案翻出来的**菜谱 id**。

    客户端不会传这两个字段（它主动 pop 掉了），所以"任务里有没有"完全由服务端决定；
    档案里存的是菜名，而排序比的是 id —— 这一步翻译错了，硬排除与（阶段二的）权重会一起失效。

    注意**不能**从 `GET /jobs/{id}` 读：`_to_out()` 有意把 `request` 收窄成
    people/days/dishes_per_day/start_date 四个字段（不对外暴露内部需求），
    所以要直接读任务行的原文。
    """
    import json
    import os
    import sqlite3

    r = await client.post("/api/v1/plans", json={"days": 3})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    path = os.environ["DATABASE_URL"].split("///", 1)[-1]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        raw = con.execute("SELECT request FROM job WHERE id = ?", (job_id,)).fetchone()[0]
    finally:
        con.close()
    req = json.loads(raw)

    assert req["disliked_dishes"] == ["r3"], req      # 红烧排骨
    assert "r1" in req["liked_dishes"], req           # 番茄炒蛋
    await wait_job(client, job_id, timeout=60)


@pytest.mark.asyncio
async def test_任务里带着两个偏好信号(api, client):
    """服务化模式下，"会衰减的权重"与"上次吃是多少天前"也必须是**服务端**算出来的。

    这两个信号在直连模式下由 `app.build_constraints` 算（界面进程读同一份事件），
    服务化模式下客户端**什么都没传**（`client.create_plan` 只提交需求表单）——
    所以服务端不注入的话，`USE_API=1` 的部署里菜单上永远不会出现"好久没吃这道了"，
    这正是上一次"服务模式排菜不读档案"的同一类洞（当时也是行为测试才逼出来的）。

    为了让断言不可能空过，先往库里塞两条真事件：`done`（40 天前）+ `like`（3 天前）。
    用一道**档案里没提过**的菜，这样权重里只有这两条事件在起作用（数值是精确的）。
    """
    import json
    import os
    import sqlite3
    from datetime import datetime, timedelta

    path = os.environ["DATABASE_URL"].split("///", 1)[-1]
    con = sqlite3.connect(path)
    try:
        hid = con.execute("SELECT id FROM household LIMIT 1").fetchone()[0]
        rid = con.execute("SELECT id FROM recipe WHERE name NOT IN"
                          " ('番茄炒蛋', '红烧排骨', '清炒时蔬') LIMIT 1").fetchone()[0]
        now = datetime.now()
        for action, days, meal in (("done", 40, "晚餐"), ("like", 3, "")):
            con.execute(
                "INSERT INTO dish_event (household_id, recipe_id, action, meal, day_no, source,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (hid, rid, action, meal, 1 if meal else None, "做完了", now - timedelta(days=days)))
        con.commit()
    finally:
        con.close()

    r = await client.post("/api/v1/plans", json={"days": 3})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        req = json.loads(con.execute(
            "SELECT request FROM job WHERE id = ?", (job_id,)).fetchone()[0])
    finally:
        con.close()

    assert req["dish_last_seen"].get(rid) in (40, 41), req["dish_last_seen"]
    assert req["dish_weights"].get(rid) == 8.0, req["dish_weights"]
    await wait_job(client, job_id, timeout=60)
