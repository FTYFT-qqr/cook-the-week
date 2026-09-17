"""偏好事件：**每道菜**上发生了什么（docs/12 §4 阶段二）。

## 这一层解决什么

理想文档要的"时间衰减 × 反馈方向"权重，前提是**有带时间戳的逐菜信号**。
现在能拿到的只有两个布尔列表（喜欢/不喜欢）和几条带 `MM/DD` 的评分 ——
"换掉过一道菜""某顿跳过没做"这类**最说明问题**的信号根本没存下来。

## 两个设计判断

**① 事件从"状态差异"推导，不从每个按钮里手写。**
按钮有八九处（换一道 / 回家晚了 / 来客人了 / 不喜欢 / 打分 / 定做 / 省钱 / 重排…），
每处手写一遍必然漏。而"菜单变了什么"在**持久化那一处**（`store.update_result` /
`PlanRepo.update_result`）就能算出来，"档案变了什么"在 `profile.set_feedback/rate` 那一处能算出来 ——
所以只在这**两个漏斗**上挂 `menu_events()` / `profile_events()`，两个前端、两种存储全覆盖。

**② 动作词表是一个闭集**（`ACTIONS`），权重函数按它取方向分。
表(`dish_event`)上**故意不加 CheckConstraint**：以后加一种动作不该需要一次迁移，
合法性由这里 + 测试守（`tests/test_dish_events.py`）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from recipe_planner.infra.jsonfile import read_json, write_json
from recipe_planner.models import MEALS, PlanResult

# 动作词表（闭集）
SELECT = "select"          # 被排进菜单（含"改回来做"）
SWAP_OUT = "swap_out"      # 被换掉（用户主动换或"不喜欢"带出来的换）
SKIP = "skip"              # 因为"这顿不做饭"被去掉
LIKE = "like"
UNLIKE = "unlike"
DISLIKE = "dislike"
UNDISLIKE = "undislike"    # 从"不喜欢"里挪回来（例如改主意点回喜欢）
LOCK = "lock"              # 定住：强偏好
UNLOCK = "unlock"
RATE_GOOD = "rate_good"    # 好吃
RATE_OK = "rate_ok"        # 一般
RATE_NEVER = "rate_never"  # 下次不做
DONE = "done"              # 这顿做完了（docs/12 阶段二 2.6：「吃过」的最直接证据）
SNOOZE = "snooze"          # 临时避开：默认 7 天后自动恢复
UNSNOOZE = "unsnooze"      # 提前取消临时避开

ACTIONS = frozenset({SELECT, SWAP_OUT, SKIP, LIKE, UNLIKE, DISLIKE, UNDISLIKE,
                     LOCK, UNLOCK, RATE_GOOD, RATE_OK, RATE_NEVER, DONE,
                     SNOOZE, UNSNOOZE})

# 允许的界面/来源（只做人话标注，不参与计算）
SOURCES = frozenset({"今晚页", "今天页", "本周计划", "口味档案", "做完了打分", "排一周", "接口"})

DEFAULT_EVENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "dish_events.json"
MAX_EVENTS = 5000      # JSON 后端的保留上限（数据库后端不截断）


def _slot_ids(result: Optional[PlanResult], day: int, meal: str) -> list[str]:
    if result is None:
        return []
    slot = result.slot(day, meal)
    return list(slot.recipe_ids()) if slot is not None else []


def menu_events(before: Optional[PlanResult], after: PlanResult, *,
                meals: Optional[Iterable[str]] = None,
                source: str = "") -> list[dict]:
    """比较"改动前 / 改动后"的菜单，推出逐菜事件。

    - 少掉的菜 → `swap_out`；如果那一顿整顿变成了"不做饭" → `skip`（语义不同，别混）；
    - 多出来的菜 → `select`；
    - `must_include_recipes` 里新加的 → `lock`、去掉的 → `unlock`（"定住"不在菜里，在约束里）；
    - 没变的菜**不产生事件**（否则每次改动都会刷一堆噪声）。
    """
    out: list[dict] = []
    wanted = list(meals or MEALS)
    days = sorted({p.day for p in after.days} | {p.day for p in (before.days if before else [])})
    for day in days:
        for meal in wanted:
            old_ids = _slot_ids(before, day, meal)
            new_ids = _slot_ids(after, day, meal)
            slot_after = after.slot(day, meal)
            removed = [rid for rid in old_ids if rid not in new_ids]
            added = [rid for rid in new_ids if rid not in old_ids]
            gone = bool(slot_after is None or slot_after.skipped) or (not new_ids and old_ids)
            for rid in removed:
                out.append({"recipe_id": rid, "action": SKIP if gone else SWAP_OUT,
                            "meal": meal, "day_no": day, "source": source})
            for rid in added:
                out.append({"recipe_id": rid, "action": SELECT,
                            "meal": meal, "day_no": day, "source": source})
    old_locked = set((before.constraints.must_include_recipes if before else []) or [])
    new_locked = set(after.constraints.must_include_recipes or [])
    for rid in sorted(new_locked - old_locked):
        out.append({"recipe_id": rid, "action": LOCK, "meal": "", "day_no": None, "source": source})
    for rid in sorted(old_locked - new_locked):
        out.append({"recipe_id": rid, "action": UNLOCK, "meal": "", "day_no": None, "source": source})
    return out


def _set_diff(old: Iterable[str], new: Iterable[str]) -> tuple[set, set]:
    return set(old or []) - set(new or []), set(new or []) - set(old or [])


def done_events(slot, *, day: Optional[int] = None, source: str = "") -> list[dict]:
    """这一顿「做完了」→ 逐菜一条 `done` 事件（docs/12 阶段二 2.6）。

    为什么单独一个动作：菜单类事件的入口是"改动前后比一比"，而**标记做完不改菜单** ——
    它没有差异可推，只能显式记一条。而它恰恰是"**确实吃过**"最直接的证据：
    `select` 只是"排进了菜单"，拿它当吃过的证据会变成自我实现（算法排得越多越像爱吃）。

    **取消标记不写事件**：饭已经下肚了，取消只是"标错了"，不是"没吃过"。
    """
    if slot is None:
        return []
    return [{"recipe_id": rid, "action": DONE, "meal": getattr(slot, "meal", "") or "",
             "day_no": day, "source": source}
            for rid in slot.recipe_ids()]


def profile_events(before: dict, after: dict, *, by_name: dict[str, str],
                   source: str = "") -> list[dict]:
    """比较"改动前 / 改动后的档案"，推出逐菜事件（菜名 → 菜谱 id）。

    `by_name`：菜名 → 菜谱 id。档案里没有的菜（库里已经删了/改了名）就跳过 ——
    事件存的是 id，翻不出来就没法用。
    """
    out: list[dict] = []

    def add(name: str, action: str) -> None:
        rid = by_name.get(name)
        if rid:
            out.append({"recipe_id": rid, "action": action, "meal": "", "day_no": None,
                        "source": source})

    old_like, new_like = set(before.get("liked_dishes") or []), set(after.get("liked_dishes") or [])
    old_hate, new_hate = set(before.get("disliked_dishes") or []), set(after.get("disliked_dishes") or [])
    for name in sorted(new_like - old_like):
        add(name, LIKE)
    for name in sorted(old_like - new_like):
        add(name, UNLIKE)
    for name in sorted(new_hate - old_hate):
        add(name, DISLIKE)
    for name in sorted(old_hate - new_hate):
        # 从"不喜欢"里挪出来：可能只是"取消不喜欢"，也可能被挪进了喜欢（那就另有 like 事件）
        add(name, UNDISLIKE)

    old_ratings = before.get("ratings") or {}
    new_ratings = after.get("ratings") or {}
    score_action = {2: RATE_GOOD, 1: RATE_OK, 0: RATE_NEVER}
    for name, info in new_ratings.items():
        new_score = int((info or {}).get("score", 1))
        old_score = (old_ratings.get(name) or {}).get("score")
        if old_score is None or int(old_score) != new_score:
            add(name, score_action.get(new_score, RATE_OK))
    return out


# ---------------------------------------------------------------- 读写（两种后端）
#
# 与 store.py / profile.py 同一套办法：本模块给 JSON 实现，`STORAGE=db` 时
# 末尾那几行用 `storage/db_events.py` 的同名函数覆盖它，调用方一行都不用改。

def events_path() -> Path:
    import os

    return Path(os.environ.get("RECIPE_EVENTS_FILE", str(DEFAULT_EVENTS_FILE)))


def load_events() -> list[dict]:
    """按时间从早到晚返回所有事件。

    docs/11 §4.1 P0-2 的同一套纪律：文件**不在** = 还没有事件；**在但读不出来** = 报错留档，
    绝不当成"没有事件"（那会让权重悄悄回退到"没有偏好"）。
    """
    data = read_json(events_path())
    if data is None:
        return []
    raw = data.get("events", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError(f"事件文件格式不对：{events_path()}")
    return [e for e in raw if isinstance(e, dict) and e.get("recipe_id")]


def record(events: list[dict], *, now: Optional[datetime] = None,
           plan_id: Optional[str] = None) -> int:
    """追加事件（JSON 后端）。返回写入条数。"""
    if not events:
        return 0
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    fresh = [{**e, "plan_id": plan_id, "created_at": stamp} for e in events]
    rows = load_events() + fresh
    write_json(events_path(), {"version": 1, "events": rows[-MAX_EVENTS:]})
    return len(fresh)


def recent(days: int = 180, *, today: Optional[date] = None) -> list[dict]:
    """最近 N 天的事件（权重只看这段窗口）。"""
    cutoff = (today or date.today()) - timedelta(days=max(1, days))
    out = []
    for e in load_events():
        when = _when(e)
        if when is None or when.date() >= cutoff:
            out.append(e)
    return out


def _when(e: dict) -> Optional[datetime]:
    raw = e.get("created_at") or ""
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def clear() -> None:
    write_json(events_path(), {"version": 1, "events": []})


def record_profile_diff(before: dict, after: dict, *, source: str = "") -> int:
    """档案改动 → 事件。**档案只有一个写入口**（`save_profile`），所以挂在那里就全覆盖：
    界面点喜欢 / API 的 PUT /profile / 撤销恢复 / 清空，全都会经过它。

    翻不出 id 的菜（库里已删/改名）直接跳过 —— 事件存 id，翻不出来就没法用。
    """
    try:
        from recipe_planner.db import load_db

        by_name = {r.name: r.id for r in load_db().recipes}
    except Exception:
        return 0
    fresh = profile_events(before, after, by_name=by_name, source=source)
    return record(fresh)


# ---------------------------------------------------------------- 后端切换（docs/08 §7）
from recipe_planner.infra import settings as _settings  # noqa: E402

if _settings.storage_kind() == "db":  # pragma: no cover - 由环境变量决定
    from recipe_planner.storage.db_events import (  # noqa: E402,F401,F811
        clear, events_path, load_events, recent, record,
    )
