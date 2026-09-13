"""P1-2 验收：只读接口（docs/09 P1-2 / docs/08 §6）。

重点不是"能不能返回 200"，而是**契约**：今晚页要的字段必须都在 `/plans/current` 里，
而且数字要带口径（05 §4 第 7 条：数字永远带口径）。
"""
from __future__ import annotations

import httpx
import pytest

from recipe_planner.api.main import create_app
from recipe_planner.storage.engine import get_engine, reset_engine
from recipe_planner.storage.repositories import PlanRepo

# ---------------------------------------------------------------- 存活/就绪

async def test_health_does_not_touch_dependencies(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_ready_reports_each_dependency(client):
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    names = {c["name"]: c for c in body["checks"]}
    assert set(names) == {"db", "redis", "llm", "auth"}       # auth 检查是 P1-4 加的
    assert names["db"]["ok"] is True
    # 没配 Redis / 没配密钥都不算"没就绪"，但要如实说明（否则排障时看不见）
    assert names["redis"]["ok"] is True and "内存" in names["redis"]["detail"]
    assert names["llm"]["ok"] is True and "兜底" in names["llm"]["detail"]
    assert names["auth"]["ok"] is True


async def test_ready_is_degraded_when_db_unreachable(monkeypatch, api):
    """DB 打不开 → 503 且明确说是 db 不行；Redis/LLM 不受影响。"""
    # 指向一个目录：SQLite 一定打不开
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///D:/cook/recipe-planner/.tmp")
    reset_engine()
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "degraded"
        db_check = next(c for c in body["checks"] if c["name"] == "db")
        assert db_check["ok"] is False and "连不上数据库" in db_check["detail"]
    finally:
        reset_engine()


# ---------------------------------------------------------------- 菜谱

async def test_recipes_list_and_filters(client):
    r = await client.get("/api/v1/recipes")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5 and len(body["items"]) == 5
    first = body["items"][0]
    for key in ("id", "name", "category", "difficulty", "time_min", "cost_yuan",
                "taste_tags", "allergens", "ingredients", "liked", "disliked"):
        assert key in first, key
    assert first["ingredients"] and first["ingredients"][0]["name"]

    # 顺序不保证，按菜名对号入座
    by_name = {i["name"]: i for i in body["items"]}
    assert by_name["番茄炒蛋"]["liked"] is True        # 档案里喜欢
    assert by_name["红烧排骨"]["disliked"] is True     # 档案里不喜欢
    assert by_name["清炒时蔬"]["liked"] is False and by_name["清炒时蔬"]["disliked"] is False


async def test_recipes_query_and_category(client):
    assert (await client.get("/api/v1/recipes", params={"q": "排骨"})).json()["total"] == 1
    assert (await client.get("/api/v1/recipes", params={"q": "清淡"})).json()["total"] == 2

    by_cat = (await client.get("/api/v1/recipes", params={"category": "汤"})).json()
    assert by_cat["total"] == 1 and by_cat["items"][0]["name"] == "紫菜蛋花汤"


async def test_recipes_liked_filter_and_pagination(client):
    liked = (await client.get("/api/v1/recipes", params={"liked": "true"})).json()
    assert liked["total"] == 1 and liked["items"][0]["name"] == "番茄炒蛋"

    not_liked = (await client.get("/api/v1/recipes", params={"liked": "false"})).json()
    assert not_liked["total"] == 4

    page1 = (await client.get("/api/v1/recipes", params={"limit": 2})).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"] == 2
    page2 = (await client.get("/api/v1/recipes",
                              params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    assert len(page2["items"]) == 2
    assert {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]} == set()
    page3 = (await client.get("/api/v1/recipes", params={"limit": 2, "cursor": 4})).json()
    assert len(page3["items"]) == 1 and page3["next_cursor"] is None


# ---------------------------------------------------------------- 方案列表

async def test_plans_list_marks_current(api, client):
    _, _, record = api
    body = (await client.get("/api/v1/plans")).json()
    assert body["total"] == 1
    assert body["current_id"] == record.id
    item = body["items"][0]
    assert item["id"] == record.id
    assert item["is_current"] is True
    assert item["days"] == 3 and item["dishes"] == 6
    assert item["total_cost"] > 0
    assert item["created_at"] and item["start_date"] == "2026-09-14"


# ---------------------------------------------------------------- 今晚

TONIGHT_KEYS = {"state", "plan_id", "kicker", "headline", "meta", "reason", "day", "weekday",
                "date_label", "week_label", "dishes", "minutes", "cost", "people", "eat_eta",
                "hint", "next_steps"}


async def test_current_plan_has_everything_the_tonight_page_needs(client):
    r = await client.get("/api/v1/plans/current")
    assert r.status_code == 200
    body = r.json()
    assert TONIGHT_KEYS <= set(body), TONIGHT_KEYS - set(body)
    assert body["state"] in {"planned", "week_over", "skipped", "done"}
    assert body["headline"] and "、" in body["headline"]
    # 数字带口径：分钟 + 金额（"按 N 人算"只在"来客人了"那天才出现，与界面一致）
    assert "分钟" in body["meta"] and "¥" in body["meta"]
    assert body["people"] is None
    # 18:30 开始做 → 要给"几点能吃上"（B2）
    assert body["eat_eta"].startswith("18:30 开始做") and "能吃上" in body["eat_eta"]
    # 每道菜都要能单独点"这道不吃"，所以要有 recipe_id / 耗时 / 难度
    assert body["dishes"], "今晚没有菜"
    for dish in body["dishes"]:
        assert dish["recipe_id"] and dish["name"] and dish["time_min"] > 0 and dish["difficulty"]
    assert body["reason"], "缺一句推荐理由"
    ops = {s["op"] for s in body["next_steps"]}
    assert {"start_cooking", "faster", "guests", "mark_done"} <= ops


async def test_current_plan_returns_no_plan_state_not_404(api, client):
    """没有方案是"状态④"，不是错误 —— 前端要拿它渲染引导卡，所以必须 200。"""
    _, _, record = api
    await PlanRepo.delete_record(record.id)

    r = await client.get("/api/v1/plans/current")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "no_plan"
    assert body["plan_id"] is None
    assert body["next_steps"][0]["op"] == "create_plan"
    assert "排一周" in body["next_steps"][0]["label"]


# ---------------------------------------------------------------- 方案详情

async def test_plan_detail_days_dishes_shopping_summary(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == record.id and body["is_current"] is True
    assert body["change_note"] == "第一次排的"
    assert body["constraints"]["people"] == 2
    assert body["constraints"]["cook_start"] == "18:30"

    assert len(body["days"]) == 3
    day1 = body["days"][0]
    assert day1["day"] == 1 and day1["weekday"] == "周一" and day1["date_label"] == "9/14"
    assert len(day1["dishes"]) == 2 and day1["minutes"] > 0 and day1["cost"] > 0
    assert day1["done"] is False and day1["skipped"] is False

    # 摘要里的总花费要和逐天加起来一致（同一条口径，不能两个数）
    assert abs(sum(d["cost"] for d in body["days"]) - body["summary"]["total_cost"]) < 0.01
    assert body["summary"]["structure"], "缺结构统计（几荤几素）"
    assert body["summary"]["days"] == 3 and body["summary"]["dishes"] == 6

    shopping = {i["name"]: i for i in body["shopping"]}
    assert shopping["番茄"]["category"] == "蔬菜"
    assert shopping["番茄"]["for_recipes"] == ["番茄炒蛋"]
    assert shopping["番茄"]["batch"] == 2          # 蔬菜属于"周中再买"那批
    assert shopping["番茄"]["checked"] is False
    assert all(i["batch"] in (1, 2, None) for i in body["shopping"])


async def test_plan_detail_reflects_checks_and_done(client, api):
    _, _, record = api
    await PlanRepo.set_checked(record.id, ["番茄", "青菜"])
    await PlanRepo.set_done(record.id, 1, True)

    body = (await client.get(f"/api/v1/plans/{record.id}")).json()
    checked = {i["name"] for i in body["shopping"] if i["checked"]}
    assert checked == {"番茄", "青菜"}
    assert body["checked_items"] == sorted(["番茄", "青菜"])
    assert body["done_days"] == [1]
    assert body["days"][0]["done"] is True


async def test_plan_detail_404_is_problem_json(client):
    r = await client.get("/api/v1/plans/deadbeef")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "plan_not_found"
    assert body["details"]["next_steps"][0]["op"] == "list_plans"


# ---------------------------------------------------------------- 单天

async def test_day_detail_includes_cook_order(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}/days/1")
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == 1
    assert body["cook_order"], "缺下锅顺序"
    assert all("分钟" in line for line in body["cook_order"])
    assert isinstance(body["has_parallel"], bool)


async def test_day_detail_out_of_range_is_404(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}/days/9")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert "这一天" in r.json()["message"]


# ---------------------------------------------------------------- 档案

async def test_profile_round_trip(client):
    body = (await client.get("/api/v1/profile")).json()
    assert body["liked_dishes"] == ["番茄炒蛋"]
    assert body["disliked_dishes"] == ["红烧排骨"]
    assert body["ratings"]["清炒时蔬"]["score"] == 2
    assert body["history"][0]["name"] == "番茄炒蛋"
    assert body["history"][0]["source"] == "菜单页"
    # 指纹 = 唯一算法算出来的那个（与界面用的是同一个函数）
    from recipe_planner.profile import profile_signature_of

    assert body["signature"] == profile_signature_of({
        "liked_dishes": body["liked_dishes"], "disliked_dishes": body["disliked_dishes"]})


# ---------------------------------------------------------------- OpenAPI

def test_openapi_is_available():
    spec = create_app().openapi()
    assert spec["info"]["title"] == "Recipe Planner API"
    paths = set(spec["paths"])
    assert {"/health", "/ready", "/api/v1/recipes", "/api/v1/plans",
            "/api/v1/plans/current", "/api/v1/plans/{plan_id}",
            "/api/v1/plans/{plan_id}/days/{day}", "/api/v1/profile"} <= paths


@pytest.mark.parametrize("path", ["/health", "/ready", "/api/v1/recipes", "/api/v1/plans",
                                  "/api/v1/plans/current", "/api/v1/profile"])
async def test_read_routes_never_leak_internal_names(client, path):
    """对外文案里不出现 Python 内部名字（05 §5.2 工程词替换）。"""
    r = await client.get(path)
    text = r.text
    for leak in ("Traceback", "sqlalchemy", "aiosqlite", "recipe_planner.", "KeyError"):
        assert leak not in text, f"{path} 泄漏了内部字样：{leak}"
