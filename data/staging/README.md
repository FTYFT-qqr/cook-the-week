# 菜谱扩库 staging

这里是 C-01 的批次暂存区，不是生产菜谱库。

- `recipes.jsonl`：待发布批次，每行一个结构化菜谱，可带审核辅助字段
- `reviewed/*.json`：人工审核清单；只有 `status=approved` 且 ID 完整匹配时才允许发布
- `scripts/validate_recipe_batch.py`：字段、步骤、分类、克数和隐性过敏原校验
- `scripts/check_recipe_duplicates.py`：与正式库的重复名和高相似度检查
- `scripts/publish_recipe_batch.py`：审核后才合并到 `data/recipes.json`，不会直接写用户数据库

发布顺序：先校验，再重复检查，再人工审核，最后执行发布脚本并跑完整 `scripts/verify.ps1`。
