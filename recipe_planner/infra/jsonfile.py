"""本地 JSON 存档的读写 —— 只做两件事：**原子写** 与 **读失败不静默**（docs/11 §4.1 P0-2）。

这是"文件被写坏"这一类事故的止损层。两种真实事故的后果完全不同：

1. **非原子覆盖写**：原来用 `path.write_text(...)`，它是"先截断、再写"。写到一半
   断电 / 被 kill / 磁盘满，文件就只剩半截 JSON —— 而那一份**原来还在的数据已经没了**。
2. **读失败被当成空存档**：原来用 `except Exception: return []`。于是"读不出来"和
   "本来就没有"变成同一件事，紧接着的保存动作拿**空基**覆盖历史 —— 用户攒下来的东西
   一次全没，而且界面上没有任何提示。

所以这里定两条规矩，`store.py`（方案存档）与 `profile.py`（口味档案）共用：

- **写**：先写**同目录**的临时文件 → `flush` + `os.fsync` → `os.replace` 原子换名。
  同盘 rename 是原子的：读的人要么看到旧文件、要么看到新文件，不会看到半截的。
- **读**：文件**不存在** = 真的还没有（返回 `None`）；文件**存在但读不出来** =
  抛 `ArchiveBroken`，并且把坏文件**改名留档**（`xxx.json.broken-<时间戳>`）——
  原始字节一个都不丢，下一次保存也永远不可能覆盖它。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class ArchiveBroken(RuntimeError):
    """存档文件读不出来。

    **它的含义不是"没有存档"** —— 恰恰相反：文件在，但读不出内容，所以谁都不许
    把它当成空的、更不许拿空内容去覆盖它。界面应该把这条消息原样给用户看。
    """

    def __init__(self, path: Path | str, reason: str, backup: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.backup = Path(backup) if backup else None
        self.reason = reason
        if self.backup is not None:
            tail = f"原始文件已经**原样留档**到 {self.backup}，它一个字都没丢。"
        else:
            tail = f"原文件仍在 {self.path}，我没有动它。"
        super().__init__(f"存档文件读不出来：{self.path}（{reason}）。{tail}"
                         "修好之前我不会覆盖它 —— 请把这句话连同文件名一起告诉我。")


def quarantine(path: Path) -> Optional[Path]:
    """把坏掉的文件改名留档，返回留档后的路径（失败就给 None，但调用方照旧报错）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i in range(100):
        cand = path.with_name(f"{path.name}.broken-{stamp}" + (f"-{i}" if i else ""))
        if cand.exists():
            continue
        try:
            os.replace(path, cand)
            return cand
        except OSError:
            return None
    return None


def read_json(path: Path | str, default: Any = None) -> Any:
    """读一份 JSON。

    - 文件不存在 → `default`（这才是"还没有"）；
    - 文件在、但解析不出来 → 留档 + 抛 `ArchiveBroken`。
    """
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ArchiveBroken(p, f"{type(exc).__name__}: {exc}", quarantine(p)) from exc


def write_json(path: Path | str, payload: Any) -> None:
    """原子写：同目录临时文件 → fsync → `os.replace`。

    中途任何一步失败（包括进程被 kill），目标文件都保持**改动前**的内容，
    不会出现"打开是一半"的存档。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
