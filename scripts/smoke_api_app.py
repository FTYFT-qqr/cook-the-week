"""界面走服务端（`USE_API=1`）时的冒烟：真起一个服务端，走完同一条主链路（docs/09 P1-7）。

和 `smoke_app.py` 的分工：
- `smoke_app.py`：界面直连领域层（`USE_API=0`），165 项，覆盖全部按钮与边界；
- 本脚本：界面**经过 HTTP**（`USE_API=1`），验证"主链路在服务化之后照样能用"：
  空状态 → 排一周（服务端任务 + SSE 进度）→ 今晚 → 本周计划 → 喜欢/不喜欢 →
  买菜清单打勾 → 带走清单，并且每一步都**回服务端确认**状态真的落库了。

这一层能抓到本地模式抓不到的错：DTO 字段漏了、SSE 分帧错了、写完缓存没失效、
服务没起来时的提示是不是人话。所以它不是 `smoke_app.py` 的重复，而是它的补充。
"""
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["TEMP"] = os.environ["TMP"] = os.path.join(ROOT, ".tmp")
os.makedirs(os.environ["TEMP"], exist_ok=True)


def _patch_tempfile_permissions() -> None:
    """本机环境的一个坑，必须在 Streamlit 之前修（与 `smoke_app.py` 同一个问题）。

    `tempfile.mkdtemp` 把权限写死成 0o700，而本机（沙箱令牌）连这样的目录都列不了，
    结果 Streamlit/AppTest 建完就用不了、退出也删不掉，atexit 打一串 PermissionError。
    """
    import tempfile

    def mkdtemp(suffix=None, prefix=None, dir=None):        # noqa: A002 - 与标准库同签名
        prefix, suffix, target, output_type = tempfile._sanitize_params(prefix, suffix, dir)
        for _ in range(tempfile.TMP_MAX):
            name = next(tempfile._get_candidate_names())
            path = os.path.join(target, prefix + name + suffix)
            try:
                os.mkdir(path)
            except FileExistsError:
                continue
            return os.fsencode(path) if output_type is bytes else path
        raise FileExistsError("没有可用的临时目录名")

    tempfile.mkdtemp = mkdtemp


_patch_tempfile_permissions()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"
DB_TMP = os.path.join(ROOT, ".tmp", "smoke_api_app.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_TMP + _suffix):
        os.remove(DB_TMP + _suffix)

os.environ["USE_API"] = "1"                 # 本脚本存在的意义就是这个开关
os.environ["STORAGE"] = "db"
os.environ["API_BASE_URL"] = BASE
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + DB_TMP.replace(os.sep, "/")
os.environ["DEEPSEEK_API_KEY"] = ""         # 确定性路径，不联网
os.environ["RATE_LIMIT"] = "off"            # AppTest 会连点，别被限流打断
os.environ["AUTH_MODE"] = "off"

from recipe_planner.db import _load_json_db                                  # noqa: E402
from recipe_planner.storage import engine, migrate, sync_bridge               # noqa: E402
from recipe_planner.storage.repositories import RecipeRepo                    # noqa: E402

# 用 Alembic 建库（和 `smoke_app.py` 的 DB 模式同一条路径）
migrate.ensure_schema()
sync_bridge.run(RecipeRepo.upsert_many(_load_json_db().recipes))

import uvicorn                                                                # noqa: E402

from recipe_planner.api.main import create_app                                # noqa: E402

_server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=PORT,
                                        log_level="warning", access_log=False,
                                        log_config=None))
_thread = threading.Thread(target=_server.run, name="smoke-uvicorn", daemon=True)
_thread.start()
_deadline = time.monotonic() + 20
while not getattr(_server, "started", False) and time.monotonic() < _deadline:
    time.sleep(0.05)

from streamlit.testing.v1 import AppTest                                      # noqa: E402

from recipe_planner import client                                             # noqa: E402

client.reset()
PASS, FAIL = 0, []
print(f"[服务端] {BASE}   （数据库 {os.path.basename(DB_TMP)}）")


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL.append(name)
        print(f"  ✘ {name} {detail}")


def page_text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [f.value for f in at.success] + [w.value for w in at.warning]
    parts += [i.value for i in at.info] + [e.value for e in at.error]
    parts += [c.value for c in at.caption]
    parts += [t.value for t in at.title] + [h.value for h in at.subheader]
    return "\n".join(str(p) for p in parts)


def btns(at, prefix):
    return [b for b in at.button if (b.key or "").startswith(prefix)]


def nav_to(at, key: str) -> bool:
    found = [b for b in at.button if (b.key or "") == f"nav_{key}"]
    if not found:
        return False
    found[0].click()
    at.run()
    return True


def current_page(at):
    return at.session_state["page"] if "page" in at.session_state else None


def menu_dish_names(at, db) -> set:
    names = set()
    for b in at.button:
        key = b.key or ""
        if key.startswith("like_") or key.startswith("hate_"):
            recipe = db.by_id(key.split("_", 2)[2])
            if recipe:
                names.add(recipe.name)
    return names


print("[1] 服务端在不在 + 守卫不该误报")
check("客户端的探针连得上（界面这一层能用 HTTP 说话）", client.ping() is True)
check("服务端 /ready 正常",
      client._request("GET", "/ready", root=True)["status"] in ("ready", "degraded"))

at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=180)
at.run()
check("首屏无异常", not at.exception, str([str(e.value) for e in at.exception]))
check("没有走进「连不上排菜服务」那页", "连不上排菜服务" not in page_text(at))
check("全新客户默认落在「今晚」页", current_page(at) == "tonight", current_page(at))
check("空状态文案来自服务端（状态④）", "还没有这周的菜单" in page_text(at), page_text(at)[:160])
check("空状态给「帮我排一周」引导卡",
      any((b.key or "") == "tonight_start" for b in at.button))
check("服务端这边确实一份方案都没有", client.list_plans() == [])

print("[2] 排一周：任务提交给服务端，进度由 SSE 推回来")
nav_to(at, "create")
fill = [b for b in at.button if (b.key or "").startswith("scene_btn_")]
check("有三张场景卡", len(fill) == 3)
if fill:
    fill[0].click()
    at.run()
run_btn = [b for b in at.button if (b.label or "").startswith("生成菜单")]
check("有生成菜单按钮", bool(run_btn))
if run_btn:
    run_btn[0].click()
    at.run()
check("生成后无异常", not at.exception, str([str(e.value) for e in at.exception]))
check("首次排完自动落到「今晚」", current_page(at) == "tonight", current_page(at))
check("今晚页有主角大卡", "hero" in page_text(at) and "今晚" in page_text(at),
      page_text(at)[:160])
plans = client.list_plans()
check("服务端上真的存下了这一版（不是界面自己存的）", len(plans) == 1, f"plans={plans}")
record = client.latest_record()
check("这一版有周期标签与菜单", record is not None and bool(record.label)
      and bool(record.result.days), getattr(record, "label", None))
check("「今晚」的状态由服务端给出",
      client.tonight_view(record).state in ("planned", "done", "skipped"),
      client.tonight_view(record).state)
check("界面上的菜名与服务端那一版一致",
      client.tonight_view(record).headline in page_text(at),
      f"server={client.tonight_view(record).headline!r}")
check("阶段进度确实来自服务端（会话里留下了这一版）",
      at.session_state["record_id"] == record.id,
      f"{at.session_state['record_id']} vs {record.id}")
print(f"    服务端的这一版: {record.label} · {len(record.result.days)} 天")

print("[3] 本周计划页：菜单渲染 + 反馈按钮")
db = _load_json_db()
nav_to(at, "plan")
check("本周计划页无异常", not at.exception, str([str(e.value) for e in at.exception]))
check("菜单卡片已渲染", "dish-card" in page_text(at))
check("菜单上有可反馈的菜", len(btns(at, "like_")) > 0)

print("[4] 「喜欢」：写进服务端档案，菜单一行不动")
before_menu = menu_dish_names(at, db)
like_btns = btns(at, "like_")
loved = None
if like_btns:
    rid = (like_btns[0].key or "").split("_", 2)[2]
    loved = db.by_id(rid).name
    like_btns[0].click()
    at.run()
    check("点喜欢后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    profile = client.load_profile()
    check("喜欢已落到服务端档案", loved in profile.get("liked_dishes", []), f"profile={profile}")
    check("来源痕迹也记上了", bool(client.feedback_origin(loved)), client.feedback_origin(loved))
    check("喜欢不改本次菜单", menu_dish_names(at, db) == before_menu)
    print(f"    服务端档案新增喜欢: {loved}")

print("[5] 「不喜欢」：写进档案 + 服务端把这一道换掉")
hate_btns = btns(at, "hate_")
hated = None
if hate_btns:
    rid = (hate_btns[0].key or "").split("_", 2)[2]
    hated = db.by_id(rid).name
    hate_btns[0].click()
    at.run()
    check("点不喜欢后无异常", not at.exception, str([str(e.value) for e in at.exception]))
    check("不喜欢已落到服务端档案",
          hated in client.load_profile().get("disliked_dishes", []))
    server_now = client.get_record(at.session_state["record_id"])
    server_names = set()
    for day in server_now.result.days:
        for dish in day.dishes:
            recipe = db.by_id(dish.recipe_id)
            if recipe:
                server_names.add(recipe.name)
    check("服务端那一版里这道菜已经没了", hated not in server_names, f"server={sorted(server_names)}")
    check("界面上也看不到这道菜了", hated not in menu_dish_names(at, db))
    print(f"    服务端档案新增不喜欢: {hated}")

print("[6] 买菜清单：勾选写回服务端 + 新会话还在")
nav_to(at, "shopping")
check("切到清单页无异常", not at.exception, str([str(e.value) for e in at.exception]))
record_id = at.session_state["record_id"]
need = [s.name for s in at.session_state["result"].shopping if s.needed]
check("清单项已渲染成可勾选条目", len(at.checkbox) >= max(len(need), 1),
      f"checkbox={len(at.checkbox)} need={len(need)}")
if at.checkbox:
    at.checkbox[0].check()
    at.run()
    checked_now = client.get_record(record_id).checked_items
    check("勾选已写回服务端（不是只活在会话里）", len(checked_now) >= 1, f"checked={checked_now}")
    check("页面显示「已买 X / N 样」",
          f"已买 {len(checked_now)} / {len(need)} 样" in page_text(at).replace("**", ""),
          page_text(at)[:160])

    at2 = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=180)
    at2.run()
    check("回访会话首屏无异常", not at2.exception,
          str([str(e.value) for e in at2.exception]))
    check("回访首屏落在「今晚」", current_page(at2) == "tonight",
          f"page={current_page(at2)!r} 文本={page_text(at2)[:120]!r}")
    nav_to(at2, "shopping")
    check("重开一个会话，勾选还在（从服务端读回来的）",
          len([c for c in at2.checkbox if c.value]) == len(checked_now),
          f"{len([c for c in at2.checkbox if c.value])} vs {len(checked_now)}")

print("[7] 带走清单（导出）入口仍在")
exp = [b for b in at.button if (b.key or "") == "export_toggle"]
check("有「带走清单」入口", bool(exp))
if exp:
    exp[0].click()
    at.run()
    try:
        downloads = list(at.get("download_button"))
    except Exception:
        downloads = []
    check("有导出按钮", len(downloads) >= 2, [getattr(d, "label", "") for d in downloads])
    csv_bytes = client._http().get(f"/api/v1/plans/{record_id}/export/csv").content
    check("服务端的 CSV 导出可用", csv_bytes.startswith(b"\xef\xbb\xbf") and len(csv_bytes) > 200,
          f"{len(csv_bytes)} 字节")

print("[8] 服务化特有的路径：服务端没起来时，界面给人话而不是英文栈")
os.environ["API_BASE_URL"] = "http://127.0.0.1:1"
client.reset()
at_dead = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
at_dead.run()
dead_text = page_text(at_dead)


def _values(at, kind) -> str:
    """AppTest 对元素类型的支持随版本变化：取不到就返回空，别让断言假失败。"""
    try:
        return "\n".join(str(getattr(e, "value", "")) for e in at.get(kind))
    except Exception:
        return ""


# 启动命令是用 `st.code` 渲染的，page_text 拿不到它，得单独取
dead_all = dead_text + "\n" + _values(at_dead, "code")
check("连不上时给出人话标题", "连不上排菜服务" in dead_text, dead_text[:160])
check("说清了界面连的是哪个地址", "127.0.0.1:1" in dead_text)
check("给了可以直接抄的启动命令", "uvicorn" in dead_all, dead_all[:200])
check("没有把英文异常名摔在客户脸上", "ConnectError" not in dead_text and "httpx." not in dead_text)
os.environ["API_BASE_URL"] = BASE
client.reset()

print("[9] 清理")
client.delete_record(record_id)
check("删掉这一版之后服务端上没有残留", client.list_plans() == [])

_server.should_exit = True
_thread.join(timeout=20)
sync_bridge.shutdown()
engine.reset_engine()
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB_TMP + _suffix)
    except OSError:
        pass

print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("✅ USE_API=1 主链路通过（服务端任务 + SSE 进度 + 服务端今晚状态 + 回读校验 + 掉线提示）")
