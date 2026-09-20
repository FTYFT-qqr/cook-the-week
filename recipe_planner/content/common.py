"""菜谱内容批次的读取与校验。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from recipe_planner.models import Recipe


def read_batch(path: Path) -> tuple[str, list[Recipe], list[str]]:
    """读取 JSONL 批次；返回稳定批次号、菜谱和可读错误。"""
    raw_bytes = path.read_bytes()
    batch_id = hashlib.sha256(raw_bytes).hexdigest()[:16]
    recipes: list[Recipe] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return batch_id, [], [f"文件不是 UTF-8：{exc}"]
    entries: list[tuple[int, object]] = []
    stripped = text.lstrip()
    parsed_snapshot = False
    if stripped.startswith("{"):
        try:
            snapshot = json.loads(text)
            raw_entries = snapshot.get("recipes") if isinstance(snapshot, dict) else None
            if isinstance(raw_entries, list):
                parsed_snapshot = True
                entries = [(index, item) for index, item in enumerate(raw_entries, start=1)]
            elif isinstance(snapshot, dict):
                errors.append("JSON 快照必须包含 recipes 数组")
        except json.JSONDecodeError as exc:
            # JSONL 的第一行也是对象；整文件解析出现 Extra data 时，
            # 回退到逐行解析，而不是把合法批次误报成坏快照。
            if "Extra data" not in str(exc):
                errors.append(f"JSON 快照无效：{exc}")
    else:
        for line_no, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                try:
                    entries.append((line_no, json.loads(line)))
                except json.JSONDecodeError as exc:
                    errors.append(f"第 {line_no} 行不是有效 JSON：{exc}")
    if not entries and not errors and not parsed_snapshot:
        for line_no, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                try:
                    entries.append((line_no, json.loads(line)))
                except json.JSONDecodeError as exc:
                    errors.append(f"第 {line_no} 行不是有效 JSON：{exc}")
    for line_no, raw in entries:
        try:
            recipe = Recipe.model_validate(raw)
        except (ValidationError, TypeError) as exc:
            errors.append(f"第 {line_no} 行无效：{exc}")
            continue
        if recipe.id in seen_ids:
            errors.append(f"第 {line_no} 行重复菜谱 ID：{recipe.id}")
            continue
        if recipe.name in seen_names:
            errors.append(f"第 {line_no} 行重复菜名：{recipe.name}")
            continue
        seen_ids.add(recipe.id)
        seen_names.add(recipe.name)
        recipes.append(recipe.model_copy(update={"batch_id": batch_id, "status": "review"}))
    return batch_id, recipes, errors


def report(batch_id: str, recipes: list[Recipe], errors: list[str], *, dry_run: bool) -> dict:
    return {
        "batch_id": batch_id,
        "recipe_count": len(recipes),
        "status": "review",
        "dry_run": dry_run,
        "errors": errors,
    }
