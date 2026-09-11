"""数据库后端的档案门面：与 `profile.py` 公开函数同名同义。

关键：库里的 preference/rating 表 ↔ 现有 JSON 的**同一个字典形状**
（`liked_dishes/disliked_dishes/history/ratings`），所以界面与测试完全不用改。
"""
from __future__ import annotations

from typing import Optional

from recipe_planner import profile as prof_mod
from recipe_planner.storage import sync_bridge
from recipe_planner.storage.repositories import ProfileRepo

run = sync_bridge.run


def load_profile() -> dict:
    return run(ProfileRepo.load_profile())


def save_profile(profile: dict) -> None:
    run(ProfileRepo.save_profile(profile))


def _clean(names: list[str], known: Optional[set[str]]) -> list[str]:
    return prof_mod._clean(names, known) if known else list(dict.fromkeys(names))


def liked_names(known: Optional[set[str]] = None) -> list[str]:
    return _clean(load_profile().get("liked_dishes", []), known)


def disliked_names(known: Optional[set[str]] = None) -> list[str]:
    return _clean(load_profile().get("disliked_dishes", []), known)


def set_feedback(name: str, action: str, known: Optional[set[str]] = None,
                 source: str = "口味档案") -> dict:
    updated = prof_mod.apply_feedback_to_dict(load_profile(), name, action, known, source)
    save_profile(updated)
    return updated


def bulk_feedback(names: list[str], action: str, known: Optional[set[str]] = None,
                  source: str = "口味档案") -> dict:
    p = load_profile()
    for name in names:
        p = prof_mod.apply_feedback_to_dict(p, name, action, known, source, toggle=False)
    save_profile(p)
    return p


def clear_all() -> None:
    run(ProfileRepo.clear_all())


def profile_signature() -> str:
    p = load_profile()
    return "|".join(sorted(p.get("liked_dishes", []))) + "||" + \
        "|".join(sorted(p.get("disliked_dishes", [])))


def feedback_origin(name: str) -> dict:
    return dict((load_profile().get("history") or {}).get(name) or {})


def rate(name: str, score: int, known: Optional[set[str]] = None,
         source: str = "今晚页") -> dict:
    """打分（E-07）。注意：这里**不能**转调 profile.rate —— 那个名字在 db 模式下已被本模块覆盖。"""
    from datetime import date

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
