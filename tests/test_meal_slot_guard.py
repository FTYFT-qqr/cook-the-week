"""源码守卫：不许"按天取一顿"（docs/10 最大的地雷）。

一天多顿之后，`next(p for p in result.days if p.day == day)` 会**静默**拿到当天第一顿（早餐）：

- 它不报错、不返回 None，界面看起来完全正常；
- 于是早/午/晚显示成同一份菜（用户报的 bug 原话："早中晚三餐显示的都是一样的"）；
- 或者"换掉今晚这道"改掉了早上的粥，而回执还写着"第 3 天"。

这个 bug 在 docs/10 里被写进注释警告过一次，仍然在**服务端详情接口**上复发了一次 ——
说明"记得别这么写"靠不住，得让机器盯着。所以这里做一条 AST 守卫：

    next(<生成器，来源是某对象的 .days/.rows，条件里只比较了 .day>)

一律拦下来。真要按天取（"这一天所有顿"），用 `PlanResult.slots_for()` / `core.day_slots()`；
要取某一顿就用 `PlanResult.slot()` / `core.slot_of()` / `core._where`。
极少数确有必要的地方，在那一行加 `# noqa: meal-slot` 并写清理由。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "app.py"] + sorted((ROOT / "recipe_planner").rglob("*.py"))

SOURCES_OF_SLOTS = {"days", "rows"}      # 这两个集合都是"一天一条"（多餐时一天好几条）
NOQA = "noqa: meal-slot"


def _only_day_compared(gen: ast.GeneratorExp) -> bool:
    """条件里比较了 `.day`，却**没有**比较 `.meal` —— 这就是按天取一顿。"""
    attrs: set[str] = set()
    for cond in gen.generators[0].ifs:
        for node in ast.walk(cond):
            if isinstance(node, ast.Attribute):
                attrs.add(node.attr)
    return "day" in attrs and "meal" not in attrs


def _offenders(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    bad: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "next" and node.args):
            continue
        gen = node.args[0]
        if not isinstance(gen, ast.GeneratorExp):
            continue
        src = gen.generators[0].iter
        if not (isinstance(src, ast.Attribute) and src.attr in SOURCES_OF_SLOTS):
            continue
        if not _only_day_compared(gen):
            continue
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        if NOQA in line:
            continue
        bad.append((node.lineno, line.strip()))
    return bad


def test_没有按天取一顿的写法():
    offenders = [(p.relative_to(ROOT).as_posix(), lineno, line)
                 for p in SOURCES if p.exists()
                 for lineno, line in _offenders(p)]
    assert not offenders, (
        "这些地方在按天取一顿（多餐时会静默拿到早餐）：\n"
        + "\n".join(f"  {path}:{lineno}  {line}" for path, lineno, line in offenders)
        + "\n改用 PlanResult.slot(day, meal) / slots_for(day)（必要时加 `# noqa: meal-slot`）")


def test_守卫本身能抓到老写法():
    """守卫得有人看着 —— 拿真出事过的那一行喂给它，必须报出来。"""
    expr = ast.parse("x = next((p for p in record.result.days if p.day == day), None)"
                     "\n").body[0]
    gen = expr.value.args[0]                      # type: ignore[attr-defined]
    assert isinstance(gen, ast.GeneratorExp)
    assert _only_day_compared(gen), "守卫漏掉了服务端详情接口当初的写法"

    ok = ast.parse("x = next((p for p in rows if p.day == day and p.meal == meal), None)"
                   "\n").body[0]
    gen_ok = ok.value.args[0]                     # type: ignore[attr-defined]
    assert not _only_day_compared(gen_ok), "点名了餐次的写法不该被误伤"
