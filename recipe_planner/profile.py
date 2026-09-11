"""客户口味档案：读写 data/customer_profile.json。

独立成模块的原因：
- 菜单页与「口味档案」页共用；
- 反馈（喜欢/不喜欢）是客户资产，需要统一互斥规则与持久化；
- 通过 RECIPE_PROFILE_FILE 环境变量可指向临时文件，便于自动化测试。
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

DEFAULT_PROFILE_FILE = Path(__file__).resolve().parent.parent / "data" / "customer_profile.json"


def profile_path() -> Path:
    return Path(os.environ.get("RECIPE_PROFILE_FILE", str(DEFAULT_PROFILE_FILE)))


def load_profile() -> dict:
    p = profile_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    return {}


def save_profile(profile: dict) -> None:
    p = profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean(names: list[str], known: set[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        if n in known and n not in out:
            out.append(n)
    return out


def liked_names(known: set[str] | None = None) -> list[str]:
    p = load_profile()
    names = p.get("liked_dishes", [])
    return _clean(names, known) if known else list(dict.fromkeys(names))


def disliked_names(known: set[str] | None = None) -> list[str]:
    p = load_profile()
    names = p.get("disliked_dishes", [])
    return _clean(names, known) if known else list(dict.fromkeys(names))


def _stamp(hist: dict, name: str, source: str) -> None:
    """记下这条偏好"什么时候、在哪来的"（05 M5：让列表变成我的记忆）。"""
    hist[name] = {"since": date.today().strftime("%m/%d"), "source": source or "口味档案"}


def apply_feedback_to_dict(p: dict, name: str, action: str,
                           known: set[str] | None = None, source: str = "口味档案",
                           toggle: bool = True) -> dict:
    """纯函数：在档案字典上应用一条反馈（JSON 与数据库两种后端共用，避免两套规则）。"""
    known = known or set()
    hist = dict(p.get("history") or {})
    liked = _clean(p.get("liked_dishes", []), known) if known else list(dict.fromkeys(p.get("liked_dishes", [])))
    disliked = _clean(p.get("disliked_dishes", []), known) if known else list(dict.fromkeys(p.get("disliked_dishes", [])))

    if action == "like":
        if name in liked and toggle:
            liked.remove(name)
            hist.pop(name, None)
        else:
            if name not in liked:
                liked.append(name)
            disliked = [n for n in disliked if n != name]
            _stamp(hist, name, source)
    elif action == "dislike":
        if name in disliked and toggle:
            disliked.remove(name)
            hist.pop(name, None)
        else:
            if name not in disliked:
                disliked.append(name)
            liked = [n for n in liked if n != name]
            _stamp(hist, name, source)
    elif action == "remove":
        liked = [n for n in liked if n != name]
        disliked = [n for n in disliked if n != name]
        hist.pop(name, None)
    elif action == "clear_like":
        liked = []
        hist = {k: v for k, v in hist.items() if k in disliked}
    elif action == "clear_dislike":
        disliked = []
        hist = {k: v for k, v in hist.items() if k in liked}
    else:
        raise ValueError(f"未知 action: {action}")

    p["liked_dishes"] = liked
    p["disliked_dishes"] = disliked
    p["history"] = hist
    p.setdefault("customer_name", "默认客户")
    return p


def set_feedback(name: str, action: str, known: set[str] | None = None,
                 source: str = "口味档案") -> dict:
    """写入一条反馈（判定规则见 apply_feedback_to_dict）。"""
    p = apply_feedback_to_dict(load_profile(), name, action, known, source)
    save_profile(p)
    return p


def feedback_origin(name: str) -> dict:
    """这条偏好的来源痕迹（界面显示成「8/10 在菜单里点的收藏」）。"""
    return dict((load_profile().get("history") or {}).get(name) or {})


RATINGS = {"好吃": 2, "一般": 1, "下次不做": 0}


def apply_rating_to_dict(p: dict, name: str, score: int, known: set[str] | None = None,
                         source: str = "今晚页", today: Optional[date] = None,
                         toggle: bool = True) -> dict:
    """纯函数：把一次打分写进档案字典（ratings + 好吃/下次不做 联动偏好）。

    两种后端与 API 都调这一个，别再各写一份。
    `toggle=True` 沿用界面现在的行为（重复打同一分会在喜欢/不喜欢之间来回切）——
    这个语义有点可疑，已记在 docs/09 待产品负责人定夺，改动前不要单方面改这里。
    """
    p = dict(p)
    ratings = dict(p.get("ratings") or {})
    ratings[name] = {"score": int(score), "date": (today or date.today()).strftime("%m/%d")}
    p["ratings"] = ratings
    if score >= 2:
        p = apply_feedback_to_dict(p, name, "like", known, source, toggle=toggle)
    elif score <= 0:
        p = apply_feedback_to_dict(p, name, "dislike", known, source, toggle=toggle)
    return p


def rate(name: str, score: int, known: set[str] | None = None,
         source: str = "今晚页") -> dict:
    """E-07：做完之后打一分（好吃 / 一般 / 下次不做）。

    好吃 → 记进「喜欢」；下次不做 → 记进「不喜欢」；一般 → 只留记录、不改偏好。
    这样这个产品才开始积累"我家真正的经验"。
    """
    p = apply_rating_to_dict(load_profile(), name, score, known, source)
    save_profile(p)
    return load_profile()


def rating_of(name: str) -> dict:
    return dict((load_profile().get("ratings") or {}).get(name) or {})


def bulk_feedback(names: list[str], action: str, known: set[str] | None = None,
                  source: str = "口味档案") -> dict:
    """批量写入（一次落盘），用于「快速添加」多选场景。"""
    p = load_profile()
    for name in names:
        p = apply_feedback_to_dict(p, name, action, known, source, toggle=False)
    save_profile(p)
    return p


def clear_all() -> None:
    save_profile({"customer_name": load_profile().get("customer_name", "默认客户"),
                  "liked_dishes": [], "disliked_dishes": [], "history": {}, "ratings": {}})


def profile_signature_of(p: dict) -> str:
    """档案指纹的**唯一**算法（纯函数）：菜单页据此判断偏好变了需要重排。

    两种后端（JSON / DB）都必须调这个，不要再各写一遍。
    """
    return "|".join(sorted(p.get("liked_dishes", []))) + "||" + \
        "|".join(sorted(p.get("disliked_dishes", [])))


def profile_signature() -> str:
    return profile_signature_of(load_profile())


# ---------------------------------------------------------------- 后端切换（docs/08 §7）
# STORAGE=db 时用数据库实现覆盖上面的 JSON 实现；`app.py` 与现有测试一行都不用改。
from recipe_planner.infra import settings as _settings  # noqa: E402

if _settings.storage_kind() == "db":  # pragma: no cover - 由环境变量决定
    from recipe_planner.storage.db_profile import (  # noqa: E402,F401,F811
        bulk_feedback, clear_all, disliked_names, feedback_origin, liked_names,
        load_profile, profile_signature, rate, save_profile, set_feedback,
    )
