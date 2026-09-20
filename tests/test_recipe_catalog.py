"""C-02 菜谱数据库主源验收。"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from recipe_planner.models import Recipe, RecipeReference
from recipe_planner.storage import engine as engine_mod
from recipe_planner.storage.orm import Base
from recipe_planner.storage.repositories import RecipeRepo

TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"


@pytest.fixture()
async def catalog_db(monkeypatch):
    path = TMP_ROOT / f"catalog_{uuid4().hex[:8]}.db"
    monkeypatch.setenv("STORAGE", "db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    engine_mod.reset_engine()
    engine = engine_mod.get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield
    finally:
        await engine.dispose()
        engine_mod.reset_engine()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)


def _recipe(recipe_id: str, name: str = "测试菜") -> Recipe:
    return Recipe(id=recipe_id, name=name, time_min=15, cost_yuan=8,
                  ingredients=[{"name": "青菜", "amount": "1把", "category": "蔬菜"}])


async def test_catalog_status_version_hash_and_archive(catalog_db):
    published = _recipe("r-published")
    review = _recipe("r-review", "待审核菜")
    assert await RecipeRepo.upsert_many([published]) == 1
    assert await RecipeRepo.upsert_many([review], status="review", batch_id="batch-1") == 1

    visible = await RecipeRepo.load_db()
    assert [r.id for r in visible.recipes] == ["r-published"]
    all_rows = await RecipeRepo.load_db(published_only=False)
    assert {r.id for r in all_rows.recipes} == {"r-published", "r-review"}

    first = next(r for r in visible.recipes if r.id == "r-published")
    assert first.version == 1 and first.content_hash
    await RecipeRepo.upsert_many([published])
    same = next(r for r in (await RecipeRepo.load_db()).recipes if r.id == "r-published")
    assert same.version == 1

    changed = published.model_copy(update={"description": "内容发生变化"})
    await RecipeRepo.upsert_many([changed])
    changed_row = next(r for r in (await RecipeRepo.load_db()).recipes if r.id == "r-published")
    assert changed_row.version == 2 and changed_row.content_hash != first.content_hash

    assert await RecipeRepo.publish_batch("batch-1") == 1
    assert {r.id for r in (await RecipeRepo.load_db()).recipes} == {"r-published", "r-review"}
    assert await RecipeRepo.archive("r-review", expected_version=1)
    assert {r.id for r in (await RecipeRepo.load_db()).recipes} == {"r-published"}


async def test_catalog_references_and_snapshot(catalog_db):
    recipe = _recipe("r-reference")
    recipe = recipe.model_copy(update={
        "references": [RecipeReference(kind="video", platform="B站",
                                         title="做法", url="https://example.com/video")]
    })
    await RecipeRepo.upsert_many([recipe])
    loaded = (await RecipeRepo.load_db()).recipes[0]
    assert loaded.references[0].platform == "B站"

    snapshot = await RecipeRepo.export_snapshot()
    assert snapshot["schema_version"] == 2
    assert snapshot["recipes"][0]["references"][0]["url"] == "https://example.com/video"
