"""扫描 Git 已跟踪文件里的疑似真实凭据。

只读 Git index，不读取 `.env` 或其它未跟踪本地文件；占位符允许出现在
`.env.example` 和文档中。该检查用于防止本地密钥误进提交历史，不能替代
服务商侧的密钥撤销与轮换。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_ASSIGNMENT = re.compile(
    r"(?i)\b(?:deepseek_api_key|openai_api_key|api_key|secret|token)\s*="
    r"\s*([\"'])([^\"']+)\1"
)
DEEPSEEK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
PLACEHOLDER = re.compile(
    r"(?i)^(?:<[^>]+>|x{4,}|sk-x{8,}|your[_-].*|replace[_-].*|"
    r"change[_-].*|example.*)$"
)


def tracked_files() -> list[Path]:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True, capture_output=True,
    )
    return [ROOT / item for item in proc.stdout.decode("utf-8").split("\0") if item]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in KEY_ASSIGNMENT.finditer(line):
                value = match.group(2).strip()
                if value and not PLACEHOLDER.match(value):
                    findings.append(f"{rel}:{line_no} 疑似凭据赋值")
            if DEEPSEEK_TOKEN.search(line) and rel != ".env.example":
                findings.append(f"{rel}:{line_no} 疑似 DeepSeek 密钥")
    if findings:
        print("发现疑似真实凭据，请先移除后再提交：")
        print("\n".join(f"  - {item}" for item in findings))
        return 1
    print(f"[OK] 已扫描 {len(tracked_files())} 个 Git 跟踪文件：未发现疑似真实凭据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
