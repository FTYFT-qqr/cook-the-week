"""隐性过敏原的护栏（docs/12 §3.5 与 §4 阶段零）。

## 背景是实测，不是推测

`Recipe.all_allergen_names()` 原来只有两层：**显式标注** + **食材名字面匹配**。
而 171 个食材名里，「海鲜 / 坚果 / 大豆 / 麸质 / 芝麻」各出现 **0 次** ——
食材写的是「蚝油」「生抽」「蒸鱼豉油」，永远不会出现「海鲜」二字。
于是这 5 个标签完全依赖 `allergens` 字段被人工填对，而 19/38 道菜该字段为空：
实测「蚝油生菜」（食材含蚝油＝牡蛎）对海鲜过敏的用户**不会被排除** ——
直接违反 `docs/05` 的不变量 R6「忌口永不妥协」。

这组用例守三件事：
1. 实测的那几个漏检案例**现在必须被排除**（正向护栏）；
2. 真正没有过敏原的菜**不能被误伤**（反向护栏 —— 安全优先不等于乱标，误伤一样让用户少一道菜）；
3. 以后**新加**的鱼虾类菜忘了标字段时，测试要能自己发现（`酸菜鱼` 就是这么漏的，
   它不在 docs/12 列的案例里：映射表不敢收光秃秃的「鱼」字，因为会命中「鱼香肉丝」）。
"""
from __future__ import annotations

import pytest

from recipe_planner.allergens import HIDDEN_ALLERGENS
from recipe_planner.core import in_meal_pool, recipe_conflicts, validate_plan
from recipe_planner.db import _load_json_db
from recipe_planner.models import ALLERGENS, ChosenDish, DayPlan, Recipe, RecipeDB, UserConstraints

DB = _load_json_db()
BY_NAME = {r.name: r for r in DB.recipes}


def _c(**kw) -> UserConstraints:
    base = dict(people=2, days=3, max_time_min=180, spice_level="辣")
    base.update(kw)
    return UserConstraints(**base)


def _conflicts(recipe: Recipe, tag: str) -> list[str]:
    return recipe_conflicts(recipe, _c(allergens=[tag]))


# ---------------------------------------------------------------- 正向：漏检必须被堵住

# docs/12 §3.5 ③ 的 5 个案例 + 我自己扫出来的第 6 个（酸菜鱼：草鱼，映射表收不了的）
LEAKS = [
    ("蚝油生菜", "海鲜"),      # 蚝油 = 牡蛎
    ("白灼菜心", "海鲜"),      # 蒸鱼豉油
    ("可乐鸡翅", "大豆"),      # 生抽
    ("土豆炖牛肉", "大豆"),    # 生抽
    ("糖醋里脊", "麸质"),      # 番茄酱（酿造醋/小麦淀粉）
    ("酸菜鱼", "海鲜"),        # 草鱼 —— docs/12 没列到，是扫出来的
]


@pytest.mark.parametrize("dish,tag", LEAKS)
def test_实测漏检的菜现在会被排除(dish, tag):
    r = BY_NAME[dish]
    assert "allergen" in _conflicts(r, tag), \
        f"「{dish}」对「{tag}」过敏又没被排除了（食材：{[i.name for i in r.ingredients]}）"


@pytest.mark.parametrize("dish,tag", LEAKS)
def test_隐性来源也进硬校验(dish, tag):
    """`all_allergen_names` 只有两个调用点：检索过滤 + `validate_plan`。两条都要生效。"""
    r = BY_NAME[dish]
    plans = [DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id=r.id, reason="测试")])]
    issues = validate_plan(plans, DB, _c(allergens=[tag]))
    assert any(i.code == "allergen" for i in issues), [i.message for i in issues]


def test_映射对以后新加的菜也生效():
    """修的是"表"，不是"把这几道菜的数据填对" —— 新菜只写食材也该被拦住。"""
    fresh = Recipe(id="new1", name="清炒芥蓝", category="热菜", time_min=10, cost_yuan=6,
                   ingredients=[{"name": "芥蓝", "amount": "300克", "category": "蔬菜"},
                                {"name": "蚝油", "amount": "1勺", "category": "调料"}])
    assert "allergen" in recipe_conflicts(fresh, _c(allergens=["海鲜"]))
    assert fresh.all_allergen_names() >= {"海鲜"}


# ---------------------------------------------------------------- 反向：不许误伤

CLEAN = ["蒜蓉西兰花", "香菇青菜", "酸辣土豆丝", "清炒油麦菜", "冬瓜排骨汤",
         "山药炒木耳", "手撕包菜", "莲藕排骨汤", "小米南瓜粥"]


@pytest.mark.parametrize("dish", CLEAN)
def test_确实没有过敏原的菜不会被误伤(dish):
    """这 9 道菜确实不含常见过敏原（所以 `allergens` 是空的**正确**值）。

    它们一样是候选池的一部分 —— 映射表写宽一点就会把它们整片打掉，
    而"安全优先"不该变成"随便标"（误伤同样让用户少吃一道菜）。
    """
    r = BY_NAME[dish]
    assert r.all_allergen_names() == set(), sorted(r.all_allergen_names())
    for tag in ALLERGENS:
        assert "allergen" not in _conflicts(r, tag), f"「{dish}」被误标成了「{tag}」"


@pytest.mark.parametrize("name", ["鱼香肉丝", "土豆丝", "玉米面发糕", "荞麦面", "四季豆炒肉"])
def test_含糊的食材名不许当关键词(name):
    """光秃秃的「鱼/豆/面/麦」会把没有该过敏原的菜整片打掉。

    - `鱼` → 鱼香肉丝（没有鱼）；`豆` → 土豆/四季豆（不是大豆）；
    - `面`/`麦` → 玉米面/荞麦（不含麸质）。
    所以映射表里只收「小麦/大麦/麦芽/燕麦/麦片」这类明确写法。
    """
    r = Recipe(id="x", name=name, category="热菜", time_min=10, cost_yuan=5,
               ingredients=[{"name": name, "amount": "1份", "category": "蔬菜"}])
    assert r.all_allergen_names() == set(), f"「{name}」被误判成了 {r.all_allergen_names()}"


def test_花生与坚果是两个标签不互相牵连():
    """`ALLERGENS` 里花生和坚果是分开的：只对花生过敏的人不该连核桃都吃不到。

    这道测试菜名刻意不带水产字（`腰果虾仁` 会因为名字里有「虾」被判成海鲜 —— 那是对的，
    但会盖掉这条用例真正要验的东西）。
    """
    nut = Recipe(id="n1", name="腰果炒西芹", category="热菜", time_min=15, cost_yuan=20,
                 ingredients=[{"name": "腰果", "amount": "50克", "category": "干货"}])
    peanut = Recipe(id="n2", name="老醋花生", category="凉菜", time_min=5, cost_yuan=6,
                    ingredients=[{"name": "花生米", "amount": "100克", "category": "干货"}])
    assert nut.all_allergen_names() == {"坚果"}
    assert peanut.all_allergen_names() == {"花生"}


# ---------------------------------------------------------------- 会自己发现新漏检的护栏

def test_所有水产类食材的菜都必须能被海鲜过敏排除():
    """这条是自动护栏：`酸菜鱼`（草鱼）就是这么漏的 —— 映射表不敢收「鱼」字，
    所以**只能靠字段**，而字段是人工填的、会忘。以后新增鱼虾蟹贝的菜忘了标，这里就红。
    """
    leaks = []
    for r in DB.recipes:
        if not any(i.category == "水产" for i in r.ingredients):
            continue
        if "allergen" not in recipe_conflicts(r, _c(allergens=["海鲜"])):
            leaks.append((r.name, [i.name for i in r.ingredients], r.allergens))
    assert not leaks, f"含水产食材但没标海鲜的菜：{leaks}"


def test_映射表里的标签都是合法过敏原():
    """防手滑写出「海鮮」这种标签 —— 它不会报错，只会安静地永不命中。"""
    assert set(HIDDEN_ALLERGENS) <= set(ALLERGENS), set(HIDDEN_ALLERGENS) - set(ALLERGENS)


def test_映射表不许收会误伤的单字关键词():
    """单字关键词里，有的安全、有的会误伤 —— 这条把区别写死，防止以后随手加宽。

    - 安全：`虾`（虾仁/虾皮/龙虾）、`蟹`（蟹肉/螃蟹）—— 只出现在水产词里；
    - 会误伤：`鱼`（鱼香肉丝没有鱼）、`豆`（土豆/四季豆不是大豆）、
      `面`/`麦`（玉米面/荞麦不含麸质）、`奶`/`蛋`（这个由 `ALLERGENS` 的既有兜底管，不重复收）。
    """
    safe_single = {"虾", "蟹"}
    risky_single = {"鱼", "豆", "面", "麦", "奶", "蛋", "油", "酱"}
    for tag, words in HIDDEN_ALLERGENS.items():
        assert words, f"{tag} 一条关键词都没有"
        for w in words:
            assert w not in risky_single, f"{tag} 收了会误伤的单字「{w}」"
            assert len(w) >= 2 or w in safe_single, f"{tag} 的单字关键词「{w}」不在安全名单里"


def test_早餐池判定不受过敏原改动影响():
    """顺手守一条：早餐池按**食材名含「蛋」**判（`in_meal_pool`），与过敏原标签是两件事。"""
    assert in_meal_pool(BY_NAME["西红柿炒鸡蛋"], "早餐") is True
    assert in_meal_pool(BY_NAME["蒜蓉西兰花"], "早餐") is False
