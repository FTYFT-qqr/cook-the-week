"""F-06：真实数据审计在 Windows 非 UTF-8 输出环境下也能退出成功。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_live_audit_gbk_console_is_read_only_and_successful():
    env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="gbk",
               STORAGE="db", USE_API="0",
               DATABASE_URL=f"sqlite+aiosqlite:///{(ROOT / 'data' / 'app.db').as_posix()}")
    result = subprocess.run(
        [sys.executable, "scripts/audit_live_data.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="gbk",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[OK]" in result.stdout
