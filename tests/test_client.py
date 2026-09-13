"""服务端客户端（docs/09 P1-7）的测试。

**为什么这里要起一个真的 uvicorn**（临时库 + 随机端口），而不是塞一个假的 transport：
客户端最容易错的恰恰是 HTTP 这一层 —— SSE 怎么分帧、`problem+json` 怎么翻译成人话、
连不上时抛什么、写完缓存有没有失效。假 transport 会把这层整个绕过去，
测试很绿但"真连上去"照样挂。

每个用例一个新库、一个新服务进程（约 1 秒），换来的是用例之间互不影响。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from recipe_planner import client
from recipe_planner.client import ApiUnavailable, ClientError
from recipe_planner.db import _load_json_db
from recipe_planner.models import ChosenDish, DayPlan, PlanRecord, PlanResult, UserConstraints

# 刻意**不用** pytest 的 `tmp_path`：它内部走 `tempfile.mkdtemp`，而本机（沙箱令牌）
# 连 0o700 建出来的目录都列不了，setup 阶段就会 `PermissionError`。
# 仓库里既有的做法是用仓库内的 `.tmp`（见 `tests/api/conftest.py` 与 `scripts/smoke_app.py`）。
TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_started(server, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def live(monkeypatch):
    """一个指向临时库的真服务端，产出**已被指向它**的客户端模块。"""
    port = _free_port()
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    db_file = TMP_ROOT / f"client_{uuid4().hex[:8]}.db"
    monkeypatch.setenv("STORAGE", "db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")       # 确定性路径，不联网、不用等模型
    monkeypatch.setenv("RATE_LIMIT", "off")          # 限流另有专门的测试，这里不掺和
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("API_BASE_URL", f"http://127.0.0.1:{port}")

    import uvicorn

    from recipe_planner.api.main import create_app
    from recipe_planner.storage import engine, sync_bridge
    from recipe_planner.storage.repositories import RecipeRepo

    engine.reset_engine()
    sync_bridge.run(engine.create_all())
    sync_bridge.run(RecipeRepo.upsert_many(_load_json_db().recipes))

    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    if not _wait_started(server):
        raise RuntimeError("测试用的 uvicorn 没起来")

    client.reset()
    try:
        yield client
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        client.reset()
        engine.reset_engine()
        for suffix in ("", "-wal", "-shm"):        # 用完就删，别在 .tmp 里攒库文件
            try:
                os.remove(str(db_file) + suffix)
            except OSError:
                pass


def _constraints(**kw) -> UserConstraints:
    base = dict(people=2, days=3, dishes_per_day=2, cook_start="18:30")
    base.update(kw)
    return UserConstraints(**base)


def _make_plan(live, **kw) -> PlanRecord:
    """跑一次真实排菜任务，返回那一版方案。"""
    from recipe_planner import store

    job = live.PlanJob(_constraints(**kw), None, start_date=store.next_monday()).start()
    assert job.wait(90), f"任务没跑完：stage={job.stage} error={job.error}"
    assert job.error is None, job.error
    assert job.record is not None
    return job.record


# ---------------------------------------------------------------- 读：空状态


def test_空库_first_open(live):
    assert live.ping() is True
    assert live.latest_record() is None
    assert live.load_records() == []
    assert live.previous_record(None) is None

    view = live.tonight_view()
    assert view.state == "no_plan"
    assert view.headline == "先花 20 秒排一周"
    assert view.next_steps, "空状态必须给出可点击的下一步"


def test_连不上时给的是人话(monkeypatch):
    # 用**永不解析**的域名而不是"127.0.0.1:1"：本机上连 1 端口会被接受然后一直不回应
    # （Windows 栈的行为），于是 ping 要等到超时才失败 —— 拿它当"必然拒绝"是错的靶子。
    monkeypatch.setenv("API_BASE_URL", "http://no-such-host.invalid:8000")
    client.reset()
    try:
        assert client.ping() is False
        with pytest.raises(ApiUnavailable) as excinfo:
            client.latest_record()
        message = str(excinfo.value)
        assert "连不上" in message and "USE_API" in message, message
        assert "uvicorn" in message, "要给出能直接抄的命令"
        assert not message.startswith("httpx."), "不能把英文异常名当文案甩给客户"
    finally:
        client.reset()


# ---------------------------------------------------------------- 排菜任务（SSE）


def test_任务_排出一版并推到终态(live):
    record = _make_plan(live)
    assert record.result.days, "排出来的方案不能是空的"
    assert record.id and record.label and record.start_date
    assert live.latest_record().id == record.id
    # SSE 的分帧：至少能看到终态，done 之后任务就该结束
    assert record.result.days[0].dishes, "第一天要有菜"


def test_任务_读得到服务端推的阶段(live):
    from recipe_planner.progress import STAGE_LABELS

    job = live.PlanJob(_constraints(), None, start_date=None).start()
    assert job.wait(90)
    assert job.error is None
    assert job.stages_seen, "SSE 一个阶段都没收到，说明分帧或连接有问题"
    assert set(job.stages_seen) <= set(STAGE_LABELS), f"收到了不认识的阶段：{job.stages_seen}"
    assert job.record is not None
    assert job.result is not None and job.result.days


def test_任务_第二版记得住上一版(live):
    first = _make_plan(live)
    job = live.PlanJob(_constraints(), None, start_date=None).start()
    assert job.wait(90) and job.error is None
    assert job.previous is not None and job.previous.id == first.id, \
        "「上一版」必须在提交任务之前读，否则读到的是刚存下的这一版"
    assert job.record.id != first.id
    assert [r.id for r in live.load_records()] == [job.record.id, first.id], "最新一版排在最前"


def test_取消任务(live):
    job = live.PlanJob(_constraints(days=7), None, start_date=None).start()
    deadline = time.monotonic() + 20
    while job.job_id is None and time.monotonic() < deadline:
        time.sleep(0.05)                       # 等它真的提交上去再取消
    job.cancel()
    assert job.wait(30)
    assert job.done
    assert job.result is None, "取消之后不该有结果"
    assert job.cancelled or job.error, "取消要么走服务端的 cancelled，要么给人话错误"


# ---------------------------------------------------------------- 今晚


def test_今晚_按版本而不是最新(live):
    first = _make_plan(live, days=3)
    second = _make_plan(live, days=5)
    assert live.tonight_view(second).week_label == second.label
    assert live.tonight_view(first).week_label == first.label, \
        "切到以前的某一版之后，今晚页要讲那一版"
    assert live.tonight_view(first).day <= len(first.result.days)


def test_今晚_手动切到第几天(live):
    record = _make_plan(live, days=4)
    view = live.tonight_view(record, day=2)
    assert view.day == 2
    assert "手动切到" in view.hint
    assert view.headline, "切天之后还是要有主角大字"


def test_今晚_做完了会进入已做状态(live):
    record = _make_plan(live)
    day = live.tonight_view(record).day
    assert live.tonight_view(record).state in ("planned", "done")

    live.set_done(record.id, day, True)
    assert live.get_record(record.id).done_days == [day]
    done_view = live.tonight_view(live.get_record(record.id), day=day)
    assert done_view.state == "done"
    assert "已经做过" in done_view.meta

    live.set_done(record.id, day, False)
    assert live.get_record(record.id).done_days == []


def test_今晚_多餐时按顿取且服务端把餐次带回来(live):
    """界面「今天」页一天问三顿 —— 服务端少回一个 `meal`，三张卡就会都以为自己看的是晚餐。"""
    record = _make_plan(live, days=2, dishes_per_meal={"早餐": 1, "午餐": 1, "晚餐": 1},
                        meals=["早餐", "午餐", "晚餐"])
    heads = {m: live.tonight_view(record, day=1, meal=m).headline
             for m in ("早餐", "午餐", "晚餐")}
    assert all(heads.values()), heads
    assert len(set(heads.values())) == 3, f"三顿的主菜串了：{heads}"

    am = live.tonight_view(record, day=1, meal="早餐")
    assert am.meal == "早餐", "服务端必须把 meal 带回来（DTO 少字段就会退回晚餐）"
    assert live.tonight_view(record, day=1, meal="晚餐").meal == "晚餐"
    assert live.tonight_view(record, day=1).meal == "晚餐", "不给 meal 就是当天最后一顿"


def test_今晚_多餐时做完一顿只影响那一顿(live):
    record = _make_plan(live, days=2, dishes_per_meal={"早餐": 1, "午餐": 1, "晚餐": 1},
                        meals=["早餐", "午餐", "晚餐"])
    live.set_done(record.id, 1, True, meal="午餐")
    after = live.get_record(record.id)
    assert after.is_done(1, "午餐")
    assert not after.is_done(1, "早餐") and not after.is_done(1, "晚餐")
    assert live.tonight_view(after, day=1, meal="午餐").state == "done"
    assert live.tonight_view(after, day=1, meal="晚餐").state == "planned"


# ---------------------------------------------------------------- 写：清单 / 整周 / 删除


def test_勾选清单是幂等的(live):
    record = _make_plan(live)
    needed = [s.name for s in record.result.shopping if s.needed][:2]
    assert needed, "这份方案的清单不该是空的"

    live.set_checked(record.id, needed)
    assert sorted(live.get_record(record.id).checked_items) == sorted(needed)
    live.set_checked(record.id, needed)                       # 再来一次结果一样
    assert sorted(live.get_record(record.id).checked_items) == sorted(needed)

    live.set_checked(record.id, [])
    assert live.get_record(record.id).checked_items == []


def test_整周改动只动变了的那些天(live):
    record = _make_plan(live, days=3)
    result = live.get_record(record.id).result
    used = {d.recipe_id for day in result.days for d in day.dishes}
    spare = next(r.id for r in _load_json_db().recipes if r.id not in used)

    before = [[d.recipe_id for d in day.dishes] for day in result.days]
    result.days[0].dishes[-1] = ChosenDish(recipe_id=spare, reason="测试换的")
    live.update_result(record.id, result)

    after = live.get_record(record.id)
    assert [d.recipe_id for d in after.result.days[0].dishes][-1] == spare
    assert [[d.recipe_id for d in day.dishes] for day in after.result.days[1:]] == before[1:], \
        "只改了第 1 天，其他天一个菜都不该动（05 §4 R1）"


def test_删掉一版不影响别的(live):
    first = _make_plan(live)
    second = _make_plan(live)
    live.delete_record(first.id)
    assert live.get_record(first.id) is None
    assert live.get_record(second.id) is not None
    assert [r.id for r in live.load_records()] == [second.id]


def test_服务化模式下不允许本地存档(live):
    with pytest.raises(ClientError) as excinfo:
        live.save_plan(None)
    assert "job.record" in str(excinfo.value), "报错要直接告诉人正确做法"


# ---------------------------------------------------------------- 档案


def test_档案_读写与撤销(live):
    names = [r.name for r in _load_json_db().recipes]
    a, b = names[0], names[1]

    assert live.liked_names() == [] and live.disliked_names() == []
    live.set_feedback(a, "like", set(names), source="测试")
    assert live.liked_names() == [a]
    assert live.feedback_origin(a).get("source") == "测试", "来源痕迹要能带出来"

    snapshot = live.load_profile()
    live.set_feedback(a, "dislike", set(names))      # 改坏：从喜欢挪到不喜欢
    live.bulk_feedback([b], "dislike", set(names))
    assert live.liked_names() == [] and set(live.disliked_names()) == {a, b}

    live.save_profile(snapshot)                     # 撤销：整份恢复
    assert live.liked_names() == [a]
    assert live.disliked_names() == []

    live.clear_all()
    assert live.load_profile().get("liked_dishes") == []


def test_档案_打分与评分一起回来(live):
    names = [r.name for r in _load_json_db().recipes]
    a = names[2]

    before = live.load_profile()
    live.rate(a, 2, set(names), source="测试打分")
    assert live.rating_of(a).get("score") == 2
    assert a in live.liked_names(), "好吃要联动进「喜欢」"

    live.save_profile(before)                       # 撤销打分
    assert live.rating_of(a) == {}, "评分必须一起回滚 —— 只回滚喜欢不回滚评分就是假撤销"
    assert a not in live.liked_names()


# ---------------------------------------------------------------- 纯函数：逐天比对


def _record(days: list[DayPlan]) -> PlanRecord:
    return PlanRecord(id="p1", start_date="2026-09-14", label="9/14–9/20",
                      result=PlanResult(constraints=_constraints(), candidate_count=0,
                                        days=days, final=True))


def test_比对_没变就不发请求():
    days = [DayPlan(day=1, dishes=[ChosenDish(recipe_id="r1")]),
            DayPlan(day=2, dishes=[ChosenDish(recipe_id="r2")])]
    server = _record(days)
    same = _record([d.model_copy(deep=True) for d in days])
    assert client._day_patches(server, same.result) == []


def test_比对_换菜发整组替换():
    server = _record([DayPlan(day=1, dishes=[ChosenDish(recipe_id="r1")])])
    local = _record([DayPlan(day=1, dishes=[ChosenDish(recipe_id="r9")])])
    assert client._day_patches(server, local.result) == [(1, {"op": "replace_day",
                                                             "recipe_ids": ["r9"]})]


def test_比对_只改人数就只发人数():
    server = _record([DayPlan(day=1, dishes=[ChosenDish(recipe_id="r1")])])
    local = _record([DayPlan(day=1, dishes=[ChosenDish(recipe_id="r1")], people=4)])
    assert client._day_patches(server, local.result) == [(1, {"op": "people", "people": 4})]


def test_比对_不做饭与改回来():
    server = _record([DayPlan(day=1, dishes=[ChosenDish(recipe_id="r1")])])
    skipped = _record([DayPlan(day=1, dishes=[], skipped=True)])
    assert client._day_patches(server, skipped.result) == [(1, {"op": "skip"})]
    assert client._day_patches(skipped, server.result) == [(1, {"op": "restore"})]


# ---- docs/10：一天多顿时必须**按 (天, 餐) 比对**，并且发出去的意图要点名那一顿 ----


def _multi(days: list[DayPlan]) -> PlanRecord:
    return PlanRecord(
        id="p2", start_date="2026-09-14", label="9/14–9/20",
        result=PlanResult(
            constraints=UserConstraints(people=2, days=1, dishes_per_day=2,
                                        meals=["早餐", "午餐", "晚餐"],
                                        dishes_per_meal={"早餐": 1, "午餐": 1, "晚餐": 1}),
            candidate_count=0, days=days, final=True))


def test_比对_多餐时按顿比对且带上餐次():
    """把早餐少排一道，只能生成"早餐"那一条 —— 以前会退化成"改晚餐"。"""
    server = _multi([DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r1")]),
                     DayPlan(day=1, meal="午餐", dishes=[ChosenDish(recipe_id="r2")]),
                     DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r3")])])
    local = _multi([DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r9")]),
                    DayPlan(day=1, meal="午餐", dishes=[ChosenDish(recipe_id="r2")]),
                    DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r3")])])
    assert client._day_patches(server, local.result) == [
        (1, {"op": "replace_day", "recipe_ids": ["r9"], "meal": "早餐"})]


def test_比对_多餐时只改一顿的人数():
    server = _multi([DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r1")]),
                     DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r3")])])
    local = _multi([DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r1")]),
                    DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r3")], people=4)])
    assert client._day_patches(server, local.result) == [
        (1, {"op": "people", "people": 4, "meal": "晚餐"})]


def test_比对_多餐时不做饭只关那一顿():
    server = _multi([DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r1")]),
                     DayPlan(day=1, meal="晚餐", dishes=[ChosenDish(recipe_id="r3")])])
    local = _multi([DayPlan(day=1, meal="早餐", dishes=[ChosenDish(recipe_id="r1")]),
                    DayPlan(day=1, meal="晚餐", dishes=[], skipped=True)])
    assert client._day_patches(server, local.result) == [(1, {"op": "skip", "meal": "晚餐"})]


def test_服务端详情转成领域对象时三餐各归各位():
    """界面吃的就是这一份转换结果 —— 它把三餐弄成一份，界面就会显示成一份。"""
    detail = {
        "id": "p9", "label": "9/14–9/20", "start_date": "2026-09-14",
        "constraints": {"people": 2, "days": 1, "meals": ["早餐", "午餐", "晚餐"],
                        "dishes_per_meal": {"早餐": 1, "午餐": 1, "晚餐": 2}},
        "days": [
            {"day": 1, "meal": "早餐", "dishes": [{"recipe_id": "r1"}]},
            {"day": 1, "meal": "午餐", "dishes": [{"recipe_id": "r2"}]},
            {"day": 1, "meal": "晚餐", "dishes": [{"recipe_id": "r3"}, {"recipe_id": "r4"}]},
        ],
        "done_slots": ["1|早餐"],
    }
    rec = client._record_from_detail(detail)

    assert rec.result.constraints.active_meals() == ["早餐", "午餐", "晚餐"]
    assert [p.meal for p in rec.result.slots_for(1)] == ["早餐", "午餐", "晚餐"]
    assert [d.recipe_id for d in rec.result.slot(1, "早餐").dishes] == ["r1"]
    assert [d.recipe_id for d in rec.result.slot(1, "午餐").dishes] == ["r2"]
    assert [d.recipe_id for d in rec.result.slot(1, "晚餐").dishes] == ["r3", "r4"]
    # "哪一顿做完了"必须还原，否则一刷新就丢
    assert rec.is_done(1, "早餐") and not rec.is_done(1, "晚餐")


# ---------------------------------------------------------------- 分派（子进程）


def test_门面在服务的模式下确实换成了客户端():
    """USE_API=1 时 `store` / `profile` 的读写函数要指向客户端实现。

    用子进程而不是 monkeypatch：这次分派发生在**模块导入时**（`store.py` 文件末尾），
    在同一进程里改环境变量再重新导入会污染别的用例（模块缓存 + 别处已经绑好的引用）。
    """
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from recipe_planner import store, profile;"
        "print(store.latest_record.__module__, profile.load_profile.__module__,"
        " store.week_label.__module__)"
    ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, USE_API="1", STORAGE="db")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, timeout=120, check=True).stdout.strip()
    assert out.split() == ["recipe_planner.client", "recipe_planner.client",
                           "recipe_planner.store"], out
