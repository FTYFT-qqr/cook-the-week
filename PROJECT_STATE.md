# 项目交接状态

> 交接编号：`handoff-2026-09-20-01`  
> 状态：材料已核对，等待接手核验  
> 项目目录：`D:\cook\recipe-planner`

## 1. 当前基线

- 分支：`main`
- 本地提交：`bf4853b`（`fix: harden api task lifecycle and exports`）
- `origin/main`：与本地提交一致，当前已同步。
- 运行时数据源：SQLite，数据库迁移版本 `0006`。
- 当前数据库检查结果：已发布菜谱 `127` 条，计划 `9` 条。
- JSON 文件仍作为种子、快照和测试夹具；正式运行时菜谱目录以数据库为准。

## 2. 已完成并验证

### C-02 / F-06

- 菜谱目录已迁移为 SQLite 运行时主数据源。
- 完成菜谱元数据、状态、版本、来源、营养信息、内容哈希、批次和引用关系的迁移支持。
- 增加内容目录 CLI：`seed`、`import_batch`、`publish_batch`、`archive`、`export_snapshot`。
- API 模式的页面从已发布数据库目录加载菜谱。
- 增加 Windows 安全的线上数据审计。

### B-01 至 B-05、B-07

- 计划任务幂等键稳定化。
- API key 模式下限流来源校验加固。
- 服务启动时回收孤儿任务。
- SSE 断开时补做一次任务状态对账。
- API 客户端缓存返回深拷贝，避免调用方污染缓存。
- CSV 导出的勾选状态改为纯文本 `是`。

### 验证证据

- `scripts\\verify.ps1`：`All verification gates passed.`
- pytest：`428 passed`
- 推荐评估：跨周 Jaccard 最大值 `0.0`，解释一致性 `1.0`，硬约束违规 `0`，周内重复 `0`。
- 线上只读审计：菜谱 `127`、计划 `9`、点赞 `0`、点踩 `1`。
- `self_check`：DB `170/0`，JSON `173/0`。
- `smoke_app`：JSON `205/0`，DB `201/0`。
- `smoke_api_app`：`44/0`。
- 数据迁移校验通过。

## 3. 尚未完成或不能宣称完成

- **B-06**：移除 `client.update_result` / `_day_patches` 过渡桥接，尚未完成架构迁移。
- **A6**：目标机器上的人工交付验收尚未完成；自动化 AppTest 不能替代人工验收。
- **M-01**：目前只有一周有效观测数据，不能据此下趋势结论。
- API 正式部署、鉴权策略和生产环境验收仍需按目标环境确认。

## 4. 工作区注意事项

以下是用户当前已有的未提交改动，本次交接未覆盖、未重写、未暂存：

- `docs/13-最终验收与推进方案.md`
- `docs/README.md`
- `docs/15-当前缺口与下一阶段整改方案.md`

`docs/15` 中部分状态文字可能早于最近两个提交；接手时应以提交记录、`docs/16-C-02与F-06执行记录.md`、`docs/17-B-01至B-05与B-07执行记录.md` 和实际验证结果交叉核对，不要直接覆盖用户文档。

`docs/07-开发进度与交接.md` 是历史快照，不应作为唯一的当前状态来源。

## 5. 接手后的第一步

1. 阅读并核对 `docs/15-当前缺口与下一阶段整改方案.md`，保留其中用户的修改。
2. 在 A6 人工验收与 B-06 架构迁移之间确定下一项工作；若没有新的优先级，优先处理 B-06。
3. 修改前先补测试，完成后运行完整验证并同步 Git/GitHub。

建议验收命令：

```powershell
$py='C:\Users\30542\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:RECIPE_PLANNER_PYTHON=$py
.\scripts\verify.ps1
```

数据库快速检查：

```powershell
$env:STORAGE='db'
$env:USE_API='0'
$env:DATABASE_URL='sqlite+aiosqlite:///D:/cook/recipe-planner/data/app.db'
& $py -c "from recipe_planner.storage import migrate; from recipe_planner.db import load_db; print('revision=', migrate.current_revision()); print('published_recipes=', len(load_db().recipes))"
```

## 6. 运行入口

直接使用数据库运行页面：

```powershell
& $py -m streamlit run app.py
```

API 模式需先启动服务，再设置 `USE_API=1` 和 `API_BASE_URL=http://127.0.0.1:8000` 后启动 Streamlit。

