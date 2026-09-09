"""临时：真实 LLM 端到端冒烟测试。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recipe_planner.db import load_db
from recipe_planner.graph import run_pipeline
from recipe_planner.models import UserConstraints

db = load_db()

r = run_pipeline(
    UserConstraints(
        people=2, days=3, dishes_per_day=2,
        goal="减脂", taste_tags=["清淡"], allergens=["海鲜"],
        budget_per_person_day=40.0, pantry_items=["鸡蛋", "西红柿"],
        spice_level="不辣",
    ),
    db,
)
print("llm_used:", r.llm_used)
print("llm_error:", r.llm_error)
print("final:", r.final, "| repairs:", r.repairs_used, "| latency:", r.latency_sec, "s")
print("issues:")
for i in r.issues:
    print("  -", i.level, i.message)
print("days:")
for p in r.days:
    names = []
    for d in p.dishes:
        rec = db.by_id(d.recipe_id)
        names.append(f"{rec.name if rec else d.recipe_id}({d.reason[:24]})")
    print("  day", p.day, ":", " | ".join(names))
print("shopping:")
for s in r.shopping:
    mark = "" if s.needed else " [库存已有]"
    print(f"  - {s.name} {s.amount}{mark}")
print("trace:")
for t in r.trace:
    print("  -", t)
