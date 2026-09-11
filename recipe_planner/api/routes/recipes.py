"""菜谱库浏览（docs/08 §6：`GET /api/v1/recipes`，口味档案页用）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from recipe_planner.models import Recipe, RecipeDB

from ..deps import get_db, get_profile
from ..schemas import IngredientOut, RecipeListOut, RecipeOut

router = APIRouter()

MAX_LIMIT = 200


def _matches(recipe: Recipe, q: str, category: str) -> bool:
    if category and recipe.category != category:
        return False
    if q:
        haystack = " ".join([recipe.name, recipe.description, *recipe.taste_tags,
                             *recipe.goal_tags])
        if q not in haystack:
            return False
    return True


def _to_out(recipe: Recipe, liked: set[str], disliked: set[str]) -> RecipeOut:
    return RecipeOut(
        id=recipe.id, name=recipe.name, category=recipe.category,
        description=recipe.description, difficulty=recipe.difficulty,
        time_min=recipe.time_min, cost_yuan=recipe.cost_yuan,
        calories=recipe.calories, protein_g=recipe.protein_g,
        spice_level=recipe.spice_level, taste_tags=recipe.taste_tags,
        goal_tags=recipe.goal_tags, allergens=recipe.allergens,
        ingredients=[IngredientOut(name=i.name, amount=i.amount, category=i.category)
                     for i in recipe.ingredients],
        liked=recipe.name in liked, disliked=recipe.name in disliked)


@router.get("/recipes", response_model=RecipeListOut, tags=["recipes"])
async def list_recipes(
    q: str = Query("", description="按菜名/描述/口味标签模糊匹配"),
    category: str = Query("", description="热菜/凉菜/汤/主食"),
    liked: str = Query("all", pattern="^(all|true|false)$",
                       description="true=只看喜欢的；false=只看还没标喜欢的"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    cursor: int = Query(0, ge=0, description="上一页返回的 next_cursor"),
    db: RecipeDB = Depends(get_db),
    profile: dict = Depends(get_profile),
) -> RecipeListOut:
    liked_set = set(profile.get("liked_dishes") or [])
    disliked_set = set(profile.get("disliked_dishes") or [])
    items = [r for r in db.recipes if _matches(r, q.strip(), category.strip())]
    if liked == "true":
        items = [r for r in items if r.name in liked_set]
    elif liked == "false":
        items = [r for r in items if r.name not in liked_set]
    total = len(items)
    page = items[cursor:cursor + limit]
    next_cursor = cursor + limit if cursor + limit < total else None
    return RecipeListOut(items=[_to_out(r, liked_set, disliked_set) for r in page],
                         total=total, next_cursor=next_cursor)
