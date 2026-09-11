"""同步桥：让同步代码（Streamlit / 现有测试）调用 async 仓储。

为什么要这样一个桥：
- 仓储是 async（SQLAlchemy async + aiosqlite）；
- 但界面与现有 163 + 165 项测试都是**同步**的，而且要求"签名不变、界面零改动"（docs/08 §7）；
- 因此用一个常驻事件循环线程承接所有协程：连接始终在同一个 loop 上，不会被反复创建/销毁。

用法：`run(repo.save_plan(...))`。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _thread = threading.Thread(target=_loop.run_forever, name="db-loop", daemon=True)
            _thread.start()
    return _loop


def run(coro: Coroutine[Any, Any, T], timeout: float = 60.0) -> T:
    """在常驻循环里执行协程并等待结果（异常原样抛出）。"""
    if _thread is not None and threading.current_thread() is _thread:
        # 在数据库线程里再提交并等待必然死锁 —— 直接报错，比超时好排查
        raise RuntimeError("sync_bridge.run 不能在数据库线程内调用（仓储内部请直接 await）")
    return asyncio.run_coroutine_threadsafe(coro, get_loop()).result(timeout)


def shutdown() -> None:
    """测试收尾用：停掉循环线程，释放连接。"""
    global _loop, _thread
    with _lock:
        if _loop is not None and not _loop.is_closed():
            _loop.call_soon_threadsafe(_loop.stop)
        _loop = None
        _thread = None
