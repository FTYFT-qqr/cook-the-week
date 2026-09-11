"""口味档案的写入接口（docs/08 §6「`/profile` 增删」）。

- `PUT /profile`：批量改（like / dislike / remove）—— 幂等，同样的请求结果一样；
- `POST /profile/clear?confirm=true`：清空（**必须二次确认**，缺 confirm → 409，05 §4 E3）；
- `GET /profile/export`：导出档案 JSON（I3 导出/隐私）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from recipe_planner import profile as prof
from recipe_planner.models import RecipeDB
from recipe_planner.storage import async_adapters as data

from ..deps import get_db, get_profile
from ..errors import ConfirmRequiredError, InvalidRequestError
from ..schemas import MutationOut, ProfileOut, ProfilePatchIn

router = APIRouter()

_VERB = {"like": "移入「喜欢」", "dislike": "移入「不喜欢」", "remove": "恢复为未表态"}
_PATH = "/api/v1/profile"


def _previous_op(profile: dict, name: str) -> str:
    """这道菜在改动**之前**是什么状态 —— 撤销要恢复的就是它。"""
    if name in set(profile.get("liked_dishes") or []):
        return "like"
    if name in set(profile.get("disliked_dishes") or []):
        return "dislike"
    return "remove"


def _undo_for(profile: dict, names: list[str]) -> dict:
    """构造精确撤销：把每一道恢复成改动前的状态（按需要恢复的状态分组）。"""
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(_previous_op(profile, name), []).append(name)
    steps = [{"method": "PUT", "path": _PATH, "body": {"op": op, "names": group}}
             for op, group in groups.items()]
    hint: dict = {"label": "撤销"}
    if len(steps) == 1:
        hint.update(steps[0])
    else:
        hint["steps"] = steps          # 状态不一致时一步说不清，就把步骤列出来
    return hint


def _profile_out(profile: dict) -> ProfileOut:
    history = profile.get("history") or {}
    items = [{"name": name, **dict(info or {})} for name, info in history.items()]
    items.sort(key=lambda x: str(x.get("since") or ""), reverse=True)
    return ProfileOut(liked_dishes=list(profile.get("liked_dishes") or []),
                      disliked_dishes=list(profile.get("disliked_dishes") or []),
                      ratings=dict(profile.get("ratings") or {}),
                      history=items, signature=prof.profile_signature_of(profile))


@router.get("/profile", response_model=ProfileOut, tags=["profile"])
async def read_profile(profile: dict = Depends(get_profile)) -> ProfileOut:
    """喜欢/不喜欢/评分/来源痕迹 + 指纹（指纹用于判断"偏好变了要不要重排"）。"""
    return _profile_out(profile)


@router.put("/profile", response_model=MutationOut, tags=["profile"])
async def patch_profile(body: ProfilePatchIn, db: RecipeDB = Depends(get_db),
                        profile: dict = Depends(get_profile)) -> MutationOut:
    """批量改口味：喜欢 / 不喜欢 / 未表态。只动档案，不动任何一周的菜单。"""
    known = {r.name for r in db.recipes}
    names = list(dict.fromkeys(body.names))
    if not names:
        raise InvalidRequestError("要告诉我改哪几道菜。",
                                  next_steps=[{"op": "list_recipes", "label": "看看菜谱库"}])
    unknown = [n for n in names if n not in known]
    if unknown:
        raise InvalidRequestError(f"菜谱库里没有「{'、'.join(unknown)}」。",
                                  next_steps=[{"op": "list_recipes", "label": "看看菜谱库"}])

    undo_hint = _undo_for(profile, names)
    new_profile = profile
    for name in names:
        new_profile = prof.apply_feedback_to_dict(new_profile, name, body.op, known,
                                                  source="口味档案", toggle=False)
    await data.save_profile(new_profile)
    log_id = await data.add_log(None, f"profile_{body.op}",
                               f"已把{'、'.join(names)} {_VERB[body.op]}", {"names": names})
    await _commit()

    return MutationOut(
        kind=f"profile_{body.op}",
        message=f"已把{'、'.join(names)} {_VERB[body.op]}（本周菜单没动）。",
        data={"liked_dishes": new_profile.get("liked_dishes", []),
              "disliked_dishes": new_profile.get("disliked_dishes", []),
              "signature": prof.profile_signature_of(new_profile)},
        action_log_id=log_id, undo_hint=undo_hint)


@router.post("/profile/clear", response_model=MutationOut, tags=["profile"],
             responses={409: {"description": "缺 confirm=true"}})
async def clear_profile(confirm: bool = Query(False, description="必须显式传 true")):
    """清空档案（喜欢/不喜欢/评分/来源痕迹全清）。**破坏性操作，必须二次确认。**"""
    if not confirm:
        raise ConfirmRequiredError(
            "清空会把喜欢、不喜欢、评分和来源痕迹一起删掉，而且没法恢复。要清就带上 confirm=true。",
            next_steps=[{"op": "export_profile", "label": "先导出一份档案"},
                        {"op": "cancel", "label": "算了，先不清"}])
    before = await data.load_profile()
    await data.clear_profile()
    log_id = await data.add_log(None, "profile_clear", "已清空口味档案",
                               {"liked": len(before.get("liked_dishes") or []),
                                "disliked": len(before.get("disliked_dishes") or [])})
    await _commit()
    # 撤销线索给 None 是**故意的**：评分和来源痕迹一起没了，给个假的"可以撤销"才是骗人
    return MutationOut(kind="profile_clear",
                       message="已清空口味档案（喜欢、不喜欢、评分、来源痕迹都清了，无法撤销）。",
                       data={"liked_dishes": [], "disliked_dishes": []},
                       action_log_id=log_id, undo_hint=None,
                       next_steps=[{"op": "list_recipes", "label": "重新挑几道喜欢的"}])


async def _commit() -> None:
    from recipe_planner.storage.engine import session_scope

    async with session_scope() as session:
        await session.commit()


@router.get("/profile/export", response_model=ProfileOut, tags=["profile"])
async def export_profile(profile: dict = Depends(get_profile)) -> ProfileOut:
    """导出档案（I3）：可以直接存成 JSON 文件带走。"""
    return _profile_out(profile)
