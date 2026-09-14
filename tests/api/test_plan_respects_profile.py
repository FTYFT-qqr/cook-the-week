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
