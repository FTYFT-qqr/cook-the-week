"""存储层：SQLAlchemy 模型、仓储与 JSON 迁移。

对外只暴露仓储接口（`repositories.py`）与同名适配层（`adapters.py`），
让现有 `db.load_db() / store.* / profile.*` 的签名保持不变（docs/08 §7）。
"""
