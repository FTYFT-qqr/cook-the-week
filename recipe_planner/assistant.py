"""把客户的一句人话，变成对菜单的一次具体改动。

依据第一篇 E2（多轮说人话改需求）与第二篇 E-03（临时情况接得住）、E-05（省钱提示）、E-02（定住 / 加一道）、E1（为什么重排 / 为什么没动）。

设计要点：
- **两条解析路径产出同一个 Intent**：先用关键词规则（快、免费、离线可测），规则听不懂再交给 LLM；
  LLM 不可用不影响功能，只是能听懂的句子少一点。
- **动作实现全是确定性的**：换某天一道、这天不做饭、改成 N 个人、省钱换菜、定住某道菜……
  每个动作都返回一句人话回执 —— 客户永远知道"这次为什么动了 / 为什么没动"。
- 本模块不改约束以外的任何状态；重排、存档、撤销由界面层负责。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from recipe_planner.core import (
    candidates_matching,
    cheapest_swap,
    protein_swap,
    recipes_matching,
    refresh_result,
    replace_in_day,
    restore_day,
    skip_day,
    veg_swap,
)
from recipe_planner.models import GOALS, TASTE_TAGS, PlanResult, RecipeDB, UserConstraints

# ---------------------------------------------------------------- 意图

ACTIONS = [
    "swap_day", "skip_day", "restore_day", "set_people", "set_budget", "set_max_time",
    "set_goal", "set_taste", "cheaper", "more_protein", "more_veg",
    "lock_dish", "unlock_dish", "add_dish", "unknown",
]


class Intent:
    """一句话解析出来的意图（故意用轻量对象，方便 LLM / 规则两种来源共用）。"""

    def __init__(self, action: str = "unknown", day: Optional[int] = None,
                 keyword: str = "", value: Optional[float] = None, source: str = "rule"):
        self.action = action if action in ACTIONS else "unknown"
        self.day = day
        self.keyword = keyword
        self.value = value
        self.source = source

    def __repr__(self) -> str:
        return (f"Intent(action={self.action!r}, day={self.day}, "
                f"keyword={self.keyword!r}, value={self.value}, source={self.source!r})")


@dataclass
class ApplyOutcome:
    text: str                                   # 给客户看的一句回执
    replan: bool = False                        # 是否需要整周重排
    inputs_patch: Optional[dict] = None         # 要写回需求表单的字段
    changed_days: list[int] = field(default_factory=list)


# ---------------------------------------------------------------- 规则解析

_DAY_CHARS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
              "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7}
_DAY_RE = re.compile(r"(?:第\s*([一二三四五六日天1-7])\s*天)"
                     r"|(?:周\s*([一二三四五六日天1-7]))"
                     r"|(?:星期\s*([一二三四五六日天1-7]))")

_STOP_WORDS = [
    "帮我", "麻烦", "请", "把", "的", "这一周", "这周", "本周", "这", "那", "周", "星期", "第", "天",
    "换成", "换掉", "换", "改掉", "改成", "改", "成", "做", "来个", "来一道", "来", "加一道", "加个", "加",
    "个", "道", "菜", "吧", "呢", "啊", "了", "今天", "明天", "晚上", "晚饭", "饭", "别", "不要", "太",
    "一点", "点", "有点", "可以", "能", "能不能", "是不是", "就", "还", "再", "其他", "别的", "别的菜",
    "定住", "锁住", "锁", "保留", "取消", "不用", "想吃", "我想要", "我要", "想", "要", "吃", "一下",
    "顿", "这顿", "晚餐", "清淡", "菜谱", "最近", "以后",
]


def _find_day(text: str, days: int, today_idx: Optional[int]) -> Optional[int]:
    m = _DAY_RE.search(text)
    if m:
        ch = next((g for g in m.groups() if g), None)
        if ch:
            d = _DAY_CHARS.get(ch)
            if d is not None and 1 <= d <= days:
                return d
    if today_idx is not None and 0 <= today_idx < days:
        if "今天" in text or "今晚" in text or "今儿" in text:
            return today_idx + 1
        if "明天" in text and today_idx + 2 <= days:
            return today_idx + 2
    return None


def _extract_keyword(text: str) -> str:
    """把一句话里"要换成什么"捞出来（去掉星期、动词、语气词）。"""
    t = _DAY_RE.sub("", text)
    t = re.sub(r"第\s*[一二三四五六日天1-7]\s*天", "", t)
    for w in _STOP_WORDS:
        t = t.replace(w, "")
    t = re.sub(r"[，。！？、,.!?\s：:；;'\"“”‘’（）()]", "", t)
    return t.strip()


def parse_rules(text: str, db: RecipeDB, c: UserConstraints, days: int,
                today_idx: Optional[int] = None) -> Intent:
    """关键词规则解析：覆盖日常说法，快速且离线可用。"""
    t = (text or "").strip()
    if not t:
        return Intent()
    day = _find_day(t, days, today_idx)
    kw = _extract_keyword(t)

    def mk(action: str, **kw_) -> Intent:
        return Intent(action, day=kw_.get("day", day), keyword=kw_.get("keyword", kw),
                      value=kw_.get("value"), source="rule")

    # ① 不做饭 / 恢复做饭
    if re.search(r"(不做饭|别做饭|不用做|不做|不烧|不做菜|外卖|出去吃|休息)", t):
        return mk("skip_day") if day else Intent()
    if re.search(r"(还是要做|照常做|恢复做饭|做起来)", t):
        return mk("restore_day") if day else Intent()

    # ② 人数 / 预算 / 时长
    m = re.search(r"(\d+)\s*(?:个)?\s*人", t)
    if m:
        return mk("set_people", value=float(m.group(1)))
    if "预算" in t:
        m = re.search(r"(\d+(?:\.\d+)?)", t)
        if m:
            return mk("set_budget", value=float(m.group(1)))
    if re.search(r"(时长|耗时|分钟|做菜时间|快手)", t):
        m = re.search(r"(\d+)\s*分钟", t)
        if m:
            return mk("set_max_time", value=float(m.group(1)))
        if re.search(r"(快一点|快些|简单点|没时间|来不及)", t):
            return mk("set_max_time", value=float(max(10, c.max_time_min - 10)))

    # ③ 省钱 / 荤素
    if re.search(r"(省钱|便宜|省点|超支|超预算|太贵|降一点|省一省)", t):
        return mk("cheaper")
    if re.search(r"(太素|没肉|别太素|荤一点|来点肉|加点肉|都是菜)", t):
        return mk("more_protein")
    if re.search(r"(太油|太腻|油大|别太荤|素一点|清爽点)", t):
        return mk("more_veg")

    # ④ 目标 / 口味（说的是标准词才认）
    for g in GOALS:
        if g != "随便" and g in t:
            return mk("set_goal", keyword=g)
    for tag in TASTE_TAGS:
        if tag in t:
            return mk("set_taste", keyword=tag)

    # ⑤ 定住 / 加一道
    if re.search(r"(定住|锁住|锁|不要换|别换|保留|以后都要|必须有)", t):
        return mk("lock_dish") if kw else Intent()
    if re.search(r"(取消定住|不用定|可以换|解锁)", t):
        return mk("unlock_dish") if kw else Intent()
    if re.search(r"(加一道|加个|添一道|来个|想吃|加上)", t):
        return mk("add_dish") if kw else Intent()

    # ⑥ 换菜（指定了星期 → 换那一天；只说菜名 → 由界面按"这道菜现在在哪天"处理）
    if kw and re.search(r"(换|改|不要|吃不|换个)", t):
        return mk("swap_day")
    if day and re.search(r"(全换|都换|换一整天|换掉这)", t):
        return mk("swap_day")
    return Intent()


# ---------------------------------------------------------------- LLM 解析（规则听不懂时兜底）

_LLM_PROMPT = """客户在用一句话改他的每周晚餐菜单。请把这句话解析成 JSON 意图，只输出 JSON。

可用动作：
- swap_day：换某一天的一道菜（day 必填；keyword 是要换成什么，如"鱼"）
- skip_day：某天不做饭（day 必填）
- restore_day：某天恢复做饭（day 必填）
- set_people：改人数（value=人数）
- set_budget：改预算（value=元/人/天）
- set_max_time：改单菜耗时上限（value=分钟）
- set_goal：改目标（keyword 取：{goals}）
- set_taste：改口味偏好（keyword 取：{tastes}）
- cheaper：想省钱、超预算了
- more_protein：这周太素、想吃肉
- more_veg：太油腻、想吃清淡点
- lock_dish / unlock_dish：定住 / 取消定住某道菜（keyword=菜名）
- add_dish：想加一道菜（keyword=菜名）
- unknown：听不懂

上下文：这一周共 {days} 天，当前菜单是：
{menu}

输出格式（不要解释）：{{"action": "...", "day": 1, "keyword": "", "value": 0}}"""


def build_llm_prompt(text: str, db: RecipeDB, c: UserConstraints, result: Optional[PlanResult],
                     days: int) -> str:
    menu_lines = []
    if result is not None:
        for p in result.days:
            names = [db.by_id(d.recipe_id).name if db.by_id(d.recipe_id) else d.recipe_id
                     for d in p.dishes]
            menu_lines.append(f"第{p.day}天：{'、'.join(names) or '（不做饭）'}")
    return _LLM_PROMPT.format(goals="、".join(GOALS), tastes="、".join(TASTE_TAGS),
                              days=days, menu="\n".join(menu_lines) or "（还没有菜单）")


def parse_llm(text: str, db: RecipeDB, c: UserConstraints, result: Optional[PlanResult],
              days: int) -> Optional[Intent]:
    """用 LLM 解析；任何异常/不合规都返回 None，由规则结果兜底。"""
    try:
        from recipe_planner.llm_planner import _client, _extract_json

        client = _client()
        resp = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": "你只输出合规 JSON。"},
                {"role": "user", "content": build_llm_prompt(text, db, c, result, days)},
            ],
            temperature=0.1,
            timeout=25,
            max_tokens=256,
        )
        data = _extract_json(resp.choices[0].message.content or "")
        if not isinstance(data, dict):
            return None
        action = str(data.get("action", "unknown"))
        if action not in ACTIONS:
            return None
        day = data.get("day")
        try:
            day = int(day) if day else None
        except (TypeError, ValueError):
            day = None
        if day is not None and not (1 <= day <= days):
            day = None
        value = data.get("value")
        try:
            value = float(value) if value not in (None, "", 0) else None
        except (TypeError, ValueError):
            value = None
        return Intent(action, day=day, keyword=str(data.get("keyword") or "").strip(),
                      value=value, source="llm")
    except Exception:
        return None


def parse_intent(text: str, db: RecipeDB, c: UserConstraints, result: Optional[PlanResult],
                 days: int, today_idx: Optional[int] = None, use_llm: bool = True) -> Intent:
    """先规则、后 LLM；两条路都产出同一个 Intent。"""
    intent = parse_rules(text, db, c, days, today_idx)
    if intent.action != "unknown":
        return intent
    if use_llm and not intent.keyword:
        llm_intent = parse_llm(text, db, c, result, days)
        if llm_intent is not None:
            return llm_intent
    return intent


# ---------------------------------------------------------------- 落地执行

HINT = ("没太听懂这句话。可以这样跟我说：**周二换成鱼**、**这天不做饭**、"
        "**今天 4 个人吃**、**帮我省点钱**、**太素了**、**把红烧肉定住**。")


def apply_intent(intent: Intent, result: PlanResult, db: RecipeDB,
                 inputs: Optional[dict] = None) -> ApplyOutcome:
    """把一个意图落到菜单上。局部改动就地生效；改约束的返回 replan=True 交给界面重排。"""
    c = result.constraints
    inputs = dict(inputs or {})

    if intent.action == "unknown":
        return ApplyOutcome(HINT)

    if intent.action == "swap_day":
        kw = intent.keyword
        if not kw:
            return ApplyOutcome("你想换成什么？例如「周二换成鱼」。")
        day = intent.day
        if day is None:
            # 没指定天：看这道菜（关键词命中的）现在排在哪天，就换那天
            for p in result.days:
                for d in p.dishes:
                    r = db.by_id(d.recipe_id)
                    if r and kw in r.name:
                        day = p.day
                        break
                if day:
                    break
            day = day or 1
        day_plan = next((p for p in result.days if p.day == day), None)
        if day_plan is None:
            return ApplyOutcome(f"这一周只有 {len(result.days)} 天，没有第 {day} 天。")
        if any(kw in (db.by_id(d.recipe_id).name if db.by_id(d.recipe_id) else "")
               for d in day_plan.dishes):
            return ApplyOutcome(f"第 {day} 天已经有「{kw}」了，想换掉它的话直接点那道菜的「换一道」。")
        if not recipes_matching(db, kw):
            return ApplyOutcome(f"菜谱库里没有和「{kw}」相关的菜，换个说法试试（例如「鱼」「鸡」「豆腐」）。")
        target = day_plan.dishes[-1] if day_plan.dishes else None
        if target is None:
            cands = candidates_matching(result.days, day, kw, db, c)
            if not cands:
                return ApplyOutcome(f"第 {day} 天现在不做饭；想恢复做饭可以直接说「第 {day} 天照常做」。")
            result.days = restore_day(result.days, day, db, c)
            refresh_result(result, db)
            return ApplyOutcome(f"第 {day} 天原本不做饭，已恢复并优先排上「{kw}」。", changed_days=[day])
        cands = candidates_matching(result.days, day, kw, db, c, replace_id=target.recipe_id)
        if not cands:
            return ApplyOutcome(
                f"找不到能换进第 {day} 天的「{kw}」——可能被忌口、单菜时长上限或当天预算挡住了，"
                "可以在下面放宽条件后再试。")
        new_recipe = cands[0]
        old_name = db.by_id(target.recipe_id).name if db.by_id(target.recipe_id) else "那道菜"
        result.days = replace_in_day(result.days, day, target.recipe_id, new_recipe, c)
        refresh_result(result, db)
        return ApplyOutcome(f"已把第 {day} 天的「{old_name}」换成「{new_recipe.name}」"
                            f"（只动这一天）。", changed_days=[day])

    if intent.action == "skip_day":
        day = intent.day
        if day is None:
            return ApplyOutcome("哪天不做饭？例如「周三不做饭」或「今天不做饭」。")
        result.days = skip_day(result.days, day)
        refresh_result(result, db)
        return ApplyOutcome(f"第 {day} 天已设为不做饭：不计花费、买菜清单里也去掉了这天的食材"
                            f"（想改回来就说「第 {day} 天照常做」）。", changed_days=[day])

    if intent.action == "restore_day":
        day = intent.day
        if day is None:
            return ApplyOutcome("哪天要恢复做饭？例如「周三照常做」。")
        result.days = restore_day(result.days, day, db, c)
        refresh_result(result, db)
        names = "、".join(db.by_id(d.recipe_id).name for d in
                          next(p for p in result.days if p.day == day).dishes
                          if db.by_id(d.recipe_id))
        return ApplyOutcome(f"第 {day} 天恢复做饭：{names or '（候选不足，暂时没排上）'}。",
                            changed_days=[day])

    if intent.action == "set_people":
        if not intent.value:
            return ApplyOutcome("改成几个人吃？例如「今天 4 个人吃」。")
        people = int(intent.value)
        if not 1 <= people <= 20:
            return ApplyOutcome("人数要在 1–20 之间。")
        if people == c.people:
            return ApplyOutcome(f"现在就是按 {people} 人算的，没有改动。")
        c.people = people
        inputs["people"] = people
        refresh_result(result, db)      # 份量与花费跟着改，菜单本身不动
        return ApplyOutcome(f"已按 {people} 人重新折算份量与花费（菜单本身没动，"
                            f"这周预计 ¥{result.estimated_cost_yuan:.0f}）。",
                            inputs_patch=inputs)

    if intent.action == "cheaper":
        got = cheapest_swap(result.days, db, c)
        if got is None:
            return ApplyOutcome("这一周已经没有明显更省的换法了（再省就要动忌口或时长了）。")
        new_plans, day, old, new_recipe, saving = got
        result.days = new_plans
        refresh_result(result, db)
        return ApplyOutcome(f"想省钱：把第 {day} 天的「{old.name}」换成「{new_recipe.name}」，"
                            f"这周省了约 ¥{saving:.0f}（现在预计 ¥{result.estimated_cost_yuan:.0f}）。",
                            changed_days=[day])

    if intent.action in ("more_protein", "more_veg"):
        got = protein_swap(result.days, db, c) if intent.action == "more_protein" else \
            veg_swap(result.days, db, c)
        if got is None:
            tip = "每天都已经有荤菜了" if intent.action == "more_protein" else "这一周本来就没有太荤的菜"
            return ApplyOutcome(f"不用调了：{tip}。")
        new_plans, day, old, new_recipe = got
        result.days = new_plans
        refresh_result(result, db)
        word = "加了点肉" if intent.action == "more_protein" else "换清爽些"
        return ApplyOutcome(f"已把第 {day} 天的「{old.name}」换成「{new_recipe.name}」（{word}）。",
                            changed_days=[day])

    if intent.action in ("lock_dish", "unlock_dish", "add_dish"):
        kw = intent.keyword
        if not kw:
            return ApplyOutcome("是哪道菜？例如「把红烧肉定住」。")
        hits = recipes_matching(db, kw)
        if not hits:
            return ApplyOutcome(f"菜谱库里没有和「{kw}」相关的菜，换个说法试试。")
        target = hits[0]
        locked = list(c.must_include_recipes)
        if intent.action == "unlock_dish":
            if target.id not in locked:
                return ApplyOutcome(f"「{target.name}」现在没有被定住。")
            locked = [x for x in locked if x != target.id]
            c.must_include_recipes = locked
            inputs["must_include"] = locked
            return ApplyOutcome(f"已取消定住「{target.name}」，以后重排可以换掉它。", replan=True,
                                inputs_patch=inputs)
        if target.id in locked:
            return ApplyOutcome(f"「{target.name}」已经定住了，每次排菜都会带上它。")
        locked.append(target.id)
        c.must_include_recipes = locked
        inputs["must_include"] = locked
        verb = "定住" if intent.action == "lock_dish" else "加入"
        return ApplyOutcome(f"已{verb}「{target.name}」，正在按你的要求重排一版（会保留它）。",
                            replan=True, inputs_patch=inputs)
    if intent.action == "set_budget":
        if not intent.value or intent.value <= 0:
            return ApplyOutcome("预算改成多少？例如「预算改成 40 元一人一天」。")
        inputs["budget_per_person_day"] = float(intent.value)
        return ApplyOutcome(f"预算改成 ¥{intent.value:.0f}/人·天，正在重排一版"
                            "（会存成新的一版，旧版可以在「更多操作」里找回）。",
                            replan=True, inputs_patch=inputs)

    if intent.action == "set_max_time":
        if not intent.value or intent.value <= 0:
            return ApplyOutcome("时间上限改成多少分钟？例如「每道菜别超过 30 分钟」。")
        inputs["max_time_min"] = int(intent.value)
        return ApplyOutcome(f"单菜时长上限改成 {int(intent.value)} 分钟，正在重排一版"
                            "（旧版可以在「更多操作」里找回）。",
                            replan=True, inputs_patch=inputs)

    if intent.action == "set_goal":
        if intent.keyword not in GOALS or intent.keyword == "随便":
            return ApplyOutcome(f"目标可以选：{'、'.join(g for g in GOALS if g != '随便')}。")
        inputs["goal"] = intent.keyword
        return ApplyOutcome(f"目标改成「{intent.keyword}」，正在重排一版"
                            "（旧版可以在「更多操作」里找回）。",
                            replan=True, inputs_patch=inputs)

    if intent.action == "set_taste":
        if intent.keyword not in TASTE_TAGS:
            return ApplyOutcome(f"口味偏好可以选：{'、'.join(TASTE_TAGS)}。")
        taste = list(inputs.get("taste_tags") or c.taste_tags)
        if intent.keyword not in taste:
            taste.append(intent.keyword)
        inputs["taste_tags"] = taste
        return ApplyOutcome(f"口味偏好加上「{intent.keyword}」，正在重排一版"
                            "（旧版可以在「更多操作」里找回）。",
                            replan=True, inputs_patch=inputs)

    return ApplyOutcome(HINT)
