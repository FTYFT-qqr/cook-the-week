"""内容供给护栏（docs/12 §4 阶段一的验收指标）。

为什么要有这组测试：`docs/12` §3.4 实测出来的结论是"**菜谱不是偏少，是在常见约束下不够用**"——
默认画像只有 20 道正餐候选，遇到海鲜+蛋过敏+新手只剩 10 道（连只排晚餐都不够）。
阶段一按缺口定向扩库之后，这里把三个验收指标钉成护栏：

- 默认画像正餐候选 **≥42**（28 槽 × 1.5）
- 海鲜+蛋过敏 + 新手 + ≤30min 正餐候选 **≥42**
- 蛋过敏画像早餐候选 **≥10**（7 槽 × 1.5）

**这些数字是"够不够用"的判据，不是"菜谱总数"**。所以断言的是候选池，不是
`len(db.recipes)`（那种断言每加一道菜就要改一次，而且改了不代表更好用）。
以后要是有人删菜、或者把时长/辣度改宽，这组测试会先红。
"""
from __future__ import annotations

from collections import Counter

from recipe_planner.core import retrieve_candidates
from recipe_planner.db import _load_json_db
from recipe_planner.models import UserConstraints

DB = _load_json_db()


def _pool(meal: str, **kw) -> list:
    return retrieve_candidates(DB, UserConstraints(people=2, days=7, **kw), meal)


def test_默认画像的正餐候选够撑一周():
    """28 槽 × 1.5 = 42。不够的话"不重样 / 换一道 / 下周不复用"全都做不到。"""
    got = _pool("晚餐", max_time_min=40)
    assert len(got) >= 42, f"默认画像正餐候选只有 {len(got)} 道"


def test_最严常见画像下正餐候选也够():
    """海鲜 + 蛋过敏 + 新手 + ≤30 分钟 —— docs/12 说这是"连只晚餐都不够"的那一档。"""
    got = _pool("晚餐", max_time_min=30, allergens=["海鲜", "蛋"], skill="新手")
    assert len(got) >= 42, f"最严画像正餐候选只有 {len(got)} 道"


def test_蛋过敏的人早餐有得选():
    """7 槽 × 1.5 = 10。早餐池原来 8 道里 5 道含蛋 → 蛋过敏的人早饭几乎无解。"""
    got = _pool("早餐", max_time_min=40, allergens=["蛋"], meals=["早餐", "午餐", "晚餐"])
    assert len(got) >= 10, f"蛋过敏画像早餐候选只有 {len(got)} 道"


def test_中间时长带不是空的():
    """`docs/12` §3.4 的缺口 1：21–40 分钟这一段原来几乎是空的
    （正餐 ≤20min 有 19 道，31–40 只有 2 道）→
    "我今天多给 20 分钟"几乎买不到新选择。"""
    mid = [r for r in DB.recipes if 21 <= r.time_min <= 40 and r.category != "早餐"]
    assert len(mid) >= 20, f"21–40 分钟的正餐只有 {len(mid)} 道"
    # 而且这个带真的能变成候选（不是只存在于文件里）
    got = _pool("晚餐", max_time_min=40)
    assert len([r for r in got if 21 <= r.time_min <= 40]) >= 20


def test_15分钟内无蛋无奶的早餐够用():
    """早餐默认上限 15 分钟，所以"能真的排出来"的无蛋早餐必须是 15 分钟内的。"""
    fast = [r for r in DB.recipes
            if r.category == "早餐" and r.time_min <= 15
            and not (r.all_allergen_names() & {"蛋", "奶"})]
    assert len(fast) >= 10, f"15 分钟内的无蛋无奶早餐只有 {len(fast)} 道：{[r.name for r in fast]}"


def test_无麸质的人也吃得上早餐():
    """`docs/12` 阶段一要求补"无蛋/无奶/无麸质"的早餐（原来 8 道里 5 道含麸质）。"""
    gf = [r for r in DB.recipes if r.category == "早餐" and "麸质" not in r.all_allergen_names()]
    assert len(gf) >= 6, f"无麸质早餐只有 {len(gf)} 道"


def test_菜谱不再有看不出来的过敏原():
    """隐性来源写成字段之后，每道菜的 `allergens` 都应当与 `all_allergen_names()` 一致
    （阶段一新增 62 道时字段是自动算出来的，这条防止以后有人手写漏掉）。"""
    bad = [r.name for r in DB.recipes
           if set(r.allergens) != (r.all_allergen_names() & set(r.allergens))]
    assert not bad, f"这些菜的 allergens 字段与实测不一致：{bad}"


def test_类别词表没有被写歪():
    """食材/菜品类别是**枚举**（清单分批、采购分类、早餐池都按它判），写歪了会静默不生效。"""
    ing_cats = {"蔬菜", "肉蛋", "调料", "水产", "干货", "豆制品", "菌菇", "其他", "主食"}
    dish_cats = {"热菜", "凉菜", "汤", "主食", "早餐"}
    got_ing = {i.category for r in DB.recipes for i in r.ingredients}
    got_dish = {r.category for r in DB.recipes}
    assert got_ing <= ing_cats, got_ing - ing_cats
    assert got_dish <= dish_cats, got_dish - dish_cats


def test_非调料食材都有克数():
    """调料缺克数是虚警（它不进采购清单），但蔬菜/肉蛋缺克数会直接影响清单与份量。"""
    missing = [f"{r.name}:{i.name}" for r in DB.recipes for i in r.ingredients
               if i.category != "调料" and not i.grams]
    assert not missing, f"这些非调料食材没有克数：{missing[:8]}"


def test_菜谱id与名称都不重复():
    ids = [r.id for r in DB.recipes]
    names = [r.name for r in DB.recipes]
    assert len(ids) == len(set(ids)), "id 重复"
    assert len(names) == len(set(names)), "菜名重复"
    counter = Counter(r.category for r in DB.recipes)
    assert counter["早餐"] >= 10 and counter["热菜"] >= 30, counter
