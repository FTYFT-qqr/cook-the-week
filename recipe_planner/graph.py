"""LangGraph 编排：collect → retrieve → plan → validate ⇄ repair → shopping → answer。

亮点：plan 优先用 LLM；validate 用确定性代码；
有硬错误则带着反馈让 LLM 重排（repair），超限后切确定性兜底，保证 demo 永不崩。
"""
from __future__ import annotations

import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from recipe_planner.core import (
    plan_deterministic,
    retrieve_candidates,
    shopping_list,
    validate_plan,
)
from recipe_planner.db import load_db
from recipe_planner.llm_planner import llm_plan
from recipe_planner.models import (
    DayPlan,
    PlanResult,
    Recipe,
    RecipeDB,
    ShoppingItem,
    UserConstraints,
    ValidationIssue,
)

MAX_REPAIRS = 2  # LLM 修正轮数上限


class GraphState(TypedDict, total=False):
    db: RecipeDB
    constraints: UserConstraints
    candidates: list[Recipe]
    plans: list[DayPlan]
    issues: list[ValidationIssue]
    repairs_used: int
    llm_used: bool
    llm_error: str | None
    shopping_items: list[ShoppingItem]
    trace: list[str]
    result: PlanResult


# ---------------- 节点 ----------------

def retrieve_node(state: GraphState) -> GraphState:
    c: UserConstraints = state["constraints"]
    db: RecipeDB = state["db"]
    cands = retrieve_candidates(db, c)
    return {"candidates": cands, "trace": state["trace"] + [f"检索：{len(db.recipes)} 道菜谱 → 过滤后 {len(cands)} 道候选"]}


def plan_node(state: GraphState) -> GraphState:
    c, db, cands = state["constraints"], state["db"], state["candidates"]
    issues = state.get("issues", [])
    feedback = _build_feedback(issues) if issues else None
    plans, err = llm_plan(c, cands, feedback)
    trace = state["trace"]
    if plans is None:
        plans, _warns = plan_deterministic(cands, db, c)
        trace = trace + [f"LLM 排菜失败({err or '结构不符'}) → 确定性排菜兜底"]
        return {"plans": plans, "llm_used": False, "llm_error": err, "trace": trace}
    return {"plans": plans, "llm_used": True, "llm_error": None, "trace": trace + ["LLM 排菜完成"]}


def validate_node(state: GraphState) -> GraphState:
    c, db = state["constraints"], state["db"]
    issues = validate_plan(state["plans"], db, c)
    return {"issues": issues, "trace": state["trace"] + [f"确定性校验：{len(issues)} 个问题"]}


def repair_node(state: GraphState) -> GraphState:
    """带校验反馈再排：LLM 修正(<MAX_REPAIRS)，到最后一轮强制切确定性兜底。"""
    c, db, cands = state["constraints"], state["db"], state["candidates"]
    used = state.get("repairs_used", 0) + 1
    trace = state["trace"]
    if used < MAX_REPAIRS and state.get("llm_used", False):
        plans, err = llm_plan(c, cands, _build_feedback(state.get("issues", [])))
        if plans is not None:
            return {"plans": plans, "repairs_used": used, "llm_error": None,
                    "trace": trace + [f"第 {used} 轮修正：LLM 重排"]}
        trace = trace + [f"第 {used} 轮修正 LLM 失败({err})"]
    plans, _ = plan_deterministic(cands, db, c)
    return {"plans": plans, "repairs_used": used,
            "trace": trace + [f"第 {used} 轮修正：确定性排菜兜底"]}


def shopping_node(state: GraphState) -> GraphState:
    c, db = state["constraints"], state["db"]
    items = shopping_list(state["plans"], db, c)
    return {"shopping_items": items, "trace": state["trace"] + ["生成买菜清单"]}


def answer_node(state: GraphState) -> GraphState:
    c, db = state["constraints"], state["db"]
    plans, issues = state["plans"], state.get("issues", [])
    items = state.get("shopping_items", [])
    total_cost = 0.0
    for p in plans:
        for d in p.dishes:
            r = db.by_id(d.recipe_id)
            if r:
                total_cost += r.cost_yuan * c.people / 2.0
    hard_left = [i for i in issues if i.level == "error"]
    result = PlanResult(
        constraints=c,
        candidate_count=len(state.get("candidates", [])),
        days=plans,
        issues=issues,
        repairs_used=state.get("repairs_used", 0),
        shopping=items,
        llm_used=state.get("llm_used", False),
        llm_error=state.get("llm_error"),
        estimated_cost_yuan=round(total_cost, 2),
        final=len(hard_left) == 0,
        trace=state["trace"] + ["输出结果"],
    )
    return {"result": result}


def _build_feedback(issues: list[ValidationIssue]) -> str:
    if not issues:
        return ""
    errs = [i.message for i in issues if i.level == "error"]
    warns = [i.message for i in issues if i.level == "warning"]
    parts = []
    if errs:
        parts.append("硬性问题（必须修正）：\n- " + "\n- ".join(errs))
    if warns:
        parts.append("软性问题（尽量改善）：\n- " + "\n- ".join(warns))
    return "\n".join(parts)


def _route_after_validate(state: GraphState) -> str:
    hard = [i for i in state.get("issues", []) if i.level == "error"]
    if not hard:
        return "shopping"
    # 仍有硬错误但修正轮数已达上限 → 停止修正，如实输出（含问题说明）
    if state.get("repairs_used", 0) >= MAX_REPAIRS:
        return "shopping"
    return "repair"


# ---------------- 图 ----------------

def build_graph(db: RecipeDB | None = None) -> Any:
    db = db or load_db()
    g = StateGraph(GraphState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("plan", plan_node)
    g.add_node("validate", validate_node)
    g.add_node("repair", repair_node)
    g.add_node("shopping", shopping_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "plan")
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", _route_after_validate,
                            {"repair": "repair", "shopping": "shopping"})
    g.add_edge("repair", "validate")
    g.add_edge("shopping", "answer")
    g.add_edge("answer", END)

    return g.compile()


def run_pipeline(c: UserConstraints, db: RecipeDB | None = None) -> PlanResult:
    db = db or load_db()
    graph = build_graph(db)
    t0 = time.time()
    init: dict[str, Any] = {"db": db, "constraints": c, "trace": []}
    final_state = graph.invoke(init)
    result: PlanResult = final_state["result"]
    result.latency_sec = round(time.time() - t0, 2)
    return result
