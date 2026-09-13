"""错误文案的护栏：任何错误路径都不许把英文甩给客户（docs/05 §5.1）。

这个文件来自一次**真实**的 bug：`USE_API=1` 之后界面请求了不存在的地址，
拿回来的 404 文案是 Starlette 默认的英文 `"Not Found"` —— 因为异常处理器
"有 detail 就用 detail"，而框架自己塞的 detail 恰好就是标准英文短语。
界面只能原样显示，于是客户看到一句英文。

判断标准写在 `recipe_planner/api/errors.py::_standard_phrase`：
detail 等于标准英文短语时**不用**它，改用我们的中文默认文案。
"""
from __future__ import annotations

import httpx
from fastapi import Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from recipe_planner.api.main import create_app


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


async def test_不存在的地址也是人话(client):
    resp = await client.get("/api/v1/没有这个地址")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"
    assert body["message"] != "Not Found", "英文标准短语漏出来了"
    assert _has_chinese(body["message"]), body["message"]
    assert body["details"]["next_steps"], "失败也要给可点击的下一步"


async def test_用错请求方法也是人话(client):
    resp = await client.post("/health")
    assert resp.status_code == 405
    body = resp.json()
    assert body["code"] == "method_not_allowed"
    assert body["message"] != "Method Not Allowed", "英文标准短语漏出来了"
    assert _has_chinese(body["message"]), body["message"]


async def test_自定义的中文说明不会被吃掉(client):
    """这条守的是"别把修 bug 修过头"：我们自己写的中文 detail 必须照样透出。

    注意 `Request` 必须**在模块顶层**导入：写在函数体里它就成了局部名字，
    FastAPI 解析类型提示时找不到它，会把 `request` 当成一个查询参数 → 422。
    """
    app = create_app()

    async def boom(request: Request):
        raise StarletteHTTPException(status_code=404, detail="这份菜单被你自己删掉了。")

    app.add_api_route("/api/v1/_boom", boom, methods=["GET"])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as probe:
        resp = await probe.get("/api/v1/_boom")
    assert resp.status_code == 404
    assert resp.json()["message"] == "这份菜单被你自己删掉了。"
