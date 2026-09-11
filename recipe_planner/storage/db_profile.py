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
    return prof_mod.profile_signature_of(load_profile())


def feedback_origin(name: str) -> dict:
    return dict((load_profile().get("history") or {}).get(name) or {})


def rate(name: str, score: int, known: Optional[set[str]] = None,
         source: str = "今晚页") -> dict:
    """打分（E-07）。规则用 profile.apply_rating_to_dict，不在这里重写一遍。"""
    p = prof_mod.apply_rating_to_dict(load_profile(), name, score, known, source)
    save_profile(p)
    return load_profile()
