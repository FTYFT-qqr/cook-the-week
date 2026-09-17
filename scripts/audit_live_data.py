"""真实运行数据只读审计。

这不是迁移等价测试：用户继续使用数据库后，SQLite 比迁移前的 JSON 多出新方案、
新偏好是正常现象。本脚本只检查当前数据能否读取、方案引用是否指向已知菜谱，
不会写入、修复或覆盖 `data/`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recipe_planner import db, profile, store


def main() -> int:
    recipes = db.load_db()
    by_id = {r.id for r in recipes.recipes}
    records = store.load_records()
    prof = profile.load_profile()
    unknown: list[str] = []
    for rec in records:
        for slot in rec.result.days:
            for dish in slot.dishes:
                if dish.recipe_id not in by_id:
                    unknown.append(f"方案 {rec.id} 第 {slot.day} 天 {dish.recipe_id}")
    known_names = {r.name for r in recipes.recipes}
    for key in ("liked_dishes", "disliked_dishes"):
        unknown.extend(f"档案 {key}: {name}" for name in prof.get(key, [])
                       if name not in known_names)
    print(f"菜谱 {len(recipes.recipes)} 道；方案 {len(records)} 份；"
          f"喜欢 {len(prof.get('liked_dishes', []))} 道；"
          f"不喜欢 {len(prof.get('disliked_dishes', []))} 道")
    if unknown:
        print("✘ 发现未知引用：")
        print("\n".join(f"  - {item}" for item in unknown))
        return 1
    print("✅ 真实数据只读审计通过（未修改 data/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
