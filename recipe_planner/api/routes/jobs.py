"""排菜任务接口（docs/08 §6 / docs/09 P1-5）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/plans` | **202 + `job_id`**（异步排队），需要 `Idempotency-Key` |
| GET | `/api/v1/jobs/{id}` | 任务状态（兜底轮询；P1-6 加 SSE 推送） |
| POST | `/api/v1/jobs/{id}/cancel` | 取消（排队中直接取消，运行中在下一个节点停） |

为什么不做成同步请求：一次排菜要调模型 + 校验修正，几秒到几十秒；同步会把连接吊住，
客户端一刷新就不知道到底排没排 —— "我到底敢不敢点"就是这个问题。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from recipe_planner.infra import settings
from recipe_planner.models import ALLERGENS
from recipe_planner.storage import async_adapters as data

from ..errors import ConflictError, InvalidRequestError, NotFoundError, step
from ..schemas import JobAcceptedOut, JobOut, PlanCreateIn
from ..worker import get_runner, load_error

router = APIRouter()


def _constraints_payload(body: PlanCreateIn) -> dict:
    payload = body.model_dump(exclude={"start_date", "change_note"})
    unknown = [a for a in payload.get("allergens", []) if a not in ALLERGENS]
    if unknown:
        raise InvalidRequestError(
            f"「{'、'.join(unknown)}」我这边不认，可能是写法不一样。",
            next_steps=[step("pick_allergens", "从这些里选：" + "、".join(ALLERGENS))])
    return payload


@router.post("/plans", response_model=JobAcceptedOut, status_code=202, tags=["jobs"],
             responses={202: {"description": "已排队，用 job_id 查进度"},
                        409: {"description": "同一个 Idempotency-Key 还在处理中"},
                        422: {"description": "需求填得不对（例如不认识的忌口）"}})
async def create_plan(body: PlanCreateIn) -> JobAcceptedOut:
    """排一周：立刻返回 `job_id`，进度看 `GET /api/v1/jobs/{job_id}`。"""
    payload = _constraints_payload(body)
    payload["start_date"] = body.start_date
    payload["change_note"] = body.change_note

    repo = data.job_repo()
    before = await repo.active_count(kind="plan_week")
    job = await repo.create(kind="plan_week", request=payload)
    get_runner().submit(job["id"])

    queue_position = before + 1
    wait_hint = "" if queue_position <= 1 else f"（前面还有 {queue_position - 1} 个任务，排完就轮到你）"
    return JobAcceptedOut(
        job_id=job["id"], status="queued", queue_position=queue_position,
        message=f"已经收到你的要求，正在排这一周{wait_hint}。",
        poll_path=f"/api/v1/jobs/{job['id']}",
        timeout_sec=settings.job_timeout_sec(),
        next_steps=[step("poll_job", "看看排到哪一步了"),
                    step("cancel_job", "不想排了就取消")])


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"],
            responses={404: {"description": "任务不存在"}})
async def read_job(job_id: str) -> JobOut:
    job = await data.job_repo().get(job_id)
    if job is None:
        raise NotFoundError("没找到这个任务，可能已经过期了。",
                            next_steps=[step("create_plan", "重新排一周")])
    return _to_out(job)


@router.get("/jobs", response_model=list[JobOut], tags=["jobs"])
async def list_jobs(limit: int = 10) -> list[JobOut]:
    """最近的任务（排障用：看看刚才那次到底怎么了）。"""
    return [_to_out(j) for j in await data.job_repo().list_recent(limit)]


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["jobs"],
             responses={404: {"description": "任务不存在"},
                        409: {"description": "任务已经结束了"}})
async def cancel_job(job_id: str) -> JobOut:
    """取消：排队中的立刻取消；运行中的会在下一个节点停下（协作式取消）。"""
    repo = data.job_repo()
    job = await repo.get(job_id)
    if job is None:
        raise NotFoundError("没找到这个任务，可能已经过期了。",
                            next_steps=[step("create_plan", "重新排一周")])
    if job["status"] in ("succeeded", "failed", "cancelled"):
        raise ConflictError(f"这个任务已经{'排好了' if job['status'] == 'succeeded' else '结束了'}，不用再取消。",
                            next_steps=[step("view_plan", "看看已有的方案")])
    get_runner().cancel(job_id)
    if job["status"] == "queued":
        # 还没开跑：直接落状态，不用等工作线程转到它
        from ..worker import dump_error

        job = await repo.set_status(
            job_id, "cancelled", stage="cancelled", progress=job["progress"],
            error=dump_error("cancelled", "这次排菜已经停了，需求都还在，改完再点一次就行。",
                             [step("retry", "重新排一次")])) or job
    return _to_out(job)


def _to_out(job: dict) -> JobOut:
    error = load_error(job.get("error"))
    return JobOut(id=job["id"], kind=job["kind"], status=job["status"], stage=job["stage"],
                  progress=job["progress"], plan_id=job.get("plan_id"),
                  created_at=job.get("created_at", ""), started_at=job.get("started_at", ""),
                  finished_at=job.get("finished_at", ""),
                  error_code=(error or {}).get("code"),
                  message=(error or {}).get("message", ""),
                  next_steps=(error or {}).get("next_steps", []),
                  request={k: v for k, v in (job.get("request") or {}).items()
                           if k in ("people", "days", "dishes_per_day", "start_date")})
