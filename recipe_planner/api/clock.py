"""接口层「现在几点」的唯一来源（docs/11 §4.1 R4 / P1-6：日期炸弹）。

为什么值得为这个抽一个模块：`tonight_view` 的判定依赖"今天/现在"——
「今天是第几天」「这一周过完了吗」「过了 22:00 先看明天」都拿它比。
而**测试里的种子周是写死的** `2026-09-14`（`tests/api/conftest.py` 的 `START`）。

两者放在一起就是一颗日历炸弹：同一份代码、同一批断言，
过了 2026-09-21 会**集体翻转**（「计划中」变成「这周已结束」），
`tests/api/test_read_routes.py` 里那几处 `state in {...}` / `day in range(1, 4)`
就是被它逼出来的"宽容断言"—— 宽容的断言等于没断言。

所以规矩是：**接口层一律用这里的 `now()` / `today()`**，不要直接写
`datetime.now()`；测试用一个 autouse 夹具把这两个函数钉死，
于是"今天是第几天"在测试里永远是同一件事，断言也就能写死。
"""
from __future__ import annotations

from datetime import date, datetime


def now() -> datetime:
    """现在。测试会把它替换掉（见 `tests/api/conftest.py` 的 `frozen_clock`）。"""
    return datetime.now()


def today() -> date:
    """今天（与 `now()` 同源 —— 两者必须一致，否则会出现"今天"和"现在"不同天的怪状态）。"""
    return date.today()
