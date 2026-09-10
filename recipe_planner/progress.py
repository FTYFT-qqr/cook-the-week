"""生成过程的阶段反馈与取消。

客户的原话是：「转圈 + 一句话提示，我不知道要等 3 秒还是 30 秒，中途也不敢点。」
所以这里把 LangGraph 的每个节点翻译成客户看得懂的一句话，并让「停止」真的能停下。

做法：把流水线放到后台线程里用 stream 逐节点推进，主线程每次 rerun 只读当前阶段。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from recipe_planner.db import load_db
from recipe_planner.graph import build_graph
from recipe_planner.models import PlanResult, RecipeDB, UserConstraints

# 节点名 → 客户看得懂的一句话
STAGES = {
    "retrieve": "🥬 正在挑菜谱（先排除过敏原和忌口）…",
    "plan": "🍳 正在搭配这一周的菜…",
    "validate": "🔍 正在检查忌口、辣度、预算和时间…",
    "repair": "🛠️ 有几处不太合适，正在调整重排…",
    "shopping": "🛒 正在汇总买菜清单…",
    "answer": "✅ 马上就好…",
}
PREPARING = "🍳 正在准备…"
CANCELLED = "⏹️ 已停止"
FINISHED = "✅ 排好了"
STAGE_ORDER = ["retrieve", "plan", "validate", "repair", "shopping", "answer"]


class PlanJob:
    """一次排菜任务：可在后台推进，可随时取消。"""

    def __init__(self, constraints: UserConstraints, db: Optional[RecipeDB] = None,
                 graph_factory: Optional[Callable] = None):
        self.constraints = constraints
        self.db = db or load_db()
        self._graph_factory = graph_factory or build_graph

        self.stage: str = PREPARING
        self.stages_seen: list[str] = []
        self.result: Optional[PlanResult] = None
        self.error: Optional[str] = None
        self.done: bool = False
        self.cancelled: bool = False
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------- 生命周期

    def start(self) -> "PlanJob":
        self._thread = threading.Thread(target=self._run, name="plan-job", daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def progress(self) -> float:
        """粗略进度：让进度条不会一直停在 0。"""
        if self.done:
            return 1.0
        return min(0.9, 0.15 + 0.15 * len(self.stages_seen))

    def wait(self, timeout: float = 60.0) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return self.done

    # ---------------------------------------------------------- 工作线程

    def _run(self) -> None:
        t0 = time.time()
        try:
            graph = self._graph_factory(self.db)
            init = {"db": self.db, "constraints": self.constraints, "trace": []}
            for chunk in graph.stream(init, stream_mode="updates"):
                node = next(iter(chunk), "") if isinstance(chunk, dict) else ""
                if node:
                    self.stages_seen.append(node)
                    self.stage = STAGES.get(node, self.stage)
                if isinstance(chunk, dict) and chunk.get("answer", {}).get("result") is not None:
                    self.result = chunk["answer"]["result"]
                if self._cancel.is_set():
                    self.cancelled = True
                    self.stage = CANCELLED
                    self.result = None
                    return
            if self.result is not None:
                self.result.latency_sec = round(time.time() - t0, 2)
                self.stage = FINISHED
            else:
                self.error = "没有拿到排菜结果"
                self.stage = "⚠️ 这次没排好"
        except Exception as exc:  # 线程里任何异常都不能让页面挂掉
            self.error = f"{type(exc).__name__}: {exc}"
            self.stage = "⚠️ 这次没排好"
        finally:
            self.done = True
