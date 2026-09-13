"""P1-6 验收（1/2）：SSE 阶段进度（docs/08 §6、docs/09 P1-6）。

事件契约（docs/08 §6 原文）：
    event: stage   data: {"stage":"plan","label":"正在搭配这一周的菜…","progress":0.45}
    event: done    data: {"plan_id":"…","week_start":"…"}
    event: error   data: {"code":"…","message":"…","next_steps":[…]}

取消也走 `event: error`（`code: "cancelled"`）—— docs/08 只定义了这三种事件，
不为取消单开一种，客户端看 `code` 即可。
"""
from __future__ import annotations

import asyncio
import json
import re

from recipe_planner.progress import STAGE_LABELS

from conftest import fake_graph_factory

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 [(event, data), …]（注释行 `: ping` 会被忽略）。"""
    events: list[tuple[str, dict]] = []
    current: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            current["data"] = json.loads(line[5:].strip())
        if "event" in current and "data" in current:
            events.append((current["event"], current["data"]))
            current = {}
    return events


# ---------------------------------------------------------------- 成功路径

ORDER = ["queued", "retrieve", "plan", "validate", "repair", "shopping", "answer"]


async def test_sse_streams_stages_then_done(api, client, runner_factory):
    # 假图慢一点（每节点 0.4s），才可能真的观察到"边跑边推"而不是只剩终态
    runner_factory(graph_factory=fake_graph_factory(delay=0.4))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]

    r = await client.get(f"/api/v1/jobs/{job_id}/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"

    events = parse_sse(r.text)
    names = [e for e, _ in events]
    assert names[0] == "stage", f"第一条应该是 stage：{events[:2]}"
    assert names[-1] == "done", f"最后一条应该是 done：{events[-3:]}"
    assert set(names) <= {"stage", "done", "error"}

    stages = [d["stage"] for e, d in events if e == "stage"]
    assert len(stages) >= 3, f"应该能边跑边推好几条，实际只有 {stages}"
    assert all(s in STAGE_LABELS for s in stages), stages
    # 阶段顺序必须是规范顺序的**子序列**（不能往回跳；中间漏掉几个是正常的，
    # 因为连接可能比任务起步晚，或者某一阶段在两次轮询之间就跑完了）
    indexes = [ORDER.index(s) for s in stages]
    assert indexes == sorted(indexes), f"阶段顺序乱了：{stages}"
    assert stages[-1] == "answer", "done 之前应该先到 answer"

    progresses = [d["progress"] for e, d in events if e == "stage"]
    assert progresses == sorted(progresses), progresses

    done = events[-1][1]
    assert done["job_id"] == job_id and done["plan_id"]
    assert done["status"] == "succeeded"
    # 方案真的落库了
    assert (await client.get(f"/api/v1/plans/{done['plan_id']}")).status_code == 200


async def test_sse_labels_are_human_and_emoji_free(api, client, runner_factory):
    """阶段文案是给家人看的：**不带 emoji**（06 设计规范），也不带工程词。"""
    runner_factory(graph_factory=fake_graph_factory(delay=0.1))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]
    r = await client.get(f"/api/v1/jobs/{job_id}/events")

    for event, data in parse_sse(r.text):
        label = data.get("label", "")
        assert not EMOJI.search(label), f"阶段文案里有 emoji：{label!r}"
        for leak in ("LLM", "Agent", "state", "node", "None"):
            assert leak not in label, f"阶段文案里有工程词：{label!r}"
    assert "正在挑菜谱" in r.text or "正在搭配" in r.text


async def test_sse_for_finished_job_returns_terminal_event_at_once(api, client, fast_runner):
    """任务早就做完了再连：立刻结束（先补一条终态 stage 让进度条走到头，再 done），不能挂着。"""
    job_id = (await client.post("/api/v1/plans", json={"days": 2})).json()["job_id"]
    for _ in range(60):
        if (await client.get(f"/api/v1/jobs/{job_id}")).json()["status"] == "succeeded":
            break
        await asyncio.sleep(0.05)

    r = await asyncio.wait_for(client.get(f"/api/v1/jobs/{job_id}/events"), timeout=5)
    events = parse_sse(r.text)
    assert [e for e, _ in events] == ["stage", "done"], events
    assert events[0][1]["stage"] == "answer" and events[0][1]["progress"] == 1.0
    assert events[1][1]["status"] == "succeeded"


# ---------------------------------------------------------------- 失败与取消

async def test_sse_emits_error_on_failure(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(raise_at="validate"))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]

    r = await client.get(f"/api/v1/jobs/{job_id}/events")
    events = parse_sse(r.text)
    assert events[-1][0] == "error"
    payload = events[-1][1]
    assert payload["code"] == "error"
    assert payload["message"].endswith("。")             # 人话
    assert "RuntimeError" not in r.text and "Traceback" not in r.text
    assert payload["next_steps"][0]["op"] == "retry"


async def test_sse_emits_error_with_timeout_code(api, client, runner_factory):
    runner_factory(timeout_sec=1, graph_factory=fake_graph_factory(delay=0.4))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]

    r = await client.get(f"/api/v1/jobs/{job_id}/events")
    payload = parse_sse(r.text)[-1][1]
    assert payload["code"] == "timeout"
    assert "排得太久" in payload["message"]
    assert payload["next_steps"]


async def test_sse_emits_error_when_cancelled(api, client, runner_factory):
    runner_factory(graph_factory=fake_graph_factory(delay=0.25))
    job_id = (await client.post("/api/v1/plans", json={"days": 3})).json()["job_id"]
    for _ in range(40):
        if (await client.get(f"/api/v1/jobs/{job_id}")).json()["status"] == "running":
            break
        await asyncio.sleep(0.05)
    await client.post(f"/api/v1/jobs/{job_id}/cancel")

    r = await client.get(f"/api/v1/jobs/{job_id}/events")
    payload = parse_sse(r.text)[-1][1]
    assert payload["code"] == "cancelled"
    assert "已经停了" in payload["message"]


# ---------------------------------------------------------------- 边界

async def test_sse_unknown_job_is_problem_json_not_a_stream(client):
    r = await client.get("/api/v1/jobs/nope/events")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "not_found"


def test_sse_message_format():
    from recipe_planner.api.routes.jobs import _sse

    raw = _sse("stage", {"stage": "plan", "label": "正在搭配这一周的菜…", "progress": 0.45})
    assert raw.startswith("event: stage\ndata: {")
    assert raw.endswith("\n\n")
    assert "\n\n" not in raw[:-2], "data 必须是单行，否则消息会被截断"
    # 中文不能被转义成 \\uXXXX（前端直接显示）
    assert "正在搭配" in raw
    assert json.loads(raw.split("data: ", 1)[1].strip())["progress"] == 0.45
