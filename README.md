# cook-the-week · 一周晚餐规划 Agent

填一次需求 → 排出一周菜单与买菜清单 → 天天能用、越用越懂你。

排菜交给 LLM，**但所有硬约束由确定性代码把关**（过敏原、辣度、时长、预算、每顿结构、
"这顿不做饭"）：模型说了不算，`validate → repair` 循环最多修两次，修不动就退到确定性兜底方案。
这条分工是整个项目的立身之本 —— 也是它和"套个提示词的排菜机器人"的区别。

> 单机自用/家庭自用：数据只落在本机（默认 SQLite，可切回 JSON 文件），**没有账号、不上传、不联网也能排菜**。

## 长什么样

| 页面 | 干什么 |
|---|---|
| **今晚 / 今天** | 打开就知道今晚吃什么：主角大卡 + 每道菜 + `回家晚了` / `来客人了` / `开始做饭` / `做完了`。勾了三餐时自动改叫「今天」，一天列三顿 |
| **本周计划** | 整周卡片式菜单：**选天胶囊**一次只渲染一天，一天里早/午/晚各一张卡；每道菜可 `换一道`/`定住`/`喜欢`/`不喜欢`；`哪里能省`、`都满意`、`分享视图`；底部折叠 `整周一览` 与历史版本 |
| **排一周** | 需求表单（人数、天数、餐次、预算、忌口、口味、厨艺、开饭时间、家里已有食材）；排不出来时给**可点击的放宽选项** |
| **买菜清单** | 到店模式：一行一样、可打勾且**关掉浏览器还记着**、已买沉底；拆两批（先买耐放的）；可导出 CSV / 文本 / **A4 一页打印** |
| **口味档案** | 「喜欢 / 不喜欢」两个独立列表 + 全库挑选 + `最近的吃法`（最近常吃 / 好久没吃 / 刚开始爱吃）+ 改动历史与多步撤销 |

菜单上每道菜都能点开**怎么做**：先做什么（下锅顺序）→ 3–5 步做法 → 参考视频入口。

真实输出（`python` 跑确定性兜底排菜得到的，不是画的）：

```
这一周的晚饭（9/14–9/20）

周一 9/14　西红柿炒鸡蛋、蚝油生菜、小葱拌豆腐　（约 31 分钟 · ¥18）
　　【西红柿炒鸡蛋】
　　1. 西红柿两个去蒂切滚刀块，鸡蛋三个加少许盐打散
　　2. 热锅倒两勺油，倒入蛋液中火炒至凝固盛出
　　3. 锅内留底油下西红柿，中火翻炒三分钟出红汁
　　4. 倒回鸡蛋，加盐炒匀，撒小葱段翻两下出锅
　　【小葱拌豆腐】
　　1. 嫩豆腐一盒倒扣取出，切成两厘米见方的块
　　2. 锅中水烧开，豆腐块下锅烫一分钟去豆腥
　　3. 捞出沥干装盘，撒少许盐
　　4. 小葱两根切碎撒在豆腐上
　　5. 淋几滴香油，轻轻拌匀即可上桌
...
本周预计 ¥128
```

```
买菜清单（9/14–9/20） · 2 人 · 5 天
[ ] 西红柿（约 1.3 斤）  ← 西红柿炒鸡蛋、番茄豆腐汤
[ ] 鸡蛋（约 1.3 斤）    ← 西红柿炒鸡蛋、紫菜蛋花汤、韭菜炒鸡蛋、扬州炒饭、虾仁滑蛋
[ ] 猪里脊（约 250 克）  ← 青椒肉丝
...
—— 共 22 项；卖场里买一样划一样 ——
```

## 快速开始

```bash
git clone https://github.com/FTYFT-qqr/cook-the-week.git
cd cook-the-week
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                              # 填上 DEEPSEEK_API_KEY（不填也能跑，见下）
python -m recipe_planner.storage.migrate           # 建库/升级到当前 Alembic head
python -m recipe_planner.content.seed --file data/recipes.json  # 初始化已发布菜谱
# 兼容旧的一次性迁移（同时导入方案、档案）：
python -m recipe_planner.storage.import_json

streamlit run app.py                              # 打开 http://localhost:8501
```

**AI 不是可用性的硬依赖**：配置 API 时，模型只在本地过滤后的候选池里组合菜单，
并在校验失败时至多重排一次；未配置时直接走确定性算法。两条路径都经过同一套本地硬约束、
校验、买菜清单和最终理由重建（过敏/忌口、预算、每顿结构不会交给模型决定）。
`DEEPSEEK_API_KEY` 留空即可，服务化模式只是把同一条流水线交给 FastAPI worker 执行。

服务化模式（界面与排菜拆成两个进程，多端/局域网用）：

```bash
python -m uvicorn recipe_planner.api.main:app --host 127.0.0.1 --port 8000
# 另开一个终端：
USE_API=1 API_BASE_URL=http://127.0.0.1:8000 streamlit run app.py
```

## 怎么验证它是好的

```bash
python -m pytest tests                     # 领域逻辑 / 内核 / 存储双后端 / 接口
python scripts/self_check.py               # 交付前自测（默认 DB）
STORAGE=json python scripts/self_check.py  # JSON 后端同一套
python scripts/smoke_app.py                # AppTest 界面回归（默认 JSON）
SMOKE_STORAGE=db python scripts/smoke_app.py # DB 后端界面回归
python scripts/smoke_api_app.py            # 真 uvicorn 服务化链路
python scripts/verify_migration.py         # 隔离固定快照：验证 JSON → SQLite 迁移等价
python scripts/audit_live_data.py          # 真实数据只读审计（不要求与旧 JSON 相等）
python scripts/evaluate_recommendations.py # 固定场景推荐质量基线（只读业务数据）
# Windows PowerShell 可直接运行统一入口（自动设置 UTF-8，任一步失败即退出）：
powershell -ExecutionPolicy Bypass -File scripts/verify.ps1
```

日常验收以命令退出码为准，数量会随功能变化。`self_check` 与 `smoke_app` 的"项数"是 `check()` 调用计数，
不是断言数；`pytest` 是用例数。Windows PowerShell 推荐使用 `scripts/verify.ps1`，避免默认代码页导致
中文或 `✔` 输出触发假失败。

**这些脚本只写临时目录**（`.tmp/`），不会碰 `data/` 里的真实数据 —— 这一条是几次数据事故换来的，
`scripts/isolate_tmp.py` + `tests/test_isolate_tmp.py` 专门守着它。

## 架构

```
app.py                      Streamlit 界面（五页任务栏、设计 token、A4 打印样式）
recipe_planner/
  graph.py                  LangGraph 编排：retrieve → plan(LLM) → validate ⇄ repair → shopping → answer
  core.py                   确定性内核：硬过滤 / 评分 / 校验 / 修正 / 清单（可离线测试）
  models.py                 领域模型（Pydantic）
  llm_planner.py            模型调用与提示词；失败自动降级
  actions.py                意图动作（换一道 / 回家晚了 / 来客人了 / 省钱 / 打分…）
  reporting.py              客户语言层：概览、下锅顺序、结构统计、A4/CSV/分享文本
  preference.py             偏好权重：带时间衰减的逐菜权重 + "上次吃是多少天前"
  events.py                 偏好事件（逐菜一行：换掉 / 跳过 / 喜欢 / 打几分 / 做完了）
  allergens.py              隐性过敏原映射（蚝油→海鲜、生抽→大豆+麸质…）
  store.py / profile.py     JSON 后端的方案与档案
  client.py                 `USE_API=1` 时走 HTTP 的客户端
  api/                      FastAPI 服务：27 个接口、任务队列、SSE 进度、中间件栈
  storage/                  SQLAlchemy 2.0 async + Alembic 迁移 + 仓储层
  content/                  菜谱批次导入、审核发布、归档与数据库快照
data/recipes.json           菜谱 seed/审查快照：127 道（运行时主源为 SQLite published 菜谱）
data/                       你的数据（app.db / 档案 / 方案）—— **已被 .gitignore 排除**
docs/                       编号设计、交接、体检与实施文档
tests/ scripts/             自动化测试与自测脚本
```

**两个开关决定部署形态**（都是环境变量，随时可回退）：

| 开关 | 取值 | 含义 |
|---|---|---|
| `STORAGE` | `db`（默认）/ `json` | 数据放 SQLite 还是 JSON 文件；两条路语义一致，固定快照迁移测试守着等价性 |
| `USE_API` | `0`（默认）/ `1` | 界面直连领域层，还是走 FastAPI（带任务队列、SSE 进度、多端） |

## 几个刻意的设计决定

| 决定 | 为什么 |
|---|---|
| **硬约束不交给模型** | 过敏、辣度、时长、预算、每顿结构都由代码校验；模型只在候选池里组合，最终理由也由本地事实重建。修不动就退确定性方案 —— 宁可不好看，不能排错 |
| **每个副作用只有一个入口** | 菜单类事件只挂在"保存方案"那一处，档案类只挂在"保存档案"那一处，于是界面 / API / 两种存储**全覆盖**，按钮再加也不会漏 |
| **新机制用"空值 = 老行为"换挡** | 逐菜权重、轮换加分、"上次吃"都是空字典就不生效；老数据、老测试一行不改也不回归 |
| **文案只说事实** | 菜单上写"好久没吃这道了 · 上次约 2 个月前"，不写"因为…" —— 卡片上判断不出这一道是不是算法特意挑的，写"因为"就是说谎 |
| **数据不出本机** | 没有账号体系，SQLite/JSON 就在 `data/`；默认部署 `AUTH_MODE=off` 也只监听 `127.0.0.1` |
| **做法只存链接不抓取** | 不转存别人的视频/图文；没有策展链接时给"搜索入口"而不是编一个会 404 的地址 |

## 文档

`docs/` 是按"评审 → 设计 → 实现 → 体检 → 改进"写的完整过程，接手先看两篇：

- **`docs/07-开发进度与交接.md`** —— 现状、命令、**47 条踩过的坑**（接手前务必看）
- **`docs/README.md`** —— 全部文档的索引与阅读顺序

其余：`01` 产品设计 · `02/03/04` 三轮评审意见 · `05` 功能与交互设计 · `06` 界面设计规范 ·
`08` 后端架构 · `09` 后端实现计划 · `10` 全天菜单 · `11` 项目体检报告 · `12` 理想功能对照与改进方案 ·
`13` 最终验收与推进方案（当前完成度和收尾任务的唯一入口）。

## 已知边界

- 只面向**本机/家庭**：没有注册登录、没有多租户、没有云同步（`docs/01` 明确不做）；
- 排菜是"一周排一次"的节奏，所以**频次类统计样本不足**，"每天吃还是偶尔吃"只做展示、不参与排序；
- 做法是**文字步骤 + 视频搜索入口**，没有策展的具体视频直链（链接会烂，见 `docs/12` v7）；
- A4 打印页刻意**只印菜单与清单**（一页放得下），步骤走"分享文本 / 界面"；
- 阶段四（几荤几素的结构策略、按人忌口、家庭多成员）与阶段五（评估后）尚未开始。

## 许可

MIT，见 [`LICENSE`](LICENSE)。
