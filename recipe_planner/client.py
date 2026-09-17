"""服务端客户端（docs/09 P1-7）：`USE_API=1` 时界面走 HTTP，不再直连领域层。

**为什么要有这个文件**：docs/08 决定服务化之后，「数据从哪来」「文案谁产出」必须只有一份实现。
本模块在 `store.py` / `profile.py` 的**文件末尾**覆盖同名函数（和 P0 阶段用数据库实现覆盖
JSON 实现是同一个套路），所以 `app.py` 里读写数据的那几百行一行都不用改。
界面真正需要改的只有两处：进度来源（`PlanJob`）和「今晚」状态判定（见 `tonight_view`）。

**四条设计取舍**

1. **同步**：Streamlit 的脚本本身是同步的，所以这里用 `httpx.Client` 发同步请求，
   而不是把界面改成 async（那会把重跑逻辑、session_state、按钮回调全搅一遍）。
2. **一次 rerun 一次缓存**：同一次 rerun 里 `latest_record()` 会被调好几次。
   缓存的生命周期挂在 `begin_rerun()` 上（`app.py` 每次脚本开头调一次），**不猜 TTL**；
   任何写入都立刻失效整个缓存，所以"写完再读"绝不会读到旧数据。
3. **失败给人话**：连不上 / 4xx / 5xx 一律抛 `ClientError`（带 message 与可点击的 next_steps），
   不让 Streamlit 把英文堆栈直接摔在客户脸上（05 §5.1）。
4. **不重复领域规则**：档案那半边只实现 `load_profile` / `save_profile` 两个 IO 边界，
   `set_feedback` / `rate` / `liked_names` 全部复用 `profile.py` 里的**纯函数** ——
   和 `storage/db_profile.py` 的做法完全一致，判定规则永远只有一份。

**导入时机**：本模块**不在顶层** import `recipe_planner.profile` / `tonight` / `store`。
原因是 `profile.py` 与 `store.py` 会在文件末尾反过来导入本模块（USE_API 分派），
顶层互相 import 会让 Python 拿到一个"半初始化"的模块（属性还不存在就报 ImportError）。
所以这几个 import 都写在函数里 —— 调用时早就加载完了。
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Optional

import httpx

from recipe_planner.infra import settings
from recipe_planner.models import (MEAL, ChosenDish, DayPlan, PlanRecord, PlanResult,
                                   ShoppingItem, UserConstraints, ValidationIssue)

PREFIX = "/api/v1"

# ---------------------------------------------------------------- 错误


class ClientError(RuntimeError):
    """服务端给的人话错误（或者我们替它翻译成人话的错误）。"""

    def __init__(self, message: str, *, status: int = 0, code: str = "",
                 next_steps: Optional[list[dict]] = None, request_id: str = "",
                 detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.next_steps = list(next_steps or [])
        self.request_id = request_id
        self.detail = detail

    def __str__(self) -> str:            # 界面直接把异常转成字符串显示
        return self.message


class ApiUnavailable(ClientError):
    """连不上服务端。

    这个要和"操作被拒绝"分开：前者是**服务没起来**（用户该去看进程），
    后者是**这一步做不了**（用户该点 next_steps）。混在一起会让客户对着
    "没找到这份方案"排查半天，其实是服务端根本没开。
    """


_FALLBACK_MESSAGE = {
    400: "这个请求我没看懂。",
    401: "这个部署需要 API Key 才能访问。",
    403: "你没有权限做这件事。",
    404: "没找到你要的东西。",
    409: "现在这个状态下做不了这件事。",
    422: "有个地方填得不太对。",
    429: "你点得有点快，缓一下再试。",
    500: "服务端出了点问题，我这边没能完成这一步。",
    503: "服务暂时用不了，稍后再试一次。",
}


def _offline_message(exc: Exception) -> str:
    return (f"连不上排菜服务（{base_url()}）：{type(exc).__name__}。"
            "服务端没起来，或者地址不对。"
            "启动命令：python -m uvicorn recipe_planner.api.main:app --port 8000；"
            "不想走服务端就把环境变量 USE_API 设成 0。")


def _problem(resp: httpx.Response) -> ClientError:
    """把 `application/problem+json` 原样翻译成 `ClientError`（不再自己编文案）。"""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    message = str(data.get("message") or "").strip()
    if not message:
        message = _FALLBACK_MESSAGE.get(resp.status_code, "这次请求没能完成。")
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    return ClientError(message, status=resp.status_code,
                       code=str(data.get("code") or ""),
                       next_steps=details.get("next_steps") or [],
                       request_id=str(data.get("request_id") or ""),
                       detail=resp.text[:300])


# ---------------------------------------------------------------- 连接


def base_url() -> str:
    return settings.api_base_url()


def _headers() -> dict[str, str]:
    # 本机部署 AUTH_MODE=off，不需要头；一旦切 apikey 就必须带上，否则 401
    if settings.auth_mode() == "apikey" and settings.api_key():
        return {"X-API-Key": settings.api_key()}
    return {}


_http_client: Optional[httpx.Client] = None
_stream_client: Optional[httpx.Client] = None
_http_lock = threading.Lock()


def _http() -> httpx.Client:
    global _http_client
    if _http_client is None:
        with _http_lock:
            if _http_client is None:
                _http_client = httpx.Client(base_url=base_url(), headers=_headers(),
                                            timeout=settings.api_timeout_sec(),
                                            trust_env=False)   # 见下面 _stream_http 的说明
    return _http_client


def _stream_http() -> httpx.Client:
    """SSE 专用连接：**读超时设成 None**。

    两个客户端都 `trust_env=False`：界面连的是**本机**服务端（docs/08 §13 决策 1 是本地部署），
    而 `HTTP_PROXY`/`HTTPS_PROXY` 一旦设上（公司网络很常见），httpx 会**把 127.0.0.1 也走代理** ——
    实测在这台机器上换个不存在的域名都会收到代理的 502，本地连接被无声地绕一圈甚至失败。
    本地服务不该走系统代理，所以这里显式关掉。
    

    阶段之间可能几秒没有新事件（例如在调模型），有读超时的话会误杀长连接。
    真正的上限由服务端自己兜（任务超时 + 15s 主动收流），客户端还会再加一道保险。
    """
    global _stream_client
    if _stream_client is None:
        with _http_lock:
            if _stream_client is None:
                _stream_client = httpx.Client(
                    base_url=base_url(), headers=_headers(),
                    timeout=httpx.Timeout(settings.api_timeout_sec(), read=None),
                    trust_env=False)
    return _stream_client


def reset() -> None:
    """丢掉连接池与缓存（换了服务端地址、或测试之间用）。"""
    global _http_client, _stream_client
    with _http_lock:
        for client in (_http_client, _stream_client):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        _http_client = None
        _stream_client = None
    begin_rerun()


# ---------------------------------------------------------------- 缓存

_lock = threading.RLock()
_list_cache: Optional[list[dict]] = None
_record_cache: dict[str, PlanRecord] = {}
_profile_cache: Optional[dict] = None


def begin_rerun() -> None:
    """新一次脚本运行开始：把上一次的缓存丢掉。

    界面每次重跑都会调一次（`app.py` 脚本开头一行）。刻意**不用 TTL**：
    TTL 要么短到没用、要么长到读到脏数据，而"一次重跑"是这个应用天然的边界。
    """
    global _list_cache, _profile_cache
    with _lock:
        _list_cache = None
        _profile_cache = None
        _record_cache.clear()


def _invalidate() -> None:
    """任何写入之后立刻失效：写完再读必须看到新数据。"""
    begin_rerun()


# ---------------------------------------------------------------- 通用请求


def _request(method: str, path: str, *, params: Optional[dict] = None,
             body: Optional[dict] = None, headers: Optional[dict] = None,
             root: bool = False) -> Any:
    """发一个请求。

    `path` 默认是**相对 `/api/v1` 的**（写成 `/plans`、`/profile` 这样）：
    业务接口全挂在 `/api/v1` 下面，让每个调用点自己拼前缀，迟早有人拼错或漏拼
    （第一版就是这么错的：`/plans` 被当成了绝对地址，整片接口全 404）。
    少数挂在根上的探针接口（`/health`、`/ready`）用 `root=True` 显式说明。
    """
    url = path if root else f"{PREFIX}{path}"
    try:
        resp = _http().request(method, url, params=params, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ApiUnavailable(_offline_message(exc)) from exc
    if resp.status_code >= 400:
        raise _problem(resp)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise ClientError("服务端返回了我看不懂的内容。", status=resp.status_code,
                          detail=resp.text[:300]) from exc


def ping() -> bool:
    """服务端在不在（界面的连接守卫用；探针接口不需要认证）。"""
    try:
        _http().get("/health")
        return True
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------- DTO → 领域对象


def _record_from_detail(detail: dict) -> PlanRecord:
    """`PlanDetailOut` → `PlanRecord`。

    和 `storage/repositories.py::_plan_to_record` 是**同一件事**（库 → 领域对象），
    刻意保持一致的填法：开发者字段（候选菜谱数、repairs、trace）界面用不到，填 0/默认，
    这与 `STORAGE=db` 时的行为完全一样，界面代码因此不需要区分两种后端。
    """
    c = detail.get("constraints") or {}
    summary = detail.get("summary") or {}
    result = PlanResult(
        constraints=UserConstraints(
            people=int(c.get("people") or 2),
            days=int(c.get("days") or 3),
            dishes_per_day=int(c.get("dishes_per_day") or 2),
            allergens=list(c.get("allergens") or []),
            spice_level=c.get("spice_level") or "不辣",
            taste_tags=list(c.get("taste_tags") or []),
            goal=c.get("goal") or "随便",
            strategy=c.get("strategy") or "daily_balance",
            max_time_min=int(c.get("max_time_min") or 45),
            skill=c.get("skill") or "随便",
            cook_start=c.get("cook_start") or "",
            budget_per_person_day=c.get("budget_per_person_day"),
            pantry_items=list(c.get("pantry_items") or []),
            # 「定住/加一道」的菜：界面靠它渲染"定住了"和换菜时的保留规则，必须带上
            must_include_recipes=list(c.get("must_include_recipes") or []),
            # 餐次（docs/10）：缺了这两个字段，界面的"一天几顿"会退回"只做晚餐"
            meals=list(c.get("meals") or ["晚餐"]),
            dishes_per_meal=dict(c.get("dishes_per_meal") or {}),
            breakfast_max_time_min=int(c.get("breakfast_max_time_min") or 15),
            snoozed_dishes=list(c.get("snoozed_dishes") or []),
        ),
        candidate_count=0,
        days=[DayPlan(day=int(d.get("day") or 0), meal=d.get("meal") or MEAL,
                      dishes=[ChosenDish(recipe_id=x.get("recipe_id", ""),
                                         reason=x.get("reason", ""))
                              for x in (d.get("dishes") or [])],
                      skipped=bool(d.get("skipped")),
                      people=d.get("people"))
              for d in (detail.get("days") or [])],
        issues=[ValidationIssue(level=i.get("level") or "warning",
                                code=i.get("code") or "",
                                message=i.get("message") or "",
                                day=i.get("day"), meal=i.get("meal"),
                                recipe_id=i.get("recipe_id"))
                for i in (detail.get("issues") or [])],
        shopping=[ShoppingItem(name=s.get("name", ""),
                               category=s.get("category") or "",
                               amount=s.get("amount") or "",
                               needed=bool(s.get("needed", True)),
                               for_recipes=list(s.get("for_recipes") or []))
                  for s in (detail.get("shopping") or [])],
        # 与 DB 后端一致：这个字段界面不用（只有「说人话改需求」用），填 summary 的口径
        estimated_cost_yuan=float(summary.get("total_cost") or 0.0),
        final=True,
    )
    return PlanRecord(id=detail.get("id", ""), created_at=detail.get("created_at") or "",
                      start_date=detail.get("start_date") or "",
                      label=detail.get("label") or "",
                      change_note=detail.get("change_note") or "",
                      done_days=list(detail.get("done_days") or []),
                      # docs/10：多餐时"哪一顿做完了"只有 done_slots 说得清，
                      # 不还原它，"做完了早餐"刷新一次就变回没做
                      done_slots=list(detail.get("done_slots") or []),
                      checked_items=list(detail.get("checked_items") or []),
                      result=result)


# ---------------------------------------------------------------- 读：方案


def list_plans() -> list[dict]:
    """轻量列表（`PlanListItem`，不带菜单）。"""
    global _list_cache
    with _lock:
        if _list_cache is not None:
            return list(_list_cache)
    data = _request("GET", "/plans")
    items = list(data.get("items") or [])
    with _lock:
        _list_cache = items
    return list(items)


def _detail(plan_id: str) -> PlanRecord:
    with _lock:
        hit = _record_cache.get(plan_id)
    if hit is not None:
        return hit
    rec = _record_from_detail(_request("GET", f"/plans/{plan_id}"))
    with _lock:
        _record_cache[plan_id] = rec
    return rec


def load_records() -> list[PlanRecord]:
    """全部存档（最新在前）。

    **已知的代价**：列表接口为了轻量不返回菜单，所以这里对每一份再拉一次详情 ——
    界面的「以前的方案」要用整份菜单（切到某一版时不重新排）。本机部署下方案只有几份，
    N 次请求可以接受；真要优化应该在 `GET /plans` 上加一个 `?detail=1` 的批量参数，
    而不是让界面自己拼（docs/09 记了这条）。
    """
    out: list[PlanRecord] = []
    for item in list_plans():
        try:
            out.append(_detail(item["id"]))
        except ClientError:
            continue          # 单份坏掉不能让整页崩（与 JSON 后端的容错一致）
    return out


def latest_record() -> Optional[PlanRecord]:
    items = list_plans()
    return _detail(items[0]["id"]) if items else None


def get_record(record_id: Any) -> Optional[PlanRecord]:
    if not record_id:
        return None
    try:
        return _detail(str(record_id))
    except ClientError as exc:
        if exc.status == 404:
            return None
        raise


def archive_summary() -> list[str]:
    """方案存档的一句话列表（和 JSON 后端同格式：「8/12–8/18（2026-08-09 15:20）」）。"""
    return [f"{item.get('label', '')}（{item.get('created_at', '')}）" for item in list_plans()]


def previous_record(record_id: Optional[str]) -> Optional[PlanRecord]:
    """比这一份更早的一版；id 不在列表里时返回最新那一份（与 JSON/DB 版语义一致）。"""
    items = list_plans()
    if not items:
        return None
    if record_id is None:
        return _detail(items[0]["id"])
    for i, item in enumerate(items):
        if item["id"] == record_id:
            return _detail(items[i + 1]["id"]) if i + 1 < len(items) else None
    return _detail(items[0]["id"])


def tonight_view(record: Optional[PlanRecord] = None, db: Any = None,
                 today: Any = None, now: Any = None,
                 day: Optional[int] = None, meal: Optional[str] = None) -> Any:
    """「今晚」页的数据 —— **由服务端产出**（docs/08 §5：措辞统一由后端给）。

    - 本地模式下这个函数来自 `recipe_planner.tonight`，两种模式返回**同一个** `TonightView`，
      所以界面渲染只有一份代码；
    - 给了 `record` 就查**那一版**的今晚（界面允许"切到以前的某一版"），
      没给就查最新一版（`/plans/current`，它在没有方案时也会正常返回 `no_plan`）。

    docs/10：`meal` 是"看哪一顿"（不给 = 当天最后一顿）。**必须回读 `meal`** ——
    界面拿它决定"做完了 / 来客人了 / 改回来做"落在哪一顿，丢了就会全部落到晚餐上。

    `db` / `today` / `now` 是为了和本地版**同签名**（服务端自己决定今天是哪天），
    在这里刻意不使用。
    """
    from recipe_planner.tonight import TonightDish, TonightView     # 见模块开头的导入说明

    params: dict[str, Any] = {}
    if day:
        params["day"] = int(day)
    if meal:
        params["meal"] = meal
    if record is not None:
        data = _request("GET", f"/plans/{record.id}/tonight", params=params or None)
    else:
        data = _request("GET", "/plans/current", params=params or None)

    return TonightView(
        state=data.get("state") or "no_plan",
        kicker=data.get("kicker") or "",
        headline=data.get("headline") or "",
        meta=data.get("meta") or "",
        reason=data.get("reason") or "",
        day=int(data.get("day") or 0),
        meal=data.get("meal") or MEAL,
        weekday=data.get("weekday") or "",
        date_label=data.get("date_label") or "",
        week_label=data.get("week_label") or "",
        dishes=[TonightDish(recipe_id=d.get("recipe_id", ""), name=d.get("name", ""),
                            time_min=int(d.get("time_min") or 0),
                            difficulty=d.get("difficulty") or "", reason=d.get("reason") or "")
                for d in (data.get("dishes") or [])],
        minutes=int(data.get("minutes") or 0),
        cost=float(data.get("cost") or 0.0),
        people=data.get("people"),
        eat_eta=data.get("eat_eta") or "",
        hint=data.get("hint") or "",
        next_steps=list(data.get("next_steps") or []),
    )


# ---------------------------------------------------------------- 写：方案


def _patch_day(plan_id: str, day: int, op: str, **extra: Any) -> dict:
    body: dict[str, Any] = {"op": op}
    body.update({k: v for k, v in extra.items() if v is not None})
    out = _request("PATCH", f"/plans/{plan_id}/days/{int(day)}", body=body)
    _invalidate()
    return out


def _day_patches(server: PlanRecord, result: PlanResult) -> list[tuple[int, dict]]:
    """逐顿比对"服务端那一周"和"界面这一周"，返回要发的单点操作（纯函数，可单测）。

    只做**事实判断**（哪几道菜不一样、人数一不一样），不做规则判断 ——
    规则（哪些菜能换、忌口怎么校验、清单怎么合并）永远只在服务端。

    docs/10：一天多顿时必须**按 (天, 餐) 比对**，而且每个操作都要带上 `meal`：
    用 `{d.day: d}` 建索引的话，一天里的几顿会互相覆盖（只剩最后一顿），
    于是"把早餐少排一道"会变成改晚餐。

    `meal` 只在**当天真的有好几顿**时才放进请求体：只做晚餐时请求与 docs/10 之前一字不差
    （服务端不给 `meal` 就是"当天最后一顿"，两者等价）。
    """
    server_slots = {(d.day, d.meal): d for d in server.result.days}
    per_day: dict[int, int] = {}
    for d in server.result.days:
        per_day[d.day] = per_day.get(d.day, 0) + 1
    out: list[tuple[int, dict]] = []
    for plan_day in result.days:
        old = server_slots.get((plan_day.day, plan_day.meal))
        if old is None:
            continue
        where = {"meal": plan_day.meal} if per_day.get(plan_day.day, 0) > 1 else {}
        if bool(plan_day.skipped) != bool(old.skipped):
            # 不做饭 / 改回来：由服务端重新挑菜，本地那一版不参与
            out.append((plan_day.day, {"op": "skip" if plan_day.skipped else "restore", **where}))
            continue
        ids_now = [d.recipe_id for d in plan_day.dishes]
        ids_old = [d.recipe_id for d in old.dishes]
        if ids_now != ids_old:
            out.append((plan_day.day, {"op": "replace_day", "recipe_ids": ids_now, **where}))
        elif plan_day.people != old.people:
            # 「来客人了」只改人数不改菜：人数是绝对值，None 表示回到这一周的基础人数
            out.append((plan_day.day, {"op": "people",
                                       "people": plan_day.people or result.constraints.people,
                                       **where}))
    return out


def update_result(record_id: Optional[str], result: PlanResult,
                  change_note: str = "") -> Optional[PlanRecord]:
    """把界面这一次改动同步到服务端 —— **按天表达意图，不整份覆盖**。

    界面在 `USE_API=1` 之前是自己算好整周、再整份存回去。服务化之后这样不行：
    派生数据（忌口冲突、买菜清单、花费、下锅顺序）必须由服务端算，否则立刻变成两份实现。

    所以这里只做一件事：逐天比对，只对**真的变了的那几天**发一个已有的单点操作
    （不做饭 / 改回来 / 整组替换 / 改份量），然后重新读一遍服务端的权威结果返回。
    比对用的是改动**之前**读到的服务端快照，所以多天的改动不会互相干扰。
    """
    if not record_id:
        return None
    server = get_record(record_id)
    if server is None:
        return None
    for day, body in _day_patches(server, result):
        op = body.pop("op")
        _patch_day(record_id, day, op, **body)
    return get_record(record_id)


def set_done(record_id: Optional[str], day: int, done: bool = True,
             meal: Optional[str] = None) -> Optional[PlanRecord]:
    """标记 / 取消「做过了」（M1 状态③）。

    docs/10：给了 `meal` 就只记这一顿；不给就是"这一天"（老行为，只做晚餐时等价）。
    """
    if not record_id:
        return None
    _patch_day(record_id, day, "done", done=bool(done), meal=meal)
    return get_record(record_id)


def set_checked(record_id: Optional[str], names: list[str]) -> Optional[PlanRecord]:
    """买菜清单的勾选（全量覆盖，幂等 —— 和服务端的接口语义一致）。"""
    if not record_id:
        return None
    _request("PUT", f"/plans/{record_id}/shopping/checks",
             body={"names": sorted(set(names or []))})
    _invalidate()
    return get_record(record_id)


def delete_record(record_id: str) -> None:
    """删除某一版方案（服务端要求显式 confirm，破坏性操作）。"""
    if not record_id:
        return
    _request("DELETE", f"/plans/{record_id}", params={"confirm": "true"})
    _invalidate()


def save_plan(*_args: Any, **_kwargs: Any) -> PlanRecord:
    """`USE_API=1` 下**不存在**这个动作，故意报错而不是静默什么都不做。

    服务化之后"这一周"是排菜任务在服务端直接存库的，界面再存一份就会有两份真相。
    看到这句说明有代码在服务化模式下还想本地存档 —— 正确做法是读 `job.record`。
    """
    raise ClientError(
        "USE_API=1 时方案由服务端的排菜任务直接存库，界面不该再存一份。"
        "（看到这句说明有代码还在调用本地存档，应该改成读 job.record）",
        code="save_plan_not_allowed")


def rate_day(record_id: str, day: int, score: int, meal: Optional[str] = None) -> dict:
    """做完之后打分：服务端一次事务里记「这顿做过了」+ 写档案（不会只成功一半）。

    docs/10：`meal` 指给哪一顿打分；不给 = 当天最后一顿（只做晚餐时就是那一顿）。
    """
    out = _request("POST", f"/plans/{record_id}/rate",
                   body={"day": int(day), "score": int(score), "meal": meal})
    _invalidate()
    return out


def dish_feedback(record_id: str, day: int, recipe_id: str, op: str,
                  meal: Optional[str] = None) -> dict:
    """菜单上对某一道菜的表态：like / dislike / lock / unlock / snooze / unsnooze。"""
    out = _request("POST", f"/plans/{record_id}/dishes/{int(day)}/{recipe_id}/feedback",
                   body={"op": op, "meal": meal})
    _invalidate()
    return out


def snooze_recipe(recipe_id: str) -> dict:
    """临时避开一道菜 7 天，不修改永久偏好。"""
    out = _request("POST", f"/profile/snooze/{recipe_id}")
    _invalidate()
    return out


def unsnooze_recipe(recipe_id: str) -> dict:
    """提前取消一道菜的临时避开。"""
    out = _request("DELETE", f"/profile/snooze/{recipe_id}")
    _invalidate()
    return out


def save_money(record_id: str) -> dict:
    """「哪里能省」：服务端挑最贵的一道换成更便宜的。"""
    out = _request("POST", f"/plans/{record_id}/save-money")
    _invalidate()
    return out


# ---------------------------------------------------------------- 档案
# 只实现两个 IO 边界；派生函数复用 profile.py 的纯函数（与 db_profile.py 同一套路）


def load_profile() -> dict:
    global _profile_cache
    with _lock:
        if _profile_cache is not None:
            return dict(_profile_cache)
    data = _request("GET", "/profile")
    # 服务端把来源痕迹给成列表（对前端友好），这里换回 JSON 后端同形状的字典：
    # 界面会把 load_profile() 的结果直接导成 JSON 文件给人，两边必须一模一样
    history = {h.get("name", ""): {k: v for k, v in h.items() if k != "name"}
               for h in (data.get("history") or []) if h.get("name")}
    profile = {
        "customer_name": "默认客户",
        "liked_dishes": list(data.get("liked_dishes") or []),
        "disliked_dishes": list(data.get("disliked_dishes") or []),
        "ratings": dict(data.get("ratings") or {}),
        "history": history,
        "snoozed_dishes": list(data.get("snoozed_dishes") or []),
    }
    with _lock:
        _profile_cache = profile
    return dict(profile)


def save_profile(profile: dict) -> None:
    """整份写回档案 —— 只用于"把档案恢复到上一个快照"（撤销）。

    为什么不逐条用 `PUT /profile` 表达：快照里**还有评分**（`ratings`），
    而 `PUT /profile` 只能动喜欢/不喜欢两个列表。逐条表达的结果会是
    "喜欢回来了、评分回不来"—— 那是个**假的撤销**，比没有撤销更糟（05 §4 R2）。
    所以这里走 `PUT /profile/restore`：它和本地版的 `save_profile` 是同一个语义（整份覆盖），
    服务端会自己跳过菜谱库里没有的名字。
    """
    _request("PUT", "/profile/restore", body={
        "liked_dishes": list(profile.get("liked_dishes") or []),
        "disliked_dishes": list(profile.get("disliked_dishes") or []),
        "ratings": dict(profile.get("ratings") or {}),
        "history": dict(profile.get("history") or {}),
    })
    _invalidate()


def _prof():
    """延迟导入 `profile`（见模块开头的导入说明）。"""
    from recipe_planner import profile as profile_mod
    return profile_mod


def liked_names(known: Optional[set[str]] = None) -> list[str]:
    return _prof()._clean(load_profile().get("liked_dishes", []), known) if known \
        else list(dict.fromkeys(load_profile().get("liked_dishes", [])))


def disliked_names(known: Optional[set[str]] = None) -> list[str]:
    return _prof()._clean(load_profile().get("disliked_dishes", []), known) if known \
        else list(dict.fromkeys(load_profile().get("disliked_dishes", [])))


def set_feedback(name: str, action: str, known: Optional[set[str]] = None,
                 source: str = "口味档案") -> dict:
    """写入一条反馈。

    **与本地版的一个已知差异**：服务端的 `PUT /profile` 是"确保"语义（重复点"喜欢"仍然是喜欢），
    本地版是"切换"语义（再点一次会取消喜欢）。docs/09 的待定事项(1) 已经把这件事记下来了，
    推荐方向就是"确保"；这里跟随服务端，等产品负责人拍板后两处一起定。
    """
    if action in ("clear_like", "clear_dislike"):
        _request("PUT", "/profile", body={"op": action, "source": source})
        _invalidate()
        return load_profile()
    _request("PUT", "/profile", body={"op": action, "names": [name], "source": source})
    _invalidate()
    return load_profile()


def bulk_feedback(names: list[str], action: str,
                  known: Optional[set[str]] = None, source: str = "口味档案") -> dict:
    if not names:
        return load_profile()
    _request("PUT", "/profile",
             body={"op": action, "names": list(names), "source": source})
    _invalidate()
    return load_profile()


def clear_all() -> None:
    _request("POST", "/profile/clear", params={"confirm": "true"})
    _invalidate()


def rate(name: str, score: int, known: Optional[set[str]] = None,
         source: str = "今晚页") -> dict:
    """给单道菜打分（规则复用 `profile.apply_rating_to_dict`，不重写一遍）。

    注意：**「做完了打分」应该用 `rate_day()`**（它同时记"这天做过了"，一次事务）。
    这里的逐道打分留给"档案页直接改分"这类场景。
    """
    updated = _prof().apply_rating_to_dict(load_profile(), name, int(score), known, source)
    save_profile(updated)
    return load_profile()


def rating_of(name: str) -> dict:
    return dict((load_profile().get("ratings") or {}).get(name) or {})


def feedback_origin(name: str) -> dict:
    return dict((load_profile().get("history") or {}).get(name) or {})


def profile_signature() -> str:
    return _prof().profile_signature_of(load_profile())


# ---------------------------------------------------------------- 排菜任务


class PlanJob:
    """一次排菜任务：和 `progress.PlanJob` **同读法**（stage / progress / done / cancel）。

    做法上是"开一条后台线程去读 SSE"，而不是让界面每 0.35 秒发一次轮询请求：
    - 界面那边的循环（骨架屏 + 阶段文案 + `st.rerun()`）一行都不用改；
    - 阶段变化由服务端推过来，不会因为轮询间隔而漏掉中间状态。

    多出来的是 `record`：服务端在任务里已经把这一版存好了，界面直接用它，
    `previous` 是**提交任务之前**读到的那一版（用来在回执里说"另存为新的一版"）。
    """

    def __init__(self, constraints: UserConstraints, db: Any = None, *,
                 change_note: Optional[str] = None, start_date: Any = None) -> None:
        self.constraints = constraints
        self.db = db                      # 本地模式下要用；服务端自己读菜谱库，这里不用
        self.change_note = change_note
        self.start_date = start_date

        self.stage: str = "正在提交排菜请求…"
        self.progress: float = 0.0
        self.stages_seen: list[str] = []
        self.result: Optional[PlanResult] = None
        self.record: Optional[PlanRecord] = None
        self.previous: Optional[PlanRecord] = None
        self.job_id: Optional[str] = None
        self.plan_id: Optional[str] = None
        self.message: str = ""
        self.error: Optional[str] = None
        self.error_code: str = ""
        self.next_steps: list[dict] = []
        self.done: bool = False
        self.cancelled: bool = False

        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------- 生命周期

    def start(self) -> "PlanJob":
        self._thread = threading.Thread(target=self._run, name="plan-job-api", daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: float = 60.0) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return self.done

    # ---------------------------------------------------------- 工作线程

    def _run(self) -> None:
        try:
            self.previous = latest_record()        # 必须在提交之前读，否则读到的是新这一版
            note = self.change_note or ("重新排了一版" if self.previous else "首次生成")
            accepted = create_plan(self.constraints, start_date=self.start_date,
                                   change_note=note)
            self.job_id = accepted.get("job_id") or ""
            self.stage = accepted.get("message") or self.stage
            self._consume_events(int(accepted.get("timeout_sec") or 120))
        except ClientError as exc:
            self.error = exc.message
            self.error_code = exc.code
            self.next_steps = exc.next_steps
            self.stage = "这次没排好"
        except httpx.HTTPError as exc:
            # SSE 长连接断了（服务端进程被杀、网络断了）：也要说人话
            self.error = (f"排菜过程中和服务端断开了（{type(exc).__name__}）。"
                          "服务端可能重启了，再排一次试试。")
            self.error_code = "stream_error"
            self.stage = "这次没排好"
        except Exception as exc:                  # 线程里任何异常都不能让页面挂掉
            self.error = f"{type(exc).__name__}: {exc}"
            self.stage = "这次没排好"
        finally:
            self.done = True

    def _consume_events(self, timeout_sec: int) -> None:
        deadline = _monotonic() + timeout_sec + 30      # 服务端自己兜一层，这里再加一道

        with _stream_http().stream("GET", f"{PREFIX}/jobs/{self.job_id}/events") as resp:
            if resp.status_code >= 400:
                resp.read()
                raise _problem(resp)
            event, data_lines = "", []
            for line in resp.iter_lines():
                if self._cancel.is_set():
                    self._cancel_on_server()
                    return
                if _monotonic() > deadline:
                    self.error = "等太久了，这次排菜没有结果。"
                    self.error_code = "timeout"
                    return
                if not line:
                    if data_lines:
                        self._handle(event or "message", "\n".join(data_lines))
                    event, data_lines = "", []
                    if self.done:
                        return
                    continue
                if line.startswith(":"):                # SSE 心跳注释，不是事件
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if not self.done:                            # 流被对面关掉了却没给终态
                self.error = self.error or "连接被中断了，这次排菜没有结果。"
                self.error_code = self.error_code or "stream_closed"

    def _handle(self, event: str, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if event == "stage":
            stage = data.get("stage") or ""
            if stage:
                if not self.stages_seen or self.stages_seen[-1] != stage:
                    self.stages_seen.append(stage)
                self.stage = data.get("label") or self.stage
            try:
                self.progress = min(1.0, max(0.0, float(data.get("progress") or 0.0)))
            except (TypeError, ValueError):
                pass
        elif event == "done":
            self.plan_id = data.get("plan_id")
            self.message = data.get("message") or ""
            # 服务端刚存了一版新的：先把缓存清掉，否则"以前的方案"还是旧的列表
            _invalidate()
            if self.plan_id:
                try:
                    self.record = get_record(self.plan_id)
                except ClientError as exc:
                    self.error = exc.message
                    self.error_code = exc.code
                    self.stage = "这次没排好"
                    return
                self.result = self.record.result if self.record else None
            self.progress = 1.0
            self.stage = "排好了"
            self.done = True
        elif event == "error":
            self.error = data.get("message") or "这次没排出来。"
            self.error_code = data.get("code") or ""
            self.next_steps = list(data.get("next_steps") or [])
            self.stage = "已停止" if self.error_code == "cancelled" else "这次没排好"
            self.cancelled = self.error_code == "cancelled"
            self.done = True

    def _cancel_on_server(self) -> None:
        """界面点了「停止」：服务端那边也要停，不然它会接着跑完再存一版。"""
        try:
            if self.job_id:
                _request("POST", f"/jobs/{self.job_id}/cancel")
        except ClientError:
            pass
        self.cancelled = True
        self.result = None
        self.stage = "已停止"
        self.done = True


def create_plan(constraints: UserConstraints, *, start_date: Any = None,
                change_note: str = "") -> dict:
    """提交排菜任务（返回 202 的 `job_id`）。

    带上 `Idempotency-Key`：网络抖动重试时不会排出两份一模一样的方案
    （中间件 5 只在这个头存在时生效）。
    """
    body: dict[str, Any] = constraints.model_dump(exclude_none=True)
    for key in ("liked_dishes", "disliked_dishes", "customer_name"):
        body.pop(key, None)            # 服务端自己读档案，不接受界面塞进来的偏好
    body["start_date"] = _iso_date(start_date) if start_date else None
    body["change_note"] = change_note
    return _request("POST", "/plans", body=body,
                    headers={"Idempotency-Key": uuid.uuid4().hex})


def cancel_job(job_id: str) -> dict:
    return _request("POST", f"/jobs/{job_id}/cancel")


def job_status(job_id: str) -> dict:
    return _request("GET", f"/jobs/{job_id}")


def _iso_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def _monotonic() -> float:
    import time
    return time.monotonic()
