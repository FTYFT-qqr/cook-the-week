"""`PUT /profile` 的两种"整列表"用法（界面上的两个按钮 + 撤销）。

服务化之后界面要用到两件以前只有界面自己知道的事：

1. **清空单个列表**（`clear_like` / `clear_dislike`）：不涉及具体菜名，所以 `names` 可以空，
   也不该去菜谱库里校验；清空会把"只属于这个列表"的来源痕迹一起丢掉，
   所以**没有撤销**（`undo_hint=None`）—— 但要把清了几道如实写进 `data.cleared` 和 message；
2. **整份恢复**（`PUT /profile/restore`）：撤销上一步改动时把快照原样写回去。
   快照里有**评分**，而 `PUT /profile` 碰不到评分 —— 少了这个口子，"撤销打分"就是假的撤销。

运行：python -m pytest tests -q
"""
from __future__ import annotations

from datetime import datetime, timezone

# ---------------------------------------------------------------- 清空单个列表


async def test_clear_disliked_list(client):
    """清空「不喜欢」：另一个列表、评分、只属于喜欢的痕迹都不受影响。"""
    r = await client.put("/api/v1/profile", json={"op": "clear_dislike"})   # 故意不带 names
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "profile_clear_dislike"
    assert "已清空「不喜欢」列表" in body["message"]
    assert "1 道" in body["message"]                      # 清了几道要写在回执里
    assert "本周菜单没动" in body["message"]
    assert body["data"]["cleared"] == ["红烧排骨"]
    assert body["action_log_id"]
    # 清空丢掉了来源痕迹，所以**故意**不给撤销线索（编一个假的"可以撤销"才是骗人）
    assert body["undo_hint"] is None
    assert body["next_steps"], "清完至少给个下一步"

    profile = (await client.get("/api/v1/profile")).json()
    assert profile["disliked_dishes"] == []
    assert profile["liked_dishes"] == ["番茄炒蛋"]                        # 喜欢没动
    assert profile["ratings"]["清炒时蔬"]["score"] == 2                    # 评分没动
    assert "番茄炒蛋" in {h["name"] for h in profile["history"]}           # 喜欢的痕迹没动
    # 指纹跟着变（界面靠它判断"偏好变了要不要重排"）
    assert "番茄炒蛋" in profile["signature"]


async def test_clear_liked_list_drops_only_its_own_history(client):
    """清空「喜欢」：只属于喜欢的来源痕迹也一起消失，属于不喜欢的留着。"""
    r = await client.put("/api/v1/profile", json={"op": "clear_like", "names": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["cleared"] == ["番茄炒蛋"]
    assert "已清空「喜欢」列表（1 道，本周菜单没动）。" == body["message"]
    assert body["undo_hint"] is None

    profile = (await client.get("/api/v1/profile")).json()
    assert profile["liked_dishes"] == []
    assert profile["disliked_dishes"] == ["红烧排骨"]
    assert "番茄炒蛋" not in {h["name"] for h in profile["history"]}
    assert "红烧排骨" in {h["name"] for h in profile["history"]}
    assert profile["ratings"]["清炒时蔬"]["score"] == 2


async def test_clearing_twice_is_idempotent(client):
    first = await client.put("/api/v1/profile", json={"op": "clear_dislike"})
    second = await client.put("/api/v1/profile", json={"op": "clear_dislike"})
    assert first.status_code == second.status_code == 200
    assert "0 道" in second.json()["message"]
    assert second.json()["data"]["cleared"] == []


async def test_unknown_op_is_422(client):
    r = await client.put("/api/v1/profile", json={"op": "clear_all", "names": []})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_request"
    assert body["details"]["next_steps"]


async def test_other_ops_still_need_names_from_the_recipe_book(client):
    """其它 op 的老规矩一行没变：names 不能空、必须在菜谱库里。"""
    empty = await client.put("/api/v1/profile", json={"op": "like", "names": []})
    assert empty.status_code == 422
    assert "哪几道菜" in empty.json()["message"]

    unknown = await client.put("/api/v1/profile", json={"op": "like", "names": ["佛跳墙"]})
    assert unknown.status_code == 422
    assert "菜谱库里没有" in unknown.json()["message"]

    # 而"清空"根本不看菜名：names 填个库里没有的也不该报错（它只清列表）
    clearing = await client.put("/api/v1/profile",
                                json={"op": "clear_like", "names": ["佛跳墙"]})
    assert clearing.status_code == 200


# ---------------------------------------------------------------- 整份恢复（撤销用）

SNAPSHOT = {
    "liked_dishes": ["番茄炒蛋", "清炒时蔬"],
    "disliked_dishes": [],
    "ratings": {"清炒时蔬": {"score": 2, "date": "09/10"},
                "紫菜蛋花汤": {"score": 1, "date": "09/11"}},
    "history": {"番茄炒蛋": {"since": "09/10", "source": "菜单页"},
                "清炒时蔬": {"since": "09/11", "source": "做完了打分"}},
}


async def test_restore_brings_back_likes_and_ratings(api, client):
    """撤销一次"做完打分"：喜欢回来了，**评分也回来了**（否则就是假的撤销）。"""
    _, _, record = api

    # 先改坏：给第 1 天（番茄炒蛋 + 清炒时蔬）打 0 分 —— 喜欢被移走、评分被改成 0
    rated = await client.post(f"/api/v1/plans/{record.id}/rate",
                              json={"day": 1, "score": 0})
    assert rated.status_code == 200, rated.text
    broken = (await client.get("/api/v1/profile")).json()
    assert broken["liked_dishes"] == []
    assert broken["ratings"]["清炒时蔬"]["score"] == 0

    r = await client.put("/api/v1/profile/restore", json=SNAPSHOT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "profile_restore"
    assert body["message"] == "已把口味档案恢复到上一步（喜欢、不喜欢、评分和来源痕迹都回来了）。"
    assert sorted(body["data"]["liked_dishes"]) == ["清炒时蔬", "番茄炒蛋"]
    assert body["data"]["disliked_dishes"] == []
    assert body["data"]["signature"]
    assert body["action_log_id"]
    # 它本身就是撤销，"再撤销一次"没有意义
    assert body["undo_hint"] is None

    profile = (await client.get("/api/v1/profile")).json()
    assert sorted(profile["liked_dishes"]) == ["清炒时蔬", "番茄炒蛋"]
    assert profile["disliked_dishes"] == []
    # 评分整份回来了（分数是快照里的，不是刚才打坏的 0）
    assert {name: info["score"] for name, info in profile["ratings"].items()} == {
        "清炒时蔬": 2, "紫菜蛋花汤": 1}
    # 来源痕迹也回来了（source 是原样存的）
    history = {h["name"]: h for h in profile["history"]}
    assert history["番茄炒蛋"]["source"] == "菜单页"
    assert history["清炒时蔬"]["source"] == "做完了打分"
    # 口径说明：数据库后端的 since/date 是**这行记录的写入时间**（ProfileRepo 里由
    # created_at 生成），所以恢复出来的日期是"写入那一刻"，不是快照里写的那个日期。
    #
    # **必须用写入方的口径（UTC）来算期望值**：`ProfileRepo` 写的是 `datetime.now(timezone.utc)`，
    # 而本机是 UTC+8 —— 本地 00:00–08:00 这一段里"UTC 昨天/本地今天"，用 `date.today()`
    # 断言会在这段时间里无缘无故变红（实测：本地 09-15 00:06 时它红了，白天一直绿）。
    # 这与 docs/07 踩坑 #37 是同一类"测试跟着时钟变色"的问题。
    today = datetime.now(timezone.utc).strftime("%m/%d")
    assert history["番茄炒蛋"]["since"] == today
    assert profile["ratings"]["清炒时蔬"]["date"] == today


async def test_restore_skips_dishes_missing_from_the_recipe_book(client):
    """快照里带了库里没有的菜：不 500、也不写进去（别的菜照常恢复）。"""
    r = await client.put("/api/v1/profile/restore", json={
        "liked_dishes": ["番茄炒蛋", "佛跳墙"],
        "disliked_dishes": ["库里的假菜"],
        "ratings": {"佛跳墙": {"score": 2, "date": "09/10"}},
        "history": {},
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["liked_dishes"] == ["番茄炒蛋"]      # 未知菜被静默跳过
    assert r.json()["data"]["disliked_dishes"] == []

    profile = (await client.get("/api/v1/profile")).json()
    assert profile["liked_dishes"] == ["番茄炒蛋"]
    assert profile["disliked_dishes"] == []
    assert "佛跳墙" not in profile["ratings"]


async def test_restore_accepts_an_empty_snapshot(client):
    """空快照 = 把两个列表和评分清空（退出登录/换人时用得上），不需要 confirm。"""
    r = await client.put("/api/v1/profile/restore", json={})
    assert r.status_code == 200, r.text
    profile = (await client.get("/api/v1/profile")).json()
    assert profile["liked_dishes"] == [] and profile["disliked_dishes"] == []
    assert profile["ratings"] == {}
