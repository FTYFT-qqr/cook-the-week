"""LLM 排菜节点：调用 DeepSeek（OpenAI 兼容接口）。

设计要点（demo 可靠性）：
- 提示词要求严格 JSON，菜必须从给定候选 id 里选；
- 返回结构用 pydantic 校验，解析/结构不合格 → 返回 None 走确定性兜底；
- 网络/密钥错误一律不抛出，向上层标记 llm_error，保证 demo 永不因 LLM 崩溃。
"""
from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv

from recipe_planner.core import make_reason, retrieve_candidates
from recipe_planner.models import ChosenDish, DayPlan, Recipe, RecipeDB, UserConstraints

load_dotenv()


def _client():
    from openai import OpenAI

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    return OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"))


def _extract_json(text: str) -> dict | None:
    """宽松提取：去代码块围栏，优先取第一个 {...} 平衡块。"""
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _recipe_catalog(candidates: list[Recipe], liked: set[str] | None = None) -> str:
    liked = liked or set()
    lines = ["候选菜谱（必须从中选，只可用 id 引用）："]
    for r in candidates:
        main_ings = "、".join(f"{i.name}{i.amount}" for i in r.ingredients[:4])
        heart = "❤️ " if r.id in liked else ""
        lines.append(
            f"- {r.id} | {heart}{r.name} | {r.category} | {r.time_min}分钟 | 约{r.cost_yuan}元/份 | "
            f"辣度:{r.spice_level} | 口味:{'/'.join(r.taste_tags) or '家常'} | "
            f"目标:{'/'.join(r.goal_tags) or '无'} | 过敏原:{'/'.join(r.allergens) or '无'} | "
            f"主料:{main_ings}"
        )
    return "\n".join(lines)


def build_prompt(c: UserConstraints, candidates: list[Recipe], feedback: str | None = None) -> str:
    tag_text = f"目标：{c.goal}（每顿最好至少有 1 道匹配该目标的菜）" if c.goal != "随便" else "目标：随便（无需特别匹配）"
    taste_text = f"口味偏好（尽量满足）：{'、'.join(c.taste_tags) or '无'}"
    budget_text = (f"预算：每人每天 {c.budget_per_person_day} 元，{c.people} 人，即每天总花费不超过 "
                   f"{c.budget_per_person_day * c.people:.1f} 元（菜谱价格为2人份，按人数折算）"
                   if c.budget_per_person_day is not None else "预算：不限")
    allergens = "、".join(c.allergens) or "无"
    disliked = "、".join(c.disliked_dishes) or "无"

    prompt = f"""你是一位贴心的家庭厨师规划助手。请为一户 {c.people} 人家庭安排 {c.days} 天（每天1顿晚餐）的菜单。

【硬性要求，必须全部满足】
1. 每天恰好选 {c.dishes_per_day} 道菜。
2. 只能使用下方候选菜谱里的 id，禁止编造。
3. 避开过敏原：{allergens}。
4. 辣度不超过：{c.spice_level}；单道菜耗时不超过 {c.max_time_min} 分钟。
5. 一周内不要重复同一道菜；荤素搭配、风格错开，避免连续多天都是同一种主料。
6. 客户明确不喜欢的菜（已从候选中移除）不得出现：{disliked}。

【客户喜好】
- 客户喜欢的菜（下方目录标 ❤️，标注了 id）：**优先安排，且尽量让它们分散在不同天、每周都出现**。
- {tag_text}
- {taste_text}
- {budget_text}

【每天每道菜给出 1 句中文理由（为什么选它，结合当天搭配/软偏好），不超过 40 字。】

{catalog_text(candidates, set(c.liked_dishes))}

请只输出如下 JSON（不要任何解释文字）：
{{"days": [{{"day": 1, "dishes": [{{"recipe_id": "r01", "reason": "..."}}, {{"recipe_id": "r02", "reason": "..."}}]}}, ...]}}
"""
    if feedback:
        prompt += f"\n\n【上一次方案未通过检查，请针对性修正】\n{feedback}"
    return prompt


def catalog_text(candidates: list[Recipe], liked: set[str] | None = None) -> str:
    return _recipe_catalog(candidates, liked)


def _parse_plans(raw: dict, c: UserConstraints) -> list[DayPlan] | None:
    days_raw = raw.get("days")
    if not isinstance(days_raw, list) or len(days_raw) != c.days:
        return None
    try:
        plans = [DayPlan(**d) for d in days_raw]
    except Exception:
        return None
    if {p.day for p in plans} != set(range(1, c.days + 1)):
        return None
    for p in plans:
        if len(p.dishes) != c.dishes_per_day:
            return None
        if any(not d.recipe_id or not d.reason for d in p.dishes):
            return None
    return plans


def llm_plan(c: UserConstraints, candidates: list[Recipe], feedback: str | None = None,
             timeout: int = 60) -> tuple[list[DayPlan] | None, str | None]:
    """返回 (解析通过的每日计划 或 None, 错误说明)。"""
    try:
        client = _client()
        resp = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": "你是食谱规划助手，只输出合规 JSON。"},
                {"role": "user", "content": build_prompt(c, candidates, feedback)},
            ],
            temperature=0.4,
            timeout=timeout,
            max_tokens=2048,
        )
        text = resp.choices[0].message.content or ""
        data = _extract_json(text)
        if data is None:
            return None, "LLM 未返回合法 JSON"
        plans = _parse_plans(data, c)
        if plans is None:
            return None, "LLM JSON 结构与要求不符"
        # 清洗：仅保留候选 id
        valid_ids = {r.id for r in candidates}
        for p in plans:
            p.dishes = [d for d in p.dishes if d.recipe_id in valid_ids]
        return plans, None
    except Exception as exc:  # 网络/鉴权/超时等一律降级
        return None, f"{type(exc).__name__}: {exc}"
