"""P1-3 验收：单点写入接口（docs/09 P1-3）。

核心不是"接口能改数据"，而是三件事：
1. **改一处只动一处**（05 §4 R1）：用数据库快照对比，别的天/别的方案/档案必须一动不动；
2. **回执是人话**（05 §5.3）：句式「已<做了什么>，<哪里没动>」；
3. **撤销是精确逆操作**：拿 `undo_hint` 原样再发一次，必须回到改动前的样子。

运行：python -m pytest tests -q
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from recipe_planner.storage.engine import get_sessionmaker
from recipe_planner.storage.orm import PlanDay, PlanDish, Preference, Rating
from recipe_planner.storage.repositories import PlanRepo

RECEIPT = re.compile(r"^已.+。$")          # 05 §5.3：动词开头、一句说完、句号收尾
NO_TOUCH = re.compile(r"(其他 \d+ 天没动|其他菜不受影响|菜单不变|本周菜单没动)")


# ---------------------------------------------------------------- 快照工具

async def snapshot() -> dict:
    """整库快照：每天有哪些菜 / 每天的状态 / 档案。用于证明"只动了一处"。"""
    async with get_sessionmaker()() as session:
        days = (await session.execute(select(PlanDay))).scalars().all()
        dishes = (await session.execute(select(PlanDish))).scalars().all()
        prefs = (await session.execute(select(Preference))).scalars().all()
        ratings = (await session.execute(select(Rating))).scalars().all()

    by_day: dict[tuple[str, int], list[str]] = {}
    for d in sorted(days, key=lambda x: (x.plan_id, x.day_no)):
        by_day[(d.plan_id, d.day_no)] = [x.recipe_id for x in
                                        sorted((y for y in dishes
                                                if y.plan_id == d.plan_id and y.day_no == d.day_no),
                                               key=lambda z: z.seq)]
    return {
        "dishes": by_day,
        "state": {(d.plan_id, d.day_no): (d.skipped, d.people_override, d.done_at is not None)
                  for d in days},
        "prefs": sorted((p.recipe_id, p.kind) for p in prefs),
        "ratings": sorted((r.recipe_id, r.score) for r in ratings),
    }


def changed_days(before: dict, after: dict) -> set[tuple[str, int]]:
    return {key for key in before["dishes"]
            if before["dishes"][key] != after["dishes"].get(key)}


def other_days(before: dict, plan_id: str, keep: int) -> set[tuple[str, int]]:
    return {k for k in before["dishes"] if k[0] == plan_id and k[1] != keep}


# ---------------------------------------------------------------- 换一道

async def test_swap_touches_only_that_one_dish(api, client):
    _, _, record = api
    before = await snapshot()
    day2_before = before["dishes"][(record.id, 2)]
    target = day2_before[0]

    r = await client.patch(f"/api/v1/plans/{record.id}/days/2",
                           json={"op": "swap", "recipe_id": target})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    # 只有第 2 天变了，而且只换了那一道（另一道原样）
    assert changed_days(before, after) == {(record.id, 2)}
    day2_after = after["dishes"][(record.id, 2)]
    assert target not in day2_after
    assert day2_before[1] == day2_after[1], "同一天的另一道菜不该被动"
    assert sorted(day2_after) != sorted(day2_before)
    # 其他天的状态（跳过/加人/做完）一个都没动
    for key in other_days(before, record.id, 2):
        assert before["state"][key] == after["state"][key]

    # 回执：05 §5.3 句式 + 说清哪里没动
    assert RECEIPT.match(body["message"]), body["message"]
    assert NO_TOUCH.search(body["message"]), body["message"]
    assert body["kind"] == "swap" and body["data"]["day_detail"]["day"] == 2
    assert body["action_log_id"] is not None
    assert body["undo_hint"]["body"]["op"] == "replace_day"
    # 清单跟着新菜单重算了（不是留着旧食材）
    assert body["data"]["day_detail"]["dishes"]


async def test_undo_hint_restores_exactly(api, client):
    """拿 undo_hint 原样再发一次，必须回到改动前（精确逆操作，不是"再换一次"）。"""
    _, _, record = api
    before = await snapshot()
    target = before["dishes"][(record.id, 2)][0]

    r1 = await client.patch(f"/api/v1/plans/{record.id}/days/2",
                            json={"op": "swap", "recipe_id": target})
    hint = r1.json()["undo_hint"]
    assert hint["path"].endswith(f"/plans/{record.id}/days/2")

    r2 = await client.request(hint["method"], hint["path"], json=hint["body"])
    assert r2.status_code == 200, r2.text
    after = await snapshot()
    assert after["dishes"] == before["dishes"], "撤销没有回到改动前"


# ---------------------------------------------------------------- 不做饭 / 改回来

async def test_skip_then_restore(api, client):
    _, _, record = api
    before = await snapshot()
    dropped = await client.get(f"/api/v1/plans/{record.id}/days/2")
    dropped_names = [d["name"] for d in dropped.json()["dishes"]]

    r = await client.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "skip"})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert after["dishes"][(record.id, 2)] == []                  # 这天清空了
    assert after["state"][(record.id, 2)][0] is True              # skipped
    assert changed_days(before, after) == {(record.id, 2)}
    assert "不做饭" in body["message"] and RECEIPT.match(body["message"])
    assert body["data"]["day_detail"]["skipped"] is True

    # 改回来：这天重新有菜，其他天仍不动
    mid = await snapshot()
    r2 = await client.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "restore"})
    assert r2.status_code == 200, r2.text
    after2 = await snapshot()
    assert after2["dishes"][(record.id, 2)], "恢复做饭后这天还是空的"
    assert changed_days(mid, after2) == {(record.id, 2)}
    assert after2["state"][(record.id, 2)][0] is False
    assert "恢复做饭" in r2.json()["message"]
    assert dropped_names  # 原来那天确实有菜


async def test_skip_twice_is_conflict_with_next_steps(api, client):
    _, _, record = api
    await client.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "skip"})
    r = await client.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "skip"})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "already_skipped"
    assert "本来就没安排" in body["message"]


# ---------------------------------------------------------------- 来客人了

async def test_people_only_changes_that_day(api, client):
    _, _, record = api
    before = await snapshot()

    r = await client.patch(f"/api/v1/plans/{record.id}/days/2",
                           json={"op": "people", "people": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert after["state"][(record.id, 2)][1] == 4                 # 只有这天的人数是 4
    for key in other_days(before, record.id, 2):
        assert after["state"][key][1] == before["state"][key][1]
    assert "(其他 2 天没动)" in body["message"] or "其他 2 天没动" in body["message"]
    assert "按 4 人算" in body["message"]
    assert body["data"]["day_detail"]["people"] == 4
    assert body["data"]["day_detail"]["cost"] > 0
    # 撤销回原人数
    r2 = await client.patch(f"/api/v1/plans/{record.id}/days/2",
                            json={"op": "people", "people": 2})
    assert r2.status_code == 200
    assert (await snapshot())["state"][(record.id, 2)][1] is None


# ---------------------------------------------------------------- 回家晚了

async def test_faster_makes_the_day_quicker(api, client):
    _, _, record = api
    before = await snapshot()
    minutes_before = (await client.get(f"/api/v1/plans/{record.id}/days/2")).json()["minutes"]

    r = await client.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "faster"})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert changed_days(before, after) == {(record.id, 2)}, "快手组合只该改这一天"
    assert body["data"]["day_detail"]["minutes"] < minutes_before
    assert "快手组合" in body["message"]
    assert NO_TOUCH.search(body["message"])
    assert body["undo_hint"]["body"]["op"] == "replace_day"
    # 其他天的状态标记一个都没动
    for key in other_days(before, record.id, 2):
        assert before["state"][key] == after["state"][key]


# ---------------------------------------------------------------- 哪里能省

async def test_save_money_reports_saving_and_touches_one_day(api, client):
    _, _, record = api
    before = await snapshot()

    r = await client.post(f"/api/v1/plans/{record.id}/save-money")
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert body["data"]["saving_yuan"] > 0
    assert "省了约 ¥" in body["message"]
    assert changed_days(before, after) == {(record.id, body["data"]["day"])}
    assert body["data"]["day_detail"]["dishes"]


async def test_save_money_when_already_cheapest(api, client, result_factory, start_date):
    """已经没有更省的换法 → 409 + 可点击的放宽项（05 §5.1：失败必须给补救项）。"""
    _, _, record = api
    # 5 天把 5 道菜全用掉，才真的"换无可换"（否则总有一道更便宜的替代品）
    cheap = result_factory(days=5, dishes_per_day=1)
    for i, day in enumerate(cheap.days):
        day.dishes = [day.dishes[0].__class__(recipe_id=f"r{i + 1}", reason="便宜")]
    saved = await PlanRepo.save_plan(cheap, start_date, "全都用上了", make_active=False)

    r = await client.post(f"/api/v1/plans/{saved.id}/save-money")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "already_cheapest"
    assert body["details"]["next_steps"]


# ---------------------------------------------------------------- 反馈

async def test_like_changes_profile_only(api, client):
    _, _, record = api
    before = await snapshot()
    # 挑一道**还没表过态**的菜（否则点击会走 toggle 语义，见下一个用例）
    profile = (await client.get("/api/v1/profile")).json()
    spoken = set(profile["liked_dishes"]) | set(profile["disliked_dishes"])
    detail = (await client.get(f"/api/v1/plans/{record.id}")).json()
    target = next((day, d) for day in detail["days"] for d in day["dishes"]
                  if d["name"] not in spoken)

    r = await client.post(
        f"/api/v1/plans/{record.id}/dishes/{target[0]['day']}/{target[1]['recipe_id']}"
        f"/feedback", json={"op": "like"})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert after["dishes"] == before["dishes"], "点了喜欢不该动菜单"
    assert after["prefs"] != before["prefs"]
    assert any(kind == "like" for rid, kind in after["prefs"] if rid == target[1]["recipe_id"])
    assert "菜单不变" in body["message"]
    assert body["data"]["profile"]["liked_dishes"]


async def test_like_on_already_liked_dish_toggles_off(api, client):
    """钉住现状：对**已经喜欢**的菜再点一次喜欢，会把它取消（toggle 语义）。

    这个语义有点可疑 —— 打分那条路是"确保喜欢"，这里却是"来回切"。
    已记在 docs/09 待产品负责人定夺（建议统一成"确保"）。真改了这里会红，
    正好提醒改的人顺带更新文档。
    """
    _, _, record = api
    dish = (await client.get(f"/api/v1/plans/{record.id}/days/1")).json()["dishes"][0]
    assert dish["name"] == "番茄炒蛋"                     # 档案里本来就喜欢这道

    r = await client.post(
        f"/api/v1/plans/{record.id}/dishes/1/{dish['recipe_id']}/feedback", json={"op": "like"})
    assert r.status_code == 200
    assert dish["name"] not in (await client.get("/api/v1/profile")).json()["liked_dishes"]


async def test_dislike_replaces_the_dish_on_that_day_only(api, client):
    _, _, record = api
    before = await snapshot()
    dish = (await client.get(f"/api/v1/plans/{record.id}/days/2")).json()["dishes"][0]

    r = await client.post(
        f"/api/v1/plans/{record.id}/dishes/2/{dish['recipe_id']}/feedback",
        json={"op": "dislike"})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert any(rid == dish["recipe_id"] and kind == "dislike" for rid, kind in after["prefs"])
    assert dish["recipe_id"] not in after["dishes"][(record.id, 2)]
    assert changed_days(before, after) <= {(record.id, 2)}
    assert "以后不再出现" in body["message"]


async def test_lock_keeps_the_dish_through_replan(api, client):
    _, _, record = api
    before = await snapshot()
    dish = (await client.get(f"/api/v1/plans/{record.id}/days/1")).json()["dishes"][0]

    r = await client.post(
        f"/api/v1/plans/{record.id}/dishes/1/{dish['recipe_id']}/feedback", json={"op": "lock"})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert after["dishes"] == before["dishes"], "定住不该动菜单"
    assert any(d["locked"] for d in body["data"]["day_detail"]["dishes"]), \
        body["data"]["day_detail"]["dishes"]
    assert "已定住" in body["message"]

    # 取消定住
    r2 = await client.post(
        f"/api/v1/plans/{record.id}/dishes/1/{dish['recipe_id']}/feedback", json={"op": "unlock"})
    assert r2.status_code == 200 and "取消定住" in r2.json()["message"]


async def test_feedback_on_missing_dish_is_404(api, client):
    _, _, record = api
    r = await client.post(f"/api/v1/plans/{record.id}/dishes/1/r999/feedback",
                          json={"op": "like"})
    assert r.status_code == 404
    assert r.json()["code"] == "dish_not_in_day"
    assert r.json()["message"].startswith("第 1 天没有这道菜")


# ---------------------------------------------------------------- 打分

async def test_rate_writes_profile_and_done_only(api, client):
    _, _, record = api
    before = await snapshot()
    dishes = (await client.get(f"/api/v1/plans/{record.id}/days/1")).json()["dishes"]

    r = await client.post(f"/api/v1/plans/{record.id}/rate", json={"day": 1, "score": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    after = await snapshot()

    assert after["dishes"] == before["dishes"], "打分不该动菜单"
    assert any(score == 2 for _, score in after["ratings"])
    assert after["state"][(record.id, 1)][2] is True          # 这天标记成做过了
    assert set(body["data"]["dishes"]) == {d["name"] for d in dishes}
    assert "好吃" in body["message"] and "菜单不变" in body["message"]
    assert (await client.get(f"/api/v1/plans/{record.id}")).json()["done_days"] == [1]


async def test_invalid_score_is_422(api, client):
    _, _, record = api
    r = await client.post(f"/api/v1/plans/{record.id}/rate", json={"day": 1, "score": 9})
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_request"


# ---------------------------------------------------------------- 买菜清单

async def test_checks_are_idempotent_and_report_progress(api, client):
    _, _, record = api
    detail = (await client.get(f"/api/v1/plans/{record.id}")).json()
    names = [i["name"] for i in detail["shopping"] if i["needed"]]

    r1 = await client.put(f"/api/v1/plans/{record.id}/shopping/checks",
                          json={"names": names[:2]})
    assert r1.status_code == 200, r1.text
    r2 = await client.put(f"/api/v1/plans/{record.id}/shopping/checks",
                          json={"names": names[:2]})
    assert r2.status_code == 200
    assert r2.json()["data"] == r1.json()["data"]              # 幂等：同样请求同样结果
    assert f"已记下买到 2/{len(names)} 样" in r1.json()["message"]

    got = (await client.get(f"/api/v1/plans/{record.id}/shopping/checks")).json()
    assert got["names"] == sorted(names[:2])
    assert got["total"] == len(names)
    assert set(got["remaining"]) == set(names[2:])

    # 全买齐的文案
    r3 = await client.put(f"/api/v1/plans/{record.id}/shopping/checks", json={"names": names})
    assert "全部买齐了" in r3.json()["message"]

    # 清单勾选不影响菜单
    assert (await client.get(f"/api/v1/plans/{record.id}")).json()["days"][0]["dishes"]


async def test_checks_reject_unknown_item(api, client):
    _, _, record = api
    r = await client.put(f"/api/v1/plans/{record.id}/shopping/checks",
                         json={"names": ["不存在的食材"]})
    assert r.status_code == 422
    assert r.json()["code"] == "unknown_item"


# ---------------------------------------------------------------- 档案

async def test_profile_patch_and_exact_undo(api, client):
    _, _, record = api
    before = await snapshot()

    r = await client.put("/api/v1/profile", json={"op": "dislike", "names": ["清炒时蔬"]})
    assert r.status_code == 200, r.text
    body = r.json()
    mid = await snapshot()

    assert "清炒时蔬" in body["data"]["disliked_dishes"]
    assert "本周菜单没动" in body["message"]
    assert mid["dishes"] == before["dishes"], "改档案不该动任何一周的菜单"

    # 精确撤销：清炒时蔬原来是"未表态"，所以要 remove（不是 like）
    assert body["undo_hint"]["body"]["op"] == "remove"
    r2 = await client.request(body["undo_hint"]["method"], body["undo_hint"]["path"],
                              json=body["undo_hint"]["body"])
    assert r2.status_code == 200
    assert (await snapshot())["prefs"] == before["prefs"]


async def test_profile_clear_needs_confirm(api, client):
    r = await client.post("/api/v1/profile/clear")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "confirm_required"
    assert "没法恢复" in body["message"]
    ops = [s["op"] for s in body["details"]["next_steps"]]
    assert "export_profile" in ops and "cancel" in ops

    # 档案没被清（被拦住了）
    assert (await client.get("/api/v1/profile")).json()["liked_dishes"]

    r2 = await client.post("/api/v1/profile/clear", params={"confirm": "true"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["undo_hint"] is None                  # 清空就是清空，不假装能撤销
    after = (await client.get("/api/v1/profile")).json()
    assert after["liked_dishes"] == [] and after["ratings"] == {}


async def test_profile_export(api, client):
    body = (await client.get("/api/v1/profile/export")).json()
    assert body["liked_dishes"] == ["番茄炒蛋"]
    assert body["ratings"]["清炒时蔬"]["score"] == 2


# ---------------------------------------------------------------- 隔离与审计

async def test_mutation_does_not_touch_the_other_plan(api, client, result_factory, start_date):
    """R1 不只在一周之内：改 A 方案不能碰到 B 方案。"""
    _, _, record = api
    other = await PlanRepo.save_plan(result_factory(days=2), "2026-09-21", "另一版",
                                     make_active=False)
    before = await snapshot()

    await client.patch(f"/api/v1/plans/{record.id}/days/2", json={"op": "skip"})
    after = await snapshot()

    assert changed_days(before, after) == {(record.id, 2)}
    assert after["dishes"][(other.id, 1)] == before["dishes"][(other.id, 1)]
    assert after["dishes"][(other.id, 2)] == before["dishes"][(other.id, 2)]
    assert start_date  # 夹具只是为了让这个用例与种数据用同一个起始周


async def test_action_log_records_every_change(api, client):
    _, _, record = api
    from recipe_planner.storage.repositories import LogRepo

    r = await client.patch(f"/api/v1/plans/{record.id}/days/1", json={"op": "skip"})
    log_id = r.json()["action_log_id"]
    assert log_id is not None

    logs = await LogRepo.recent(record.id, limit=5)
    assert logs and logs[0]["id"] == log_id
    assert logs[0]["kind"] == "skip"
    assert logs[0]["text"] == r.json()["message"]


@pytest.mark.parametrize("op,payload", [
    ("skip", {"op": "skip"}),
    ("faster", {"op": "faster"}),
    ("people", {"op": "people", "people": 4}),
])
async def test_receipts_read_like_human(api, client, op, payload):
    """回执必须是 05 §5.3 的句式，并说清"哪里没动"。"""
    _, _, record = api
    r = await client.patch(f"/api/v1/plans/{record.id}/days/2", json=payload)
    if r.status_code == 409:                       # faster 可能已经是最快的
        assert r.json()["details"]["next_steps"], "失败也必须给可点击的补救项"
        return
    assert r.status_code == 200, r.text
    message = r.json()["message"]
    assert RECEIPT.match(message), message
    assert NO_TOUCH.search(message), message
    for leak in ("None", "Traceback", "dict", "recipe_planner"):
        assert leak not in message, f"回执里出现工程词：{leak}"
