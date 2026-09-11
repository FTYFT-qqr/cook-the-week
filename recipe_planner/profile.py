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


def set_feedback(name: str, action: str, known: set[str] | None = None,
                 source: str = "口味档案") -> dict:
    """写入一条反馈。

    action:
      like          → 加入「喜欢」（若已喜欢则取消）
      dislike       → 加入「不喜欢」（若已不喜欢则取消）
      remove        → 从两个列表中都移除（取消表态）
      clear_like    → 清空「喜欢」
      clear_dislike → 清空「不喜欢」

    互斥：同一道菜不会同时在喜欢与不喜欢里（后写的生效）。
    """
    p = load_profile()
    known = known or set()
    hist = dict(p.get("history") or {})
    liked = _clean(p.get("liked_dishes", []), known) if known else list(dict.fromkeys(p.get("liked_dishes", [])))
    disliked = _clean(p.get("disliked_dishes", []), known) if known else list(dict.fromkeys(p.get("disliked_dishes", [])))

    if action == "like":
        if name in liked:
            liked.remove(name)          # 再点一次 = 取消喜欢
            hist.pop(name, None)
        else:
            liked.append(name)
            disliked = [n for n in disliked if n != name]
            _stamp(hist, name, source)
    elif action == "dislike":
        if name in disliked:
            disliked.remove(name)       # 再点一次 = 取消不喜欢
            hist.pop(name, None)
        else:
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
    save_profile(p)
    return p


def feedback_origin(name: str) -> dict:
    """这条偏好的来源痕迹（界面显示成「8/10 在菜单里点的收藏」）。"""
    return dict((load_profile().get("history") or {}).get(name) or {})


RATINGS = {"好吃": 2, "一般": 1, "下次不做": 0}


def rate(name: str, score: int, known: set[str] | None = None,
         source: str = "今晚页") -> dict:
    """E-07：做完之后打一分（好吃 / 一般 / 下次不做）。

    好吃 → 记进「喜欢」；下次不做 → 记进「不喜欢」；一般 → 只留记录、不改偏好。
    这样这个产品才开始积累"我家真正的经验"。
    """
    p = load_profile()
    ratings = dict(p.get("ratings") or {})
    ratings[name] = {"score": int(score), "date": date.today().strftime("%m/%d")}
    p["ratings"] = ratings
    save_profile(p)
    if score >= 2:
        set_feedback(name, "like", known, source=source)
    elif score <= 0:
        set_feedback(name, "dislike", known, source=source)
    return load_profile()


def rating_of(name: str) -> dict:
    return dict((load_profile().get("ratings") or {}).get(name) or {})


def bulk_feedback(names: list[str], action: str, known: set[str] | None = None) -> dict:
    """批量写入（一次落盘），用于「快速添加」多选场景。"""
    p = load_profile()
    known = known or set()
    liked = _clean(p.get("liked_dishes", []), known) if known else list(dict.fromkeys(p.get("liked_dishes", [])))
    disliked = _clean(p.get("disliked_dishes", []), known) if known else list(dict.fromkeys(p.get("disliked_dishes", [])))

    for name in names:
        if action == "like":
            if name not in liked:
                liked.append(name)
            disliked = [n for n in disliked if n != name]
        elif action == "dislike":
            if name not in disliked:
                disliked.append(name)
            liked = [n for n in liked if n != name]
        elif action == "remove":
            liked = [n for n in liked if n != name]
            disliked = [n for n in disliked if n != name]
        else:
            raise ValueError(f"bulk_feedback 不支持的 action: {action}")

    p["liked_dishes"] = liked
    p["disliked_dishes"] = disliked
    p.setdefault("customer_name", "默认客户")
    save_profile(p)
    return p


def clear_all() -> None:
    save_profile({"customer_name": load_profile().get("customer_name", "默认客户"),
                  "liked_dishes": [], "disliked_dishes": [], "history": {}})


def profile_signature() -> str:
    """档案指纹：菜单页据此判断偏好变了需要重排。"""
    p = load_profile()
    return "|".join(sorted(p.get("liked_dishes", []))) + "||" + "|".join(sorted(p.get("disliked_dishes", [])))
