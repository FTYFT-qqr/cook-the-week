"""接口层：一天多顿时「哪一顿」必须一路传对（docs/10）。

这一组用例是被一个真 bug 逼出来的：服务端详情接口用 `next(p for p in days if p.day == day)`
取当天计划，于是**早/午/晚三餐显示的是同一份菜**（永远是早餐那一份），而每天的用时、金额
却各算各的 —— 界面看着完全正常，只是全错。用户的原话："早中晚三餐显示的都是一样的"。

所以这里不测"接口返回 200"，测的是**三餐的菜真的不一样**，以及每一次单点改动
只落在点名的那一顿上。
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from conftest import RECIPES, START

from recipe_planner.models import (ChosenDish, DayPlan, PlanResult, Recipe, RecipeDB,
                                   ShoppingItem, UserConstraints)
from recipe_planner.storage.repositories import PlanRepo, RecipeRepo

MEALS3 = ["早餐", "午餐", "晚餐"]
# 三顿吃**完全不同**的菜：哪一处把餐次丢了，立刻就能看出来
BY_MEAL = {"早餐": ["r1", "r2"], "午餐": ["r3"], "晚餐": ["r4", "r5"]}

# 换菜要有**空着的**候选，否则"这一周没有能替换的菜了"会盖掉真正要测的东西
EXTRA = [
    Recipe(id="r6", name="麻婆豆腐", category="热菜", difficulty="简单", time_min=20,
           cost_yuan=9.0, taste_tags=["麻辣"], goal_tags=["高蛋白"],
           ingredients=[{"name": "豆腐", "amount": "1块", "category": "豆制品"}]),
    Recipe(id="r7", name="冬瓜汤", category="汤", difficulty="简单", time_min=15,
           cost_yuan=4.0, taste_tags=["清淡"], goal_tags=["清淡"],
           ingredients=[{"name": "冬瓜", "amount": "300克", "category": "蔬菜"}]),
    Recipe(id="r8", name="蒜蓉西兰花", category="热菜", difficulty="简单", time_min=12,
           cost_yuan=7.0, taste_tags=["清淡"], goal_tags=["减脂"],
           ingredients=[{"name": "西兰花", "amount": "1颗", "category": "蔬菜"}]),
    # 早餐池要有一道备用的：类别=早餐（`in_meal_pool` 认类别也认"含蛋"的食材）
    Recipe(id="r9", name="蒸蛋羹", category="早餐", difficulty="简单", time_min=10,
           cost_yuan=4.0, taste_tags=["清淡"], goal_tags=["高蛋白"],
           ingredients=[{"name": "鸡蛋", "amount": "2个", "category": "肉蛋"}]),
]


def three_meal_result(days: int = 2) -> PlanResult:
    """一天三顿、每顿菜品互不相同的种子方案。"""
    c = UserConstraints(people=2, days=days, dishes_per_day=2, cook_start="18:30",
                        meals=list(MEALS3),
                        dishes_per_meal={"早餐": 2, "午餐": 1, "晚餐": 2})
    plans = []
    for day in range(1, days + 1):
        for meal in MEALS3:
            plans.append(DayPlan(day=day, meal=meal,
                                 dishes=[ChosenDish(recipe_id=rid, reason=f"{meal}的安排")
                                         for rid in BY_MEAL[meal]]))
    return PlanResult(
        constraints=c, candidate_count=len(RECIPES), days=plans,        shopping=[ShoppingItem(name="番茄", category="蔬菜", amount="4个",
                               for_recipes=["番茄炒蛋"])],
        estimated_cost_yuan=60.0)


@pytest_asyncio.fixture()
async def meal_api(api):
    """(client, plan_id) —— 库里多一份"一天三顿"的方案。"""
    client, _app, _record = api
    await RecipeRepo.upsert_many(EXTRA)
    record = await PlanRepo.save_plan(three_meal_result(), START, "三餐版")
    return client, record.id


async def _detail(client, plan_id: str) -> dict:
    resp = await client.get(f"/api/v1/plans/{plan_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _slots(detail: dict) -> dict:
    return {(d["day"], d["meal"]): [x["recipe_id"] for x in d["dishes"]]
            for d in detail["days"]}


@pytest.mark.asyncio
async def test_详情里三餐的菜各不相同(meal_api):
    """**用户报的那个 bug**：早/午/晚必须是三份不同的菜。"""
    client, plan_id = meal_api
    detail = await _detail(client, plan_id)
    slots = _slots(detail)

    assert len(detail["days"]) == 6, "2 天 × 3 顿 = 6 条"
    for day in (1, 2):
        assert slots[(day, "早餐")] == BY_MEAL["早餐"]
        assert slots[(day, "午餐")] == BY_MEAL["午餐"]
        assert slots[(day, "晚餐")] == BY_MEAL["晚餐"]
        got = [tuple(slots[(day, m)]) for m in MEALS3]
        assert len(set(got)) == 3, f"第 {day} 天的早/午/晚显示成了同一份菜：{got}"


@pytest.mark.asyncio
async def test_详情里每顿自带餐次和各自的用时金额(meal_api):
    client, plan_id = meal_api
    detail = await _detail(client, plan_id)
    rows = [d for d in detail["days"] if d["day"] == 1]
    assert [d["meal"] for d in rows] == MEALS3
    # 每顿的用时/金额必须按**自己那几道菜**算：早餐 15+10=25，午餐 55，晚餐 12+8=20
    assert {d["meal"]: d["minutes"] for d in rows}["早餐"] == 25
    assert {d["meal"]: d["minutes"] for d in rows}["午餐"] == 55
    assert len({d["cost"] for d in rows}) == 3, "三顿的金额不该一样（不一样才说明各算各的）"


@pytest.mark.asyncio
async def test_单日接口要点名哪一顿(meal_api):
    client, plan_id = meal_api
    last = (await client.get(f"/api/v1/plans/{plan_id}/days/1")).json()
    assert last["meal"] == "晚餐" and [x["recipe_id"] for x in last["dishes"]] == BY_MEAL["晚餐"]

    brunch = (await client.get(f"/api/v1/plans/{plan_id}/days/1",
                              params={"meal": "早餐"})).json()
    assert brunch["meal"] == "早餐"
    assert [x["recipe_id"] for x in brunch["dishes"]] == BY_MEAL["早餐"]


@pytest.mark.asyncio
async def test_标记做完只记那一顿(meal_api):
    client, plan_id = meal_api
    resp = await client.patch(f"/api/v1/plans/{plan_id}/days/1",
                              json={"op": "done", "done": True, "meal": "早餐"})
    assert resp.status_code == 200, resp.text

    detail = await _detail(client, plan_id)
    done = {(d["day"], d["meal"]) for d in detail["days"] if d["done"]}
    assert done == {(1, "早餐")}, f"只有早餐该是「做完了」，实际：{done}"
    # 客户端要靠 done_slots 还原"哪一顿做完了"，接口必须带上
    assert "1|早餐" in detail["done_slots"]
    assert detail["done_days"] == []


@pytest.mark.asyncio
async def test_不做饭只影响那一顿(meal_api):
    client, plan_id = meal_api
    resp = await client.patch(f"/api/v1/plans/{plan_id}/days/1",
                              json={"op": "skip", "meal": "午餐"})
    assert resp.status_code == 200, resp.text

    slots = _slots(await _detail(client, plan_id))
    assert slots[(1, "午餐")] == []
    assert slots[(1, "早餐")] == BY_MEAL["早餐"], "只不吃午饭，早饭还得吃"
    assert slots[(1, "晚餐")] == BY_MEAL["晚餐"]


@pytest.mark.asyncio
async def test_换一道只换点名那一顿(meal_api):
    client, plan_id = meal_api
    # 晚餐的 r4 换成别的菜；早餐/午餐一道都不许动
    resp = await client.patch(f"/api/v1/plans/{plan_id}/days/1",
                              json={"op": "swap", "recipe_id": "r4", "meal": "晚餐"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["meal"] == "晚餐"

    slots = _slots(await _detail(client, plan_id))
    assert "r4" not in slots[(1, "晚餐")], "晚餐那道该被换掉了"
    assert slots[(1, "早餐")] == BY_MEAL["早餐"]
    assert slots[(1, "午餐")] == BY_MEAL["午餐"]


@pytest.mark.asyncio
async def test_换菜候选取自同一顿的池子(meal_api):
    """早餐那道换成别的菜时，不能换成只有正餐才用的硬菜（r3 是 55 分钟的红烧排骨）。"""
    client, plan_id = meal_api
    resp = await client.patch(f"/api/v1/plans/{plan_id}/days/1",
                              json={"op": "swap", "recipe_id": "r1", "meal": "早餐"})
    assert resp.status_code == 200, resp.text
    slots = _slots(await _detail(client, plan_id))
    assert "r3" not in slots[(1, "早餐")], "早餐不该排上 55 分钟的红烧排骨"


@pytest.mark.asyncio
async def test_打分只记那一顿的菜(meal_api):
    client, plan_id = meal_api
    resp = await client.post(f"/api/v1/plans/{plan_id}/rate",
                             json={"day": 1, "score": 2, "meal": "午餐"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["meal"] == "午餐"
    assert body["data"]["dishes"] == ["红烧排骨"], "只该给午餐那道打分"

    detail = await _detail(client, plan_id)
    done = {(d["day"], d["meal"]) for d in detail["days"] if d["done"]}
    assert done == {(1, "午餐")}


@pytest.mark.asyncio
async def test_反馈要带餐次才找得到那道菜(meal_api):
    """「定住」早餐的菜：以前按天取会拿到早餐那一顿的菜，多餐时就认错/认不出。"""
    client, plan_id = meal_api
    resp = await client.post(f"/api/v1/plans/{plan_id}/dishes/1/r1/feedback",
                             json={"op": "lock", "meal": "早餐"})
    assert resp.status_code == 200, resp.text
    detail = await _detail(client, plan_id)
    assert "r1" in detail["constraints"]["must_include_recipes"]


@pytest.mark.asyncio
async def test_今天页能按顿问且带回餐次(meal_api):
    """界面「今天」页一天问三顿：服务端必须接受 meal，并且**把 meal 带回来**。

    带不回来（DTO 少一个字段）就回到老毛病：界面以为自己在看晚餐，
    "做完了 / 来客人了"全落到晚餐上。
    """
    client, plan_id = meal_api
    r = await client.get(f"/api/v1/plans/{plan_id}/tonight", params={"day": 1, "meal": "午餐"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meal"] == "午餐" and body["day"] == 1
    assert body["headline"] == "红烧排骨", body["headline"]

    last = (await client.get(f"/api/v1/plans/{plan_id}/tonight", params={"day": 1})).json()
    assert last["meal"] == "晚餐", "不给 meal 就是当天最后一顿"
    assert last["headline"] == "紫菜蛋花汤、凉拌黄瓜", last["headline"]

    am = (await client.get(f"/api/v1/plans/{plan_id}/tonight",
                           params={"day": 1, "meal": "早餐"})).json()
    assert am["meal"] == "早餐" and am["headline"] == "番茄炒蛋、清炒时蔬", am["headline"]


@pytest.mark.asyncio
async def test_今天页认得出做完了的是哪一顿(meal_api):
    """早餐标记做完了：「今天」页看早餐是 done，看晚餐还是 planned。"""
    client, plan_id = meal_api
    await client.patch(f"/api/v1/plans/{plan_id}/days/1",
                       json={"op": "done", "done": True, "meal": "早餐"})
    am = (await client.get(f"/api/v1/plans/{plan_id}/tonight",
                           params={"day": 1, "meal": "早餐"})).json()
    pm = (await client.get(f"/api/v1/plans/{plan_id}/tonight",
                           params={"day": 1, "meal": "晚餐"})).json()
    assert am["state"] == "done", am["state"]
    assert pm["state"] == "planned", pm["state"]


@pytest.mark.asyncio
async def test_单餐时接口形状与以前一致(api):
    """只做晚餐的老方案：不带 meal 也能正常读（老客户端不用改）。"""
    client, _app, record = api
    detail = await _detail(client, record.id)
    assert [d["meal"] for d in detail["days"]][:1] == ["晚餐"]
    one = (await client.get(f"/api/v1/plans/{record.id}/days/1")).json()
    assert one["meal"] == "晚餐" and one["dishes"], "不给 meal 就是当天那一顿"
