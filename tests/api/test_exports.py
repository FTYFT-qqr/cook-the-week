"""P1-6 验收（2/2）：带走清单导出（docs/08 §6、docs/09 P1-6）。

三个出口的内容与界面里的「带走清单」**同一个来源**（`reporting`），
从哪个口子拿走都一样 —— 这是刻意的，不然"网页上导出"和"接口导出"会给两份不同的文件。

**关于"无 emoji"**：doc 09 写的出口标准是"内容非空且无 emoji"。实际情况是
`reporting.shopping_rows` 在勾选后用 `✅ 已买` 标记，而 `scripts/self_check.py` 第 405 行
**明确断言了这个字符串**（163 项护栏之一，不能改）。所以这里的口径是：
**除这个数据标记（`✅`）与打印用的方框（`☑`/`□`）之外，不允许出现任何 UI emoji**。
要彻底去掉 ✅，得同时改 `reporting` 与那条护栏断言 —— 那是产品决定，已记在 docs/09 待定。
"""
from __future__ import annotations

import re
from urllib.parse import unquote

import pytest

# 界面里那些装饰性 emoji（progress.py 的 STAGES / 设计规范里禁掉的那些）
UI_EMOJI = "🍳🥬🔍🛠️🛒⏹️❤️📌⚠️"
# 允许出现在导出里的符号：✅ 是"是否已买"这一列的数据标记，☑/□ 是打印用的方框
ALLOWED_SYMBOLS = "✅☑□"


# ---------------------------------------------------------------- CSV

async def test_export_csv(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}/export/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text
    assert body.startswith("\ufeff"), "CSV 要带 UTF-8 BOM，否则 Excel 打开是乱码"
    header = body.lstrip("\ufeff").splitlines()[0]
    assert header == "分类,食材,数量,是否已买,用于"
    assert "番茄" in body and "青菜" in body
    assert len(body.splitlines()) >= 4
    # 下载文件名（中文要按 RFC 5987 编码，且不能带 Windows 非法字符）
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=UTF-8''")
    name = unquote(disposition.split("''", 1)[1])
    assert name == "买菜清单-20260914.csv"
    assert not set(name) & set('/\\:*?"<>|'), f"文件名里有非法字符：{name}"


async def test_export_csv_marks_checked_items(api, client):
    """勾选之后导出要带"已买"标记 —— 与界面导出同一个来源。"""
    _, _, record = api
    detail = (await client.get(f"/api/v1/plans/{record.id}")).json()
    first = [i["name"] for i in detail["shopping"] if i["needed"]][0]
    await client.put(f"/api/v1/plans/{record.id}/shopping/checks", json={"names": [first]})

    body = (await client.get(f"/api/v1/plans/{record.id}/export/csv")).text
    assert "✅ 已买" in body
    assert not any(e in body for e in UI_EMOJI), "导出里不该有界面装饰性 emoji"


# ---------------------------------------------------------------- TXT

async def test_export_txt(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}/export/txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "一周晚餐菜单" in body
    assert "买菜清单" in body
    assert "第 1 天" in body and "周一" in body
    assert "本周预计花费" in body
    assert "共" in body and "项" in body
    assert not any(e in body for e in UI_EMOJI), "导出里不该有界面装饰性 emoji"


# ---------------------------------------------------------------- HTML

async def test_export_html_is_a_standalone_printable_page(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}/export/html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    # 必须是**能直接打开的文件**：自带 doctype 与样式（不能只给一个 <div class='a4'> 片段）
    assert body.startswith("<!doctype html>")
    assert "<meta charset='utf-8'>" in body
    assert "<style>" in body and ".a4-table" in body
    assert "@page{ size:A4" in body, "少了 A4 打印规则就贴不上冰箱"
    assert "本周晚餐菜单" in body and "买菜清单" in body
    assert body.strip().endswith("</html>")
    assert not any(e in body for e in UI_EMOJI)


async def test_export_html_marks_checked_items(api, client):
    _, _, record = api
    detail = (await client.get(f"/api/v1/plans/{record.id}")).json()
    first = [i["name"] for i in detail["shopping"] if i["needed"]][0]
    await client.put(f"/api/v1/plans/{record.id}/shopping/checks", json={"names": [first]})

    body = (await client.get(f"/api/v1/plans/{record.id}/export/html")).text
    assert "☑" in body and "□" in body, "打印版要能'买一样划一样'"


# ---------------------------------------------------------------- 边界

async def test_export_unknown_format_lists_the_options(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}/export/pdf")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_request"
    assert "pdf" in body["message"]
    labels = " ".join(s["label"] for s in body["details"]["next_steps"])
    assert "csv" in labels and "txt" in labels and "html" in labels


async def test_export_unknown_plan_is_404(client):
    r = await client.get("/api/v1/plans/deadbeef/export/csv")
    assert r.status_code == 404
    assert r.json()["code"] == "plan_not_found"


@pytest.mark.parametrize("fmt,min_len", [("csv", 50), ("txt", 200), ("html", 600)])
async def test_export_is_not_empty_for_every_format(api, client, fmt, min_len):
    """三个格式都不能是空壳（csv 天生短，所以门槛按格式分开定）。"""
    _, _, record = api
    body = (await client.get(f"/api/v1/plans/{record.id}/export/{fmt}")).text
    assert len(body) > min_len, f"{fmt} 导出内容太短：{len(body)}"


# ---------------------------------------------------------------- 分享视图

async def test_share_view_has_no_buttons_or_tech_words(api, client):
    _, _, record = api
    r = await client.get(f"/api/v1/plans/{record.id}/share")
    assert r.status_code == 200
    text = r.json()["text"]
    assert "这一周的晚饭" in text
    assert "周一" in text and "¥" in text
    for leak in ("按钮", "recipe_id", "None", "{}"):
        assert leak not in text
    assert not any(e in text for e in UI_EMOJI)
