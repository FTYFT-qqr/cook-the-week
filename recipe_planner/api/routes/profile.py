"""口味档案（docs/08 §6：读写接口；P1-2 先做只读）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from recipe_planner import profile as prof

from ..deps import get_profile
from ..schemas import ProfileOut

router = APIRouter()


@router.get("/profile", response_model=ProfileOut, tags=["profile"])
async def read_profile(profile: dict = Depends(get_profile)) -> ProfileOut:
    """喜欢/不喜欢/评分/来源痕迹 + 指纹（指纹用于判断"偏好变了要不要重排"）。"""
    history = profile.get("history") or {}
    items = [{"name": name, **dict(info or {})} for name, info in history.items()]
    items.sort(key=lambda x: str(x.get("since") or ""), reverse=True)
    return ProfileOut(
        liked_dishes=list(profile.get("liked_dishes") or []),
        disliked_dishes=list(profile.get("disliked_dishes") or []),
        ratings=dict(profile.get("ratings") or {}),
        history=items,
        signature=prof.profile_signature_of(profile))
