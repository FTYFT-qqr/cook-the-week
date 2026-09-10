"""会话级的操作记忆：操作回执、多步撤销、改动历史、二次确认。

为什么单独成模块：这些东西在菜单页和档案页都要用，
而客户最怕的两件事正好在这里 ——「点错了回不去」和「清空一下就没了」。
"""
from __future__ import annotations

from typing import Any, Optional

import streamlit as st

UNDO_LIMIT = 5      # 可连续撤销的步数
HISTORY_LIMIT = 12  # 改动历史保留条数


def init_defaults() -> None:
    """所有会话键的默认值集中在这里，避免散落在各页面。"""
    defaults: dict[str, Any] = {
        "plan_inputs": None,      # 已提交的约束快照（None = 还没生成过）
        "result": None,           # 当前展示的排菜结果
        "stale": False,           # 需要重排
        "record_id": None,        # 当前结果对应的存档 id（改动会写回它）
        "revisit": None,          # 本次是「打开就有存档」的回访态
        "job": None,              # 正在跑的排菜任务（PlanJob）
        "notice": None,           # 上一步操作回执
        "undo_stack": [],         # 多步撤销栈
        "history": [],            # 改动历史（文字）
        "confirm_arm": None,      # 二次确认：当前待确认的按钮 key
        "relax": None,            # 排不出来时待展示的放宽选项
        "check_epoch": 0,         # 清单勾选代次（换一版清单就作废旧的勾选）
        "mobile_view": False,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


# ---------------------------------------------------------------- 操作回执

def set_notice(kind: str, text: str) -> None:
    st.session_state["notice"] = {"kind": kind, "text": text}


def get_notice() -> Optional[dict]:
    return st.session_state.get("notice")


def clear_notice() -> None:
    st.session_state["notice"] = None


# ---------------------------------------------------------------- 多步撤销

def push_undo(text: str, *, days: Any = None, profile: Any = None,
              record_id: Optional[str] = None) -> None:
    stack = list(st.session_state.get("undo_stack") or [])
    stack.append({"text": text, "days": days, "profile": profile, "record_id": record_id})
    st.session_state["undo_stack"] = stack[-UNDO_LIMIT:]


def pop_undo() -> Optional[dict]:
    stack = list(st.session_state.get("undo_stack") or [])
    if not stack:
        return None
    entry = stack.pop()
    st.session_state["undo_stack"] = stack
    return entry


def undo_depth() -> int:
    return len(st.session_state.get("undo_stack") or [])


def last_undo_text() -> str:
    stack = st.session_state.get("undo_stack") or []
    return stack[-1]["text"] if stack else ""


def clear_undo() -> None:
    st.session_state["undo_stack"] = []


# ---------------------------------------------------------------- 改动历史

def push_history(text: str) -> None:
    hist = list(st.session_state.get("history") or [])
    hist.append(text)
    st.session_state["history"] = hist[-HISTORY_LIMIT:]


def history() -> list[str]:
    return list(reversed(st.session_state.get("history") or []))


# ---------------------------------------------------------------- 二次确认

def armed() -> Optional[str]:
    return st.session_state.get("confirm_arm")


def arm(key: str) -> None:
    st.session_state["confirm_arm"] = key


def disarm() -> None:
    st.session_state["confirm_arm"] = None
