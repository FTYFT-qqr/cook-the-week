"""P1-5 验收：排菜任务化（docs/09 P1-5 / docs/08 §6）。

一次排菜要调模型 + 校验修正，几秒到几十秒。做成同步请求会把连接吊住，客户端一刷新就不知道
到底排没排 —— 所以：**立刻回 202 + job_id**，状态写进 `job` 表，客户端轮询。

用假图精确控制"跑多久 / 在哪失败 / 有没有结果"，不碰真实 LLM：
- 状态机：queued → running → succeeded
- 取消：运行中（下一个节点停）与排队中（根本不开跑）
- 超时：failed(timeout) + 可点击下一步
- 失败：failed 且 `message` 是人话（不泄漏异常类名）
- 降级：没有密钥时真图会走确定性兜底，任务照样成功
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from recipe_planner.api.main import create_app
from recipe_planner.api.worker import InProcessRunner, dump_error, load_error
from recipe_planner.progress import STAGE_LABELS
from recipe_planner.storage.repositories import JobRepo

# tests/api 不是包（没有 __init__.py），pytest 会把本目录放进 sys.path，
# 所以这里能直接 import conftest；相对导入 `.conftest` 会失败。
from conftest import fake_graph_factory, wait_job  # noqa: E402

PROBLEM = "application/problem+json"


async def _client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------- 提交

async def test_post_plans_returns_202_with_job_id(api, client, fast_runner):
    r = await client.post("/api/v1/plans", json={"people": 2, "days": 3})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job_id"] and body["status"] == "queued"
    assert body["poll_path"] == f"/api/v1/jobs/{body['job_id']}"
    assert body["queue_position"] == 1
    assert "正在排这一周" in body["message"]
    assert body["timeout_sec"] >= 5
    assert {s["op"] for s in body["next_steps"]} == {"poll_job", "cancel_job"}

    done = await wait_job(client, body["job_id"])
    assert done["status"] == "succeeded" and done["plan_id"]
    assert done["progress"] == 1.0
    # 排出来的方案真的在库里、并且成了"当前方案"
    assert (await client.get(f"/api/v1/plans/{done['plan_id']}")).status_code == 200
    current = (await client.get("/api/v1/plans/current")).json()
    assert current["plan_id"] == done["plan_id"]
    assert (await client.get("/api/v1/jobs")).json()[0]["id"] == body["job_id"]


async def test_job_reports_stages_while_running(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(delay=0.25))
    r = await client.post("/api/v1/plans", json={"days": 3})
    job_id = r.json()["job_id"]

    seen_stages, seen_progress = set(), []
    for _ in range(40):
        body = (await client.get(f"/api/v1/jobs/{job_id}")).json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            break
        if body["status"] == "running":
            seen_stages.add(body["stage"])
            seen_progress.append(body["progress"])
        await asyncio.sleep(0.05)

    assert seen_stages, "运行期间应该能看到阶段"
    assert seen_stages <= set(STAGE_LABELS), seen_stages
    assert all(0 < p <= 1 for p in seen_progress)
    assert seen_progress == sorted(seen_progress), "进度只能往前走"
    assert (await wait_job(client, job_id))["status"] == "succeeded"


async def test_unknown_allergen_is_rejected_not_ignored(api, client, fast_runner):
    """忌口写错了必须报错，**绝不能当成"没有这个忌口"放过去**（05 §4：忌口永不妥协）。"""
    r = await client.post("/api/v1/plans", json={"allergens": ["花生酱"]})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_request"
    assert "花生酱" in body["message"] and "不认" in body["message"]
    assert "花生" in body["details"]["next_steps"][0]["label"]
    assert (await client.get("/api/v1/jobs")).json() == [], "被拒的请求不该留下任务"


async def test_queue_position_when_something_is_already_running(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(delay=0.3))
    first = (await client.post("/api/v1/plans", json={"days": 3})).json()
    second = (await client.post("/api/v1/plans", json={"days": 3})).json()
    assert second["queue_position"] == 2
    assert "前面还有 1 个任务" in second["message"]
    assert (await wait_job(client, first["job_id"]))["status"] == "succeeded"
    assert (await wait_job(client, second["job_id"]))["status"] == "succeeded"


# ---------------------------------------------------------------- 取消

async def test_cancel_running_job(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(delay=0.25))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]

    for _ in range(40):                      # 等它真的跑起来
        if (await client.get(f"/api/v1/jobs/{job_id}")).json()["status"] == "running":
            break
        await asyncio.sleep(0.05)

    r = await client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    body = await wait_job(client, job_id)
    assert body["status"] == "cancelled"
    assert body["error_code"] == "cancelled"
    assert "已经停了" in body["message"] and body["next_steps"]
    assert body["plan_id"] is None, "取消掉的任务不该留下方案"


async def test_cancel_queued_job_never_starts(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(delay=0.3))
    first = (await client.post("/api/v1/plans", json={"days": 3})).json()
    second = (await client.post("/api/v1/plans", json={"days": 3})).json()

    r = await client.post(f"/api/v1/jobs/{second['job_id']}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"          # 排队中的立刻取消，不用等
    assert (await wait_job(client, first["job_id"]))["status"] == "succeeded"
    # 等一会儿，确认第二个任务确实没偷偷跑
    await asyncio.sleep(0.3)
    assert (await client.get(f"/api/v1/jobs/{second['job_id']}")).json()["status"] == "cancelled"
    plans = (await client.get("/api/v1/plans")).json()
    assert plans["total"] == 2, "只该多出第一个任务排的那一份方案"


async def test_cancel_finished_job_is_conflict(api, client, fast_runner):
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]
    await wait_job(client, job_id)
    r = await client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 409
    assert "已经排好了" in r.json()["message"]
    assert r.json()["details"]["next_steps"]


async def test_cancel_unknown_job_is_404(client):
    r = await client.post("/api/v1/jobs/nope/cancel")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


# ---------------------------------------------------------------- 超时与失败

async def test_timeout_marks_failed_timeout(api, client, runner_factory):
    runner_factory(timeout_sec=1, graph_factory=fake_graph_factory(delay=0.4))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]

    body = await wait_job(client, job_id, timeout=20)
    assert body["status"] == "failed"
    assert body["error_code"] == "timeout"
    assert "排得太久" in body["message"]
    assert {s["op"] for s in body["next_steps"]} == {"reduce_days", "relax_time"}
    assert body["plan_id"] is None


async def test_pipeline_failure_is_human_readable(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(raise_at="validate"))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]

    body = await wait_job(client, job_id)
    assert body["status"] == "failed"
    assert body["error_code"] == "error"
    text = json.dumps(body, ensure_ascii=False)
    for leak in ("RuntimeError", "Traceback", "假图故意失败", "recipe_planner"):
        assert leak not in text, f"任务状态里泄漏了内部信息：{leak}"
    assert body["next_steps"][0]["op"] == "retry"


async def test_empty_result_is_reported(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(empty=True))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]
    body = await wait_job(client, job_id)
    assert body["status"] == "failed" and body["error_code"] == "empty"
    assert "不够" in body["message"]


# ---------------------------------------------------------------- 降级与落库

async def test_real_graph_degrades_without_api_key(api, client, runner_factory):
    """没有密钥时真图走确定性兜底：任务照样成功（05 §5.1：失败也不能把用户甩在原地）。"""
    runner_factory()                                  # 不给假图 → 用真实 build_graph
    job_id = (await client.post("/api/v1/plans", json={"people": 2, "days": 3})).json()["job_id"]
    body = await wait_job(client, job_id, timeout=60)
    assert body["status"] == "succeeded", body
    detail = (await client.get(f"/api/v1/plans/{body['plan_id']}")).json()
    assert len(detail["days"]) == 3
    assert all(d["dishes"] for d in detail["days"])


async def test_must_include_recipe_really_makes_it_into_the_plan(api, client, runner_factory):
    """「定住某道菜」必须真的生效：需求里的 must_include_recipes 要经任务落进方案里。

    这条走**真流水线**（显式传 `graph_factory=None`）：假图不看约束，用它只能测出
    "字符串传过去了"。没有密钥时真图走确定性兜底，不碰网络。
    """
    runner_factory(graph_factory=None)                # 显式不给假图 → 用真实 build_graph
    r = await client.post("/api/v1/plans", json={"people": 2, "days": 3,
                                                 "must_include_recipes": ["r5"]})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    body = await wait_job(client, job_id, timeout=60)
    assert body["status"] == "succeeded", body

    row = await JobRepo.get(job_id)                   # 需求原样留在库里（排障要用）
    assert row["request"]["must_include_recipes"] == ["r5"]

    detail = (await client.get(f"/api/v1/plans/{body['plan_id']}")).json()
    ids = [d["recipe_id"] for day in detail["days"] for d in day["dishes"]]
    assert "r5" in ids, "定住的菜没进方案：这个字段在服务端没生效"
    # 而且"定住"这个状态也要能被界面读到（否则界面显示不出"已定住"）
    assert detail["constraints"]["must_include_recipes"] == ["r5"]
    assert "r5" in {d["recipe_id"] for day in detail["days"]
                    for d in day["dishes"] if d["locked"]}


async def test_job_row_persists_state_in_db(api, client, fast_runner):
    job_id = (await client.post("/api/v1/plans", json={"days": 2})).json()["job_id"]
    await wait_job(client, job_id)

    row = await JobRepo.get(job_id)                   # 直接查库，不经过接口
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["plan_id"]
    assert row["started_at"] and row["finished_at"]
    # 请求参数也留在库里（排障时能看出当时填了什么）
    assert row["request"]["days"] == 2


async def test_job_error_column_is_json():
    """`job.error` 是 Text，里面存 JSON —— 存取要能往返。"""
    raw = dump_error("timeout", "人话说明", [{"op": "retry", "label": "再试一次"}])
    assert load_error(raw) == {"code": "timeout", "message": "人话说明",
                               "next_steps": [{"op": "retry", "label": "再试一次"}]}
    assert load_error(None) is None
    assert load_error("老的纯文本错误")["code"] == "failed"       # 兼容纯文本
    assert load_error("老的纯文本错误")["message"] == "老的纯文本错误"


# ---------------------------------------------------------------- 幂等与限流

async def test_same_idempotency_key_creates_one_job(api, client, fast_runner):
    headers = {"Idempotency-Key": "plan-once"}
    r1 = await client.post("/api/v1/plans", json={"days": 3}, headers=headers)
    r2 = await client.post("/api/v1/plans", json={"days": 3}, headers=headers)
    assert r1.status_code == r2.status_code == 202
    assert r2.headers.get("idempotent-replay") == "true"
    assert r2.json()["job_id"] == r1.json()["job_id"]
    await wait_job(client, r1.json()["job_id"])
    assert len((await client.get("/api/v1/jobs")).json()) == 1, "同一个 key 只该有一个任务"


async def test_plans_post_is_rate_limited(api, fast_runner, monkeypatch):
    """排菜要花钱调模型，所以单独 6/分钟（docs/08 §5 中间件 4）。"""
    monkeypatch.setenv("RATE_LIMIT_PLAN_PER_MIN", "2")
    async with await _client(create_app()) as c:
        assert (await c.post("/api/v1/plans", json={})).status_code == 202
        assert (await c.post("/api/v1/plans", json={})).status_code == 202
        third = await c.post("/api/v1/plans", json={})
    assert third.status_code == 429
    assert third.json()["details"]["bucket"] == "expensive"


async def test_runner_shutdown_is_clean(api):
    """执行器关掉之后不该留下活着的线程，而且还能重新用（热重启/测试都会这么干）。"""
    runner = InProcessRunner(graph_factory=fake_graph_factory())
    runner.submit("job_x")                    # 起工作线程
    assert runner._workers and runner._workers[0].is_alive()
    workers = list(runner._workers)
    runner.shutdown()
    assert runner._workers == []
    assert all(not w.is_alive() for w in workers)

    # 关掉之后还能再提交：新的工作线程要真的活着（不能有"哨兵"留在队列里把它弄死）
    runner.submit("job_y")
    assert any(w.is_alive() for w in runner._workers)
    runner.shutdown()


@pytest.mark.parametrize("stage", ["queued", "retrieve", "plan", "validate", "shopping", "answer"])
def test_stage_progress_is_monotonic(stage):
    from recipe_planner.progress import STAGE_PROGRESS

    ordered = ["queued", "retrieve", "plan", "validate", "repair", "shopping", "answer"]
    values = [STAGE_PROGRESS[s] for s in ordered]
    assert values == sorted(values), "阶段进度必须单调递增"
    assert stage in STAGE_PROGRESS and 0 < STAGE_PROGRESS[stage] <= 1
