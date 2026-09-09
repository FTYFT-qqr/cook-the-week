"""临时验证：菜单上点 🚫 不喜欢 → 重排 → 该菜不得再出现（确定性路径）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DEEPSEEK_API_KEY"] = ""  # 用确定性兜底路径，快且稳

from recipe_planner.db import load_db
from recipe_planner.graph import run_pipeline
from recipe_planner.models import UserConstraints

db = load_db()

base = dict(people=2, days=4, dishes_per_day=2, goal="随便", taste_tags=["下饭"],
            budget_per_person_day=40.0, spice_level="不辣")

r1 = run_pipeline(UserConstraints(**base), db)
used1 = [d.recipe_id for p in r1.days for d in p.dishes]
print("第一版菜品:", [db.by_id(i).name for i in used1])
assert r1.final and len(used1) == 8

# 模拟客户点第一道菜 🚫
victim = used1[0]
print(f"客户点 🚫 不喜欢: {db.by_id(victim).name} ({victim})")

r2 = run_pipeline(UserConstraints(**base, disliked_dishes=[victim]), db)
used2 = [d.recipe_id for p in r2.days for d in p.dishes]
print("重排后菜品:", [db.by_id(i).name for i in used2])
assert victim not in used2, "❌ 被排除的菜仍在菜单里！"
assert r2.final and len(used2) == 8, "❌ 重排结果不完整"
print("✅ 重排闭环 OK：不喜欢的菜已消失，其余 8 道完整且无硬错误")

# 模拟客户点 ❤️ 喜欢某菜（此前被排除的）→ 互斥：喜欢会取消其「不喜欢」
love = victim  # 客户对刚才不喜欢的菜改主意 → 点 ❤️
disliked_final = [d for d in [victim] if d != love]  # UI 互斥逻辑
r3 = run_pipeline(UserConstraints(**base, disliked_dishes=disliked_final, liked_dishes=[love]), db)
used3 = [d.recipe_id for p in r3.days for d in p.dishes]
print(f"客户改主意点 ❤️ 喜欢: {db.by_id(love).name} ({love})")
print("再次重排菜品:", [db.by_id(i).name for i in used3])
assert love in used3, f"❌ 喜欢的菜 {love} 未被安排"
print("✅ 改主意后，喜欢的菜回到菜单")
print("trace:")
for t in r3.trace:
    print("  -", t)
