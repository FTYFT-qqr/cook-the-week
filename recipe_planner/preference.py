"""偏好权重：让"喜欢"变成**会衰减的记忆**（docs/12 §4 阶段二 2.3–2.5）。

## 公式

    w(菜) = GAIN × Σ (方向分 × 时间衰减(距今多少天))
           再夹到 [−8, +16]

- **时间衰减**（理想文档给的常数，先手工、不自适应）：≤7 天 ×1.0、≤30 天 ×0.5、≤90 天 ×0.2、更早 ×0；
- **方向分**按动作给（`DIRECTION`）：明确说"不要"的比"换掉"重，"定住"比"喜欢"重；
- `GAIN = 8`：**一次刚发生的"喜欢"≈ 旧的静态 +8**，所以换上来不会让菜单突然大改，
  变的只是"同一条喜欢，时间越久越轻"。

## 两个刻意的设计

**① 只算"时间维度"，频次维度缓上。**
本项目的场景是"一周排一次"：一道菜一年被选 3–5 次，**算不出有意义的方差**
（docs/12 §5 的硬约束）。所以 `stability()` 只**算**不参与排序 ——
先验证"好久没吃"这种小样本也稳的信号，再考虑频次。

**② 事件优先，老档案补位。**
事件表是这次新加的，**历史偏好只存在于档案里**（`history.since` / `ratings.date`，都是 `MM/DD`）。
所以权重 = 事件算出来的 + 档案里那些**没有对应事件**的菜补一份（否则"新功能上线那一刻，
用户此前的喜好在权重里等于不存在"）。同一道菜只要事件里有记录，就不再吃档案那份，避免重复计算。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from recipe_planner import events as ev

# 时间衰减（天 → 系数）
DECAY_STEPS: tuple[tuple[int, float], ...] = ((7, 1.0), (30, 0.5), (90, 0.2))
DECAY_MAX_DAYS = 90

# 方向分：正=更想看到，负=更不想看到
DIRECTION: dict[str, float] = {
    ev.LIKE: 1.0,
    ev.RATE_GOOD: 1.0,
    ev.LOCK: 1.5,          # 定住 = 最强的正偏好（用户明确要求保留）
    ev.UNLIKE: -1.0,
    ev.RATE_NEVER: -1.0,
    ev.DISLIKE: -2.0,      # 明确说不要
    ev.UNDISLIKE: 0.5,     # 从"不喜欢"里挪回来：轻微的"可以再给它机会"
    ev.UNLOCK: 0.0,
    ev.SWAP_OUT: -0.6,     # 被换掉过：短期内别再排到它
    ev.SKIP: -0.3,         # 因为这顿不做饭被去掉：比"被换掉"轻
    ev.SELECT: 0.2,        # 被重新排进来：弱正（是算法选的，不该给太重的分）
    ev.RATE_OK: 0.0,
}

GAIN = 8.0
CLAMP = (-8.0, 16.0)


def decay(age_days: int) -> float:
    for limit, factor in DECAY_STEPS:
        if age_days <= limit:
            return factor
    return 0.0


def _age_days(when: Optional[datetime | date], today: date) -> Optional[int]:
    if when is None:
        return None
    d = when.date() if isinstance(when, datetime) else when
    return max(0, (today - d).days)


def _parse_md(text: str, today: date) -> Optional[date]:
    """档案里的 `MM/DD` → 日期（**没有年份**）。

    口径：当成"最近一次出现的那一天"（今年；今年还没到就是去年）——
    档案本来就是"当前状态 + 第一次记录的日子"，这样解释最不容易错。
    """
    raw = str(text or "").strip()
    if not raw or "/" not in raw:
        return None
    try:
        month, day = (int(x) for x in raw.split("/", 1))
        guess = date(today.year, month, day)
    except (ValueError, TypeError):
        return None
    return guess if guess <= today else date(today.year - 1, month, day)


def weights_from_events(events: list[dict], *, today: Optional[date] = None) -> dict[str, float]:
    """从事件算逐菜权重。"""
    today = today or date.today()
    raw: dict[str, float] = {}
    for e in events:
        rid, action = e.get("recipe_id"), str(e.get("action") or "")
        direction = DIRECTION.get(action)
        if not rid or direction is None:
            continue
        age = _age_days(_when(e), today)
        if age is None or age > DECAY_MAX_DAYS:
            continue
        raw[rid] = raw.get(rid, 0.0) + direction * decay(age)
    return _scaled(raw)


def weights_from_profile(profile: dict, by_name: dict[str, str], *,
                         covered: set[str], today: Optional[date] = None) -> dict[str, float]:
    """档案里那些**还没有事件**的菜补一份（老历史的兜底，见模块开头 ②）。

    `covered`：已经有事件的菜谱 id —— 它们不再吃档案这份，避免重复计算。
    """
    today = today or date.today()
    raw: dict[str, float] = {}

    def bump(name: str, direction: float, since: Optional[date]) -> None:
        rid = by_name.get(name)
        if not rid or rid in covered:
            return
        age = _age_days(since, today)
        if age is None or age > DECAY_MAX_DAYS:
            return
        raw[rid] = raw.get(rid, 0.0) + direction * decay(age)

    history = profile.get("history") or {}
    for name in profile.get("liked_dishes") or []:
        bump(name, DIRECTION[ev.LIKE], _parse_md((history.get(name) or {}).get("since"), today))
    for name in profile.get("disliked_dishes") or []:
        bump(name, DIRECTION[ev.DISLIKE],
             _parse_md((history.get(name) or {}).get("since"), today))
    for name, info in (profile.get("ratings") or {}).items():
        score = int((info or {}).get("score", 1))
        action = {2: ev.RATE_GOOD, 1: ev.RATE_OK, 0: ev.RATE_NEVER}.get(score, ev.RATE_OK)
        bump(name, DIRECTION[action], _parse_md((info or {}).get("date"), today))
    return _scaled(raw)


def _scaled(raw: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for rid, value in raw.items():
        scaled = round(GAIN * value, 2)
        if abs(scaled) < 0.01:
            continue
        out[rid] = max(CLAMP[0], min(CLAMP[1], scaled))
    return out


def dish_weights(*, events: Optional[list[dict]] = None, profile: Optional[dict] = None,
                 by_name: Optional[dict[str, str]] = None,
                 today: Optional[date] = None) -> dict[str, float]:
    """最终的逐菜权重：事件 + 老档案补位。**没有信号时返回空字典**（调用方回退老行为）。"""
    events = ev.recent() if events is None else events
    out = weights_from_events(events, today=today)
    if profile:
        covered = {str(e.get("recipe_id")) for e in events}
        extra = weights_from_profile(profile, by_name or {}, covered=covered, today=today)
        for rid, value in extra.items():
            out[rid] = max(CLAMP[0], min(CLAMP[1], round(out.get(rid, 0.0) + value, 2)))
    return out


def stability(events: list[dict], recipe_id: str) -> dict:
    """这道菜是"每天吃"还是"偶尔"？**只算不用**（docs/12 §4 阶段二 2.4）。

    返回 `{n, interval_days, cv, label}`：间隔的变异系数越小越像习惯。
    样本 <3 次直接标"刚开始"，不硬给结论 —— 理想文档自己也是这么处理的（`len<3 → 0.3`）。
    """
    stamps = sorted(w for w in (_when(e) for e in events
                                if e.get("recipe_id") == recipe_id) if w is not None)
    if len(stamps) < 3:
        return {"n": len(stamps), "interval_days": None, "cv": None, "label": "刚开始"}
    gaps = [(b - a).days for a, b in zip(stamps, stamps[1:]) if (b - a).days >= 0]
    if not gaps:
        return {"n": len(stamps), "interval_days": 0.0, "cv": None, "label": "刚开始"}
    mean = sum(gaps) / len(gaps)
    if mean <= 0:
        return {"n": len(stamps), "interval_days": 0.0, "cv": None, "label": "刚开始"}
    var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
    cv = (var ** 0.5) / mean
    label = "习惯" if cv <= 0.5 else "偶尔"
    return {"n": len(stamps), "interval_days": round(mean, 1), "cv": round(cv, 2), "label": label}


def _when(e: dict) -> Optional[datetime]:
    return ev._when(e)
