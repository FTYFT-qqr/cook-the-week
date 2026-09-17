"""发布边界回归：凭据扫描、工作区隔离和对外能力口径。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_跟踪文件凭据扫描通过():
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "scripts/scan_tracked_secrets.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_readme明确_ai不是可用性依赖且迁移与审计分离():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "AI 不是可用性的硬依赖" in readme
    assert "最终理由重建" in readme
    assert "verify_migration.py" in readme and "audit_live_data.py" in readme
    assert "JSON 与数据库内容逐字段等价" not in readme


def test外部交付物有明确忽略边界():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for directory in (".workbuddy/", "output/", "outputs/"):
        assert directory in ignore
