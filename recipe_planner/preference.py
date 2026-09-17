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
from typing import Iterable, Optional

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
    ev.SELECT: 0.0,        # 仅表示曝光；系统排进菜单不等于用户主动选择
    ev.RATE_OK: 0.0,
    # "做完了"**不给权重**：它是"吃过"的**事实**（给 2.6 的"上次多久没吃"用），
    # 不是偏好表态 —— 做过一顿饭不代表想再做，把它算进排序会让菜单越排越窄。
    ev.DONE: 0.0,
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


# ---------------------------------------------------------------- 「吃过」与轮换（2.6 用户可见）
#
# 2.3–2.5 的权重解决"排什么"，这一段解决"**排过之后记不记得**"：
# 用户要看得到"这道好久没吃了""这道最近常吃"，菜单上也该说一句为什么是它。
#
# **`select` 不算吃过**：把菜排进菜单只是"排了"，还没下锅 ——
# 拿它当"吃了"会让"最近常吃"变成"最近常排"，而算法自己排的菜当然显得常排
# （自我实现：越排越像爱吃）。所以只有**用户自己动作**过的事件才算：
# 点「做完了」、或者给这顿打分（打分本来就发生在做完之后）。
EATEN_ACTIONS = frozenset({ev.DONE, ev.RATE_GOOD, ev.RATE_OK, ev.RATE_NEVER})

RECENT_DAYS = 14    # "最近常吃"看这 14 天
STALE_DAYS = 30     # 超过 30 天没吃 = "好久没吃"
FRESH_DAYS = 14     # 超过 14 天没吃 → 开始轮到它
ROTATION_MAX = 1.5  # 轮换最多加这么多分（**必须小于 2**，见 `rotation_bonus`）
ROTATION_MIN = -3.0 # 刚吃过的短期惩罚；硬约束与永久不喜欢优先级更高
SNOOZE_DAYS = 7     # 临时避开默认有效期，不改变永久偏好档案


def eaten_events(events: Iterable[dict]) -> list[dict]:
    """只留下"确实吃过"的事件（做完了 / 打过分）。"""
    return [e for e in events if str(e.get("action") or "") in EATEN_ACTIONS]


def last_eaten(events: Optional[list[dict]] = None, *,
               today: Optional[date] = None) -> dict[str, int]:
    """每道菜"**上次吃是多少天前**"（没吃过的不在里面）。

    与权重不同，这里**不设时间窗**：一年前吃过的菜正是"好久没吃"最该提醒的对象，
    被窗口截掉就等于这个功能只在有近期记录时才存在。
    """
    today = today or date.today()
    events = ev.load_events() if events is None else events
    out: dict[str, int] = {}
    for e in eaten_events(events):
        rid, when = e.get("recipe_id"), _when(e)
        if not rid or when is None:
            continue
        age = max(0, (today - when.date()).days)
        if rid not in out or age < out[rid]:
            out[rid] = age
    return out


def rotation_adjustment(days_ago: Optional[int]) -> float:
    """按距上次真实食用的天数做轮换调整。

    近期食用会短期扣分，避免连续两周完全复用；较久未吃才给小幅正分。
    轮换调整不是硬约束，明确的过敏/永久不喜欢仍在候选过滤层优先处理。
    """
    if days_ago is None:
        return 0.0
    if days_ago <= 2:
        return ROTATION_MIN
    if days_ago <= 6:
        return -1.5
    if days_ago < FRESH_DAYS:
        return -0.5
    if days_ago < STALE_DAYS:
        return ROTATION_MAX / 2
    return ROTATION_MAX


def rotation_bonus(days_ago: Optional[int]) -> float:
    """兼容旧调用方的别名；新代码请使用 `rotation_adjustment`。"""
    return rotation_adjustment(days_ago)


def active_snoozes(events: Iterable[dict] | None = None, *,
                   today: Optional[date] = None) -> set[str]:
    """返回仍在有效期内的临时避开菜谱 id。

    同一道菜按事件时间顺序解释：最新的 `unsnooze` 可提前恢复，新的 `snooze`
    会重新开始 7 天；没有时间戳的脏事件不激活，避免永久误排除。
    """
    today = today or date.today()
    source = list(ev.load_events() if events is None else events)
    def order_key(pair: tuple[int, dict]) -> tuple[int, str, int]:
        when = _when(pair[1])
        # 统一成字符串排序，兼容 JSON 的 naive 时间和 DB 的带时区时间。
        return (1, "", pair[0]) if when is None else (0, when.isoformat(), pair[0])

    ordered = sorted(enumerate(source), key=order_key)
    state: dict[str, bool] = {}
    for _, item in ordered:
        rid = str(item.get("recipe_id") or "")
        action = str(item.get("action") or "")
        if not rid:
            continue
        if action == ev.UNSNOOZE:
            state[rid] = False
        elif action == ev.SNOOZE:
            age = _age_days(_when(item), today)
            state[rid] = age is not None and age < SNOOZE_DAYS
    return {rid for rid, enabled in state.items() if enabled}


def eating_history(events: Optional[list[dict]] = None, *,
                   today: Optional[date] = None) -> dict[str, dict]:
    """每道菜"吃过几次、上次多久前、最近两周几次、是习惯还是偶尔"。

    稳定度（`stability`）**只在这里展示、不参与排序** —— 一周排一次的场景样本太少
    （见模块开头 ①），所以它只用来给用户一句人话，不用来决定排什么。
    """
    today = today or date.today()
    events = ev.load_events() if events is None else events
    eaten = eaten_events(events)
    out: dict[str, dict] = {}
    for rid in sorted({str(e.get("recipe_id")) for e in eaten if e.get("recipe_id")}):
        mine = [e for e in eaten if str(e.get("recipe_id")) == rid]
        ages = sorted(a for a in ((today - w.date()).days for w in map(_when, mine)
                                  if w is not None) if a >= 0)
        out[rid] = {
            "n": len(ages),
            "last_days": ages[0] if ages else None,
            "recent": sum(1 for a in ages if a <= RECENT_DAYS),
            "stability": stability(mine, rid),
        }
    return out


def buckets(history: dict[str, dict], *, exclude: Iterable[str] = ()) -> dict[str, list[str]]:
    """把吃过的东西分成三堆（**顺序即界面顺序**）：

    - `recent` 最近常吃：近 14 天里吃了 ≥2 次；
    - `new` 刚开始爱吃：只吃过 1–2 次、且是近 14 天内的事；
    - `stale` 好久没吃：上次吃在 30 天以前。

    `exclude`：已经在「不喜欢」里的菜 **不进 `stale`** —— 用户明确说过不要，
    再提醒"好久没吃"就是跟用户顶嘴（`recent`/`new` 不过是事实，留着）。
    """
    skip = set(exclude)
    recent, new, stale = [], [], []
    for rid, h in history.items():
        last = h.get("last_days")
        if last is None:
            continue
        if h.get("recent", 0) >= 2:
            recent.append(rid)
        elif h.get("n", 0) <= 2 and last <= RECENT_DAYS:
            new.append(rid)
        elif last >= STALE_DAYS and rid not in skip:
            stale.append(rid)
    recent.sort(key=lambda r: (-history[r]["recent"], history[r]["last_days"]))
    new.sort(key=lambda r: history[r]["last_days"])
    stale.sort(key=lambda r: -history[r]["last_days"])
    return {"recent": recent, "new": new, "stale": stale}


def planning_signals(*, events: Optional[list[dict]] = None, profile: Optional[dict] = None,
                     by_name: Optional[dict[str, str]] = None,
                     today: Optional[date] = None) -> dict:
    """排菜要用的两样东西**一次读出来**：逐菜权重 + 上次吃是多少天前。

    分开读两遍事件不只是浪费 —— 两次读之间事件还可能变（并发写信），
    同一个方案里"权重按这批事件算、轮换按那批事件算"，出了问题没法复现。
    """
    events = ev.load_events() if events is None else events
    return {
        "dish_weights": dish_weights(events=events, profile=profile, by_name=by_name, today=today),
        "dish_last_seen": last_eaten(events, today=today),
        "snoozed_dishes": sorted(active_snoozes(events, today=today)),
    }
