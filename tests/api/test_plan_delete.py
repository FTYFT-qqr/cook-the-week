"""`DELETE /plans/{plan_id}?confirm=true`（界面「以前的方案」里的"删除"按钮）。

要点三条：
1. **破坏性操作必须二次确认**：缺 `confirm=true` → 409，而且方案一行都不能少；
2. **删干净**：天 / 菜 / 清单 / 勾选 / 操作日志这些子表里的行也要一起走 ——
   SQLite 的外键约束不一定开着，靠数据库级联会留下孤儿行；
3. 删除后**没有撤销**（`undo_hint=None`）：删掉的数据找不回来，给个假的"可以撤销"是骗人。

运行：& 'D:\\conda\\cook\\recipe-planner\\python.exe' -m pytest tests -q
"""
from __future__ import annotations

from sqlalchemy import func, select

from recipe_planner.storage.engine import session_scope
from recipe_planner.storage.orm import ActionLog, PlanDay, PlanDish, ShoppingCheck, ShoppingItem
from recipe_planner.storage.repositories import LogRepo, PlanRepo

CHILD_TABLES = (("plan_day", PlanDay), ("plan_dish", PlanDish),
                ("shopping_item", ShoppingItem), ("shopping_check", ShoppingCheck),
                ("action_log", ActionLog))


async def child_counts(plan_id: str) -> dict[str, int]:
    """直接查库：这份 plan_id 在各子表里还剩几行（孤儿行的证据）。"""
    counts: dict[str, int] = {}
    async with session_scope() as session:
        for name, model in CHILD_TABLES:
            counts[name] = int((await session.execute(
                select(func.count()).select_from(model)
                .where(model.plan_id == plan_id))).scalar() or 0)
    return counts


async def other_plan(result_factory, start: str = "2026-09-21", note: str = "第二版"):
    """再排一份（更新的）方案，用来证明「其它版本没动」。"""
    return await PlanRepo.save_plan(result_factory(), start, note)


# ---------------------------------------------------------------- 二次确认

async def test_delete_without_confirm_is_409_and_nothing_is_deleted(api, client):
    _, _, record = api
    before = await child_counts(record.id)
    assert before["plan_day"] == 3 and before["plan_dish"] == 6   # 夹具那 3 天 6 道

    r = await client.delete(f"/api/v1/plans/{record.id}")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "confirm_required"
    assert "删除后无法找回" in body["message"] and "其它版本不受影响" in body["message"]
    steps = body["details"]["next_steps"]
    assert len(steps) >= 2, "至少要给「先看看这一周」与「算了，先不删」两个可点击的选择"
    assert all(s["op"] and s["label"] for s in steps)

    # 方案还在，子表也一行没少
    assert (await client.get(f"/api/v1/plans/{record.id}")).status_code == 200
    assert await child_counts(record.id) == before


# ---------------------------------------------------------------- 删掉

async def test_delete_with_confirm_removes_the_plan_and_its_child_rows(api, client,
                                                                      result_factory):
    old = api[2]
    new = await other_plan(result_factory)
    # 给这份方案留下各种子表行（否则"删干净"这句话没被测到）
    await PlanRepo.set_checked(old.id, ["番茄", "青菜"])          # shopping_check
    await PlanRepo.set_done(old.id, 1, True)                     # plan_day.done_at
    await LogRepo.add(old.id, "test", "给这份方案留一条操作流水")   # action_log
    before = await child_counts(old.id)
    assert before == {"plan_day": 3, "plan_dish": 6, "shopping_item": 3,
                      "shopping_check": 2, "action_log": 1}, before

    r = await client.delete(f"/api/v1/plans/{old.id}", params={"confirm": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "plan_delete"
    assert body["message"] == (f"已删除「{old.label}」这一版方案"
                               "（其它版本没动，删除后无法找回）。")
    assert body["data"] == {"deleted_id": old.id, "remaining": 1}
    assert body["action_log_id"]
    # 删掉的数据找不回来：**故意**不给撤销线索
    assert body["undo_hint"] is None
    assert [s["op"] for s in body["next_steps"]] == ["list_plans", "create_plan"]

    # 这一版没了、其它版本还在
    assert (await client.get(f"/api/v1/plans/{old.id}")).status_code == 404
    assert (await client.get(f"/api/v1/plans/{new.id}")).status_code == 200
    assert (await client.get("/api/v1/plans")).json()["total"] == 1

    # 最重要的一条：子表里不能留下孤儿行
    assert await child_counts(old.id) == {"plan_day": 0, "plan_dish": 0, "shopping_item": 0,
                                         "shopping_check": 0, "action_log": 0}
    # 另一份方案的子表一行没动
    assert (await child_counts(new.id))["plan_day"] == 3


async def test_deleting_the_latest_version_falls_back_to_the_older_one(client, result_factory):
    """删掉最新那一版之后，今晚页要老实指向剩下那一版（不能指向已删的 id）。"""
    new = await other_plan(result_factory)
    assert (await client.delete(f"/api/v1/plans/{new.id}",
                                params={"confirm": "true"})).json()["data"]["remaining"] == 1

    tonight = (await client.get("/api/v1/plans/current")).json()
    assert tonight["plan_id"] and tonight["plan_id"] != new.id


# ---------------------------------------------------------------- 不存在

async def test_delete_missing_plan_is_404(client):
    r = await client.delete("/api/v1/plans/deadbeef", params={"confirm": "true"})
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "plan_not_found"
    assert body["details"]["next_steps"][0]["op"] == "list_plans"
