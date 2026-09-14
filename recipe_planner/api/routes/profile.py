"""口味档案的写入接口（docs/08 §6「`/profile` 增删」）。

- `PUT /profile`：批量改（like / dislike / remove）+ 清空单个列表（clear_like / clear_dislike）—— 幂等，同样的请求结果一样；
- `PUT /profile/restore`：整份恢复（**给界面"撤销上一步"用**，不是常规写入路径）；
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
from ..schemas import MutationOut, ProfileOut, ProfilePatchIn, ProfileRestoreIn

router = APIRouter()

_VERB = {"like": "移入「喜欢」", "dislike": "移入「不喜欢」", "remove": "恢复为未表态",
         "clear_like": "清空「喜欢」列表", "clear_dislike": "清空「不喜欢」列表"}
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
    """批量改口味：喜欢 / 不喜欢 / 未表态；也可以一次清空其中一个列表。只动档案，不动任何一周的菜单。"""
    known = {r.name for r in db.recipes}

    if body.op in ("clear_like", "clear_dislike"):
        # 清空不涉及具体菜名：names 允许为空，也不做"菜谱库里有没有这道菜"的校验
        key = "liked_dishes" if body.op == "clear_like" else "disliked_dishes"
        cleared = list(dict.fromkeys(profile.get(key) or []))
        new_profile = prof.apply_feedback_to_dict(profile, "", body.op, known)
        await data.save_profile(new_profile)
        log_id = await data.add_log(None, f"profile_{body.op}",
                                    f"{_VERB[body.op]}（{len(cleared)} 道）",
                                    {"cleared": cleared})
        await _commit()
        # undo_hint=None 是**故意的**：清空会把只属于这个列表的来源痕迹一起丢掉，
        # 编一个"可以撤销"是骗人（要恢复只能重新一道道加回来）。
        return MutationOut(
            kind=f"profile_{body.op}",
            message=f"已{_VERB[body.op]}（{len(cleared)} 道，本周菜单没动）。",
            data={"liked_dishes": new_profile.get("liked_dishes", []),
                  "disliked_dishes": new_profile.get("disliked_dishes", []),
                  "signature": prof.profile_signature_of(new_profile),
                  "cleared": cleared},
            action_log_id=log_id, undo_hint=None,
            next_steps=[{"op": "list_recipes",
                         "label": "重新挑几道喜欢的" if body.op == "clear_like"
                                  else "重新挑几道不喜欢的"}])

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
    # 来源要**照客户端说的记**：界面上的「来源痕迹」显示的是"8/10 在菜单里点的收藏"（05 M5），
    # 写死成"口味档案"会把"这条偏好是在哪来的"整个丢掉。
    source = (body.source or "口味档案").strip()[:20] or "口味档案"
    for name in names:
        new_profile = prof.apply_feedback_to_dict(new_profile, name, body.op, known,
                                                  source=source, toggle=False)
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


@router.put("/profile/restore", response_model=MutationOut, tags=["profile"])
async def restore_profile(body: ProfileRestoreIn,
                          profile: dict = Depends(get_profile)) -> MutationOut:
    """**给「撤销」用的整份恢复接口**：把档案（喜欢/不喜欢/评分/来源痕迹）整份写回去。

    这不是常规写入路径 —— 常规改偏好请用 `PUT /profile`（一次只动一处，还能精确撤销）。
    它只为一件事存在（05 §4 R2）：界面撤销上一步改动时，把上一步的快照原样恢复。
    否则会出现"喜欢回来了、评分没回来"—— 那是一次**假的撤销**，比没有撤销更糟。

    菜谱库里已经没有的名字会被静默跳过（`save_profile` 本来就是整体覆盖 + 跳过未知菜），
    所以快照里带了库里没有的菜也不会写进不存在的菜。这里以**库里真正写进去的**为准回话。
    """
    snapshot = dict(profile)                      # 保留 customer_name 等其它键
    snapshot.update(liked_dishes=list(body.liked_dishes),
                    disliked_dishes=list(body.disliked_dishes),
                    ratings=dict(body.ratings), history=dict(body.history))
    await data.save_profile(snapshot)
    restored = await data.load_profile()
    log_id = await data.add_log(None, "profile_restore", "已把口味档案恢复到上一步",
                                {"liked": len(restored.get("liked_dishes") or []),
                                 "disliked": len(restored.get("disliked_dishes") or []),
                                 "ratings": len(restored.get("ratings") or {})})
    await _commit()
    # undo_hint=None 是**故意的**：它本身就是撤销，"再撤销一次"没有意义，给个假的只会误导
    return MutationOut(kind="profile_restore",
                       message="已把口味档案恢复到上一步"
                               "（喜欢、不喜欢、评分和来源痕迹都回来了）。",
                       data={"liked_dishes": list(restored.get("liked_dishes") or []),
                             "disliked_dishes": list(restored.get("disliked_dishes") or []),
                             "signature": prof.profile_signature_of(restored)},
                       action_log_id=log_id, undo_hint=None)


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
    """提交本次请求（与 `plan_mutations._commit` 同一件事，实现在 `async_adapters.commit`）。"""
    await data.commit()


@router.get("/profile/export", response_model=ProfileOut, tags=["profile"])
async def export_profile(profile: dict = Depends(get_profile)) -> ProfileOut:
    """导出档案（I3）：可以直接存成 JSON 文件带走。"""
    return _profile_out(profile)
