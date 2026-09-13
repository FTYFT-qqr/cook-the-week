"""任务执行器（docs/09 P1-5）：`POST /plans` 排队、后台跑、可取消、可超时。

为什么要把排菜做成任务而不是同步请求：一次排菜要调模型、要校验修正，**几秒到几十秒**都有可能。
同步请求会把连接吊在那里，客户端刷新一下就不知道到底排没排（"你敢不敢点"就是这个）。
所以：立刻回 202 + `job_id`，进度写进 `job` 表，客户端轮询（P1-6 再换 SSE）。

**InProcessRunner 的做法**：一个常驻工作线程 + 一个待办队列（默认并发 1）。
"并发上限 1" 自然带来 docs/08 §6 要的"同一 household 的 plan_week 任务多余入队"。

三件如实说明的事：

1. **取消与超时都是"协作式"的**：Python 杀不掉线程，所以取消/超时在每个流水线节点之间检查
   （外加一次 LLM 调用内部的等待只能等它自己返回）。节点粒度是秒级，够用；
2. **任务状态存在 `job` 表里**，所以进程重启后哪怕任务没了，状态也查得到（不会出现"幽灵 running"）；
   P2 的 `QueueRunner`（Redis）会把"重启后 queued 任务可续跑"补上；
3. **P2 只换 Runner**，路由与状态机不动。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional, Protocol

from recipe_planner.infra import settings
from recipe_planner.infra.logging import log_event
from recipe_planner.models import PlanResult, UserConstraints
from recipe_planner.progress import STAGE_LABELS, STAGE_PROGRESS
from recipe_planner.storage import async_adapters as data
from recipe_planner.storage import sync_bridge
from recipe_planner.storage.repositories import PlanRepo

logger = logging.getLogger("recipe_planner.jobs")

DEFAULT_TIMEOUT_SEC = 120


def dump_error(code: str, message: str, next_steps: Optional[list[dict]] = None) -> str:
    """`job.error` 列（Text）里存的是 JSON —— 见 JobRepo 的说明。"""
    return json.dumps({"code": code, "message": message, "next_steps": next_steps or []},
                      ensure_ascii=False)


def load_error(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    return {"code": "failed", "message": raw, "next_steps": []}


class JobRunner(Protocol):
    def submit(self, job_id: str) -> None: ...
    def cancel(self, job_id: str) -> bool: ...
    def shutdown(self) -> None: ...


class InProcessRunner:
    """单进程任务执行器。测试可以注入假图（`graph_factory`）来精确控制时长与失败。"""

    def __init__(self, timeout_sec: Optional[int] = None, max_concurrent: int = 1,
                 graph_factory: Optional[Callable] = None,
                 db_loader: Optional[Callable] = None) -> None:
        self.timeout_sec = timeout_sec or DEFAULT_TIMEOUT_SEC
        self.max_concurrent = max(1, max_concurrent)
        self._graph_factory = graph_factory
        self._db_loader = db_loader
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._stopping = threading.Event()

    # ---------------------------------------------------------- 对外接口

    def submit(self, job_id: str) -> None:
        self._ensure_started()
        with self._lock:
            self._cancels.setdefault(job_id, threading.Event())
        self._queue.put(job_id)

    def cancel(self, job_id: str) -> bool:
        """置取消位。排队中的任务会在被取到时直接跳过，运行中的在下一个节点停下。"""
        with self._lock:
            event = self._cancels.get(job_id)
        if event is not None:
            event.set()
            return True
        return False

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancels

    def queue_size(self) -> int:
        return self._queue.qsize()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        for worker in self._workers:
            worker.join(timeout)
        self._workers.clear()

    # ---------------------------------------------------------- 工作线程

    def _ensure_started(self) -> None:
        with self._lock:
            # 允许 shutdown 之后再 submit（测试与将来的热重启都会这么用）
            self._stopping.clear()
            alive = [w for w in self._workers if w.is_alive()]
            while len(alive) < self.max_concurrent:
                worker = threading.Thread(target=self._loop, name="plan-worker", daemon=True)
                worker.start()
                alive.append(worker)
            self._workers = alive

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue                       # 没有活儿就再看看要不要停（不用哨兵：哨兵会留在队列里害下一个 worker）
            try:
                self._run_one(job_id)
            except Exception:                  # 工作线程绝不能因为一个任务挂掉
                logger.exception("任务执行器出错：%s", job_id)
            finally:
                with self._lock:
                    self._cancels.pop(job_id, None)

    # ---------------------------------------------------------- 一个任务的完整生命周期

    def _run_one(self, job_id: str) -> None:
        job = sync_bridge.run(data.job_repo().get(job_id))
        if job is None:
            log_event(logger, logging.WARNING, "任务不存在，跳过", job_id=job_id)
            return
        if job["status"] == "cancelled" or self._cancelled(job_id):
            self._finish(job_id, "cancelled", stage="cancelled", progress=0.0)
            return

        self._set_status(job_id, "running", stage="retrieve", progress=STAGE_PROGRESS["retrieve"])
        deadline = time.monotonic() + self.timeout_sec
        started = time.time()
        try:
            result = self._work(job_id, job["request"], deadline)
        except _Cancelled:
            self._finish(job_id, "cancelled", stage="cancelled", progress=0.0)
            log_event(logger, logging.INFO, "任务已取消", job_id=job_id)
            return
        except _TimedOut:
            self._finish(
                job_id, "failed", stage="timeout", progress=STAGE_PROGRESS["plan"],
                code="timeout",
                message=f"这次排得太久（超过 {self.timeout_sec} 秒）我先停了。"
                        "可以少排几天，或者把时长上限放宽一点再试。",
                next_steps=[{"op": "reduce_days", "label": "先排 3 天"},
                            {"op": "relax_time", "label": "时长上限放宽到 60 分钟"}])
            log_event(logger, logging.WARNING, "任务超时", job_id=job_id,
                      timeout_sec=self.timeout_sec)
            return
        except Exception as exc:                   # 兜底：任何异常都要落到任务状态里
            self._finish(job_id, "failed", stage="failed", progress=STAGE_PROGRESS["plan"],
                         code="error",
                         message="这次没排出来，我这边出了点问题。稍后再试一次就行。",
                         next_steps=[{"op": "retry", "label": "再排一次"}])
            log_event(logger, logging.ERROR, "任务失败", job_id=job_id,
                      error=type(exc).__name__)
            logger.exception("任务失败：%s", job_id)
            return

        if result is None:
            self._finish(job_id, "failed", stage="failed", progress=STAGE_PROGRESS["shopping"],
                         code="empty",
                         message="这次没排出来，可能能选的菜不够了。放宽一点条件再试一次。",
                         next_steps=[{"op": "relax_time", "label": "时长上限放宽到 60 分钟"},
                                     {"op": "relax_budget", "label": "预算放宽一点"}])
            return

        result.latency_sec = round(time.time() - started, 2)
        record = sync_bridge.run(data.save_plan(result,
                                                job["request"].get("start_date"),
                                                job["request"].get("change_note", ""),
                                                make_active=True))
        self._finish(job_id, "succeeded", stage="succeeded", progress=1.0, plan_id=record.id)
        log_event(logger, logging.INFO, "任务完成", job_id=job_id, plan_id=record.id,
                  seconds=result.latency_sec, llm_used=result.llm_used)

    # ---------------------------------------------------------- 真正干活

    def _work(self, job_id: str, request: dict, deadline: float) -> Optional[PlanResult]:
        """在工作线程里跑流水线，逐节点更新阶段、检查取消与超时。"""
        constraints = UserConstraints(**{k: v for k, v in request.items()
                                         if k not in ("start_date", "change_note")})
        db = (self._db_loader or self._load_db)()
        graph = (self._graph_factory or self._build_graph)(db)
        init = {"db": db, "constraints": constraints, "trace": []}
        result: Optional[PlanResult] = None

        for chunk in graph.stream(init, stream_mode="updates"):
            if self._cancelled(job_id):
                raise _Cancelled()
            if time.monotonic() > deadline:
                raise _TimedOut()
            node = next(iter(chunk), "") if isinstance(chunk, dict) else ""
            if node in STAGE_LABELS:
                self._set_status(job_id, "running", stage=node,
                                 progress=STAGE_PROGRESS.get(node, 0.5))
            if isinstance(chunk, dict) and chunk.get("answer", {}).get("result") is not None:
                result = chunk["answer"]["result"]
        if self._cancelled(job_id):
            raise _Cancelled()
        return result

    @staticmethod
    def _load_db():
        """同步门面自己会按 STORAGE 分派（json 模式读文件，db 模式读库）。"""
        from recipe_planner.db import load_db

        return load_db()

    @staticmethod
    def _build_graph(db):
        from recipe_planner.graph import build_graph

        return build_graph(db)

    # ---------------------------------------------------------- 状态落库

    def _cancelled(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancels.get(job_id)
        return bool(event and event.is_set())

    def _set_status(self, job_id: str, status: str, *, stage: str, progress: float) -> None:
        sync_bridge.run(data.job_repo().set_status(job_id, status, stage=stage,
                                                   progress=progress))

    def _finish(self, job_id: str, status: str, *, stage: str, progress: float,
                plan_id: Optional[str] = None, code: str = "", message: str = "",
                next_steps: Optional[list[dict]] = None) -> None:
        repo = data.job_repo()
        if status == "succeeded":
            sync_bridge.run(repo.set_status(job_id, status, stage=stage, progress=progress,
                                            plan_id=plan_id))
            return
        if status == "cancelled":
            code, message = "cancelled", "这次排菜已经停了，需求都还在，改完再点一次就行。"
            next_steps = next_steps or [{"op": "retry", "label": "重新排一次"}]
        sync_bridge.run(repo.set_status(job_id, status, stage=stage, progress=progress,
                                        error=dump_error(code or "failed", message,
                                                         next_steps)))


class _Cancelled(Exception):
    """协作式取消：在节点之间抛出，由 `_run_one` 落成 cancelled。"""


class _TimedOut(Exception):
    """协作式超时：同上，落成 failed(timeout)。"""


# ---------------------------------------------------------------- 全局执行器

_runner: Optional[InProcessRunner] = None
_runner_lock = threading.Lock()


def get_runner() -> InProcessRunner:
    """进程内单例（懒启动：第一次提交任务时才起工作线程）。"""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = InProcessRunner(timeout_sec=settings.job_timeout_sec())
        return _runner


def set_runner(runner: Optional[InProcessRunner]) -> None:
    """测试用：换掉执行器（注入假图、缩短超时），用例结束后还原。"""
    global _runner
    with _runner_lock:
        if _runner is not None and _runner is not runner:
            _runner.shutdown()
        _runner = runner


def shutdown_runner() -> None:
    global _runner
    with _runner_lock:
        if _runner is not None:
            _runner.shutdown()
            _runner = None
