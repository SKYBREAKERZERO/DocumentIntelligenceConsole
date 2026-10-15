# DocumentIntelligenceConsole Enterprise v1.30 CN FULL

## v1.30 — 内部知识约束型智能体（Stage 2 前置）

- **先理解，再查内部库**：新增日中英和常见错字联想；不用输入完整 BT/规格/QA-BUG 文档名。自由模型工具不会执行任意 SQL 或访问外网。
- **没有证据就不生成企业事实**：区分 SQL/Qdrant 故障和正常零命中，应用层无证据拒答，不调用 Ollama 补充未证实的业务事实。
- **只读在线诊断**：`scripts/inspect_internal_knowledge_v130.py` 及授权 `GET /api/v1/health/knowledge` 查询本机四个项目知识源状态；不会迁移、导入或删除数据。
- **Open WebUI 重要边界**：必须选择 `document-intelligence-rag`，禁用其他普通 Ollama 模型/外部 Web Search。见 [`docs/ENGINEERING_INTERNAL_AGENT_v1.30_CN.md`](docs/ENGINEERING_INTERNAL_AGENT_v1.30_CN.md)。
- **兼容性**：保留既有 OpenAI API v1.20 契约；新版本是完整 Python 源码，不是重新打包的 EXE。没有执行真实 Windows / PostgreSQL / Qdrant / 业务 PDF 验收。

## v1.29 — Stage 1 依赖 / 测试可复现性 / 实机只读预检

- `requirements.txt` 从 UTF-16 修复为 UTF-8，保留原精确版本（`pywin32` 加 Windows 平台条件），便于 pip 读取；**包版本及 Windows 3.12 安装成功与否仍需在目标机验证**。
- `DatabaseConnection` 在真实连接时才依赖 `psycopg`；`IngestionPipeline` 按格式延迟导入，支持无数据库的离线测试收集。
- 清理旧 `test_database.py` 和 `test_pipeline.py` 的 import-time 连接 / 读取缺失 PDF 行为，改为隔离测试；GUI 集成测试缺依赖时明确 `SKIP`，不是通过。
- 新增 `scripts/stage1_environment_smoke.py`：默认离线，只在 `--check-gui-import` / `--check-db` 明确指定后执行对应检查。数据库 SQL 始终使用只读事务并回滚，错误脱敏，不记录密码。
- Windows Python 3.12 安装脚本 `scripts/install_v129_windows.ps1`、独立验收操作手册 `docs/ENGINEERING_STAGE1_v1.29_WINDOWS_SQL_CN.md`。

**v1.29 不代表 Stage 1 已由企业生产环境签收。** 离线预检和模拟 SQL 测试不能代替真实 Windows GUI 交互、PostgreSQL 数据绑定、人工 Gold 或性能验收。

## v1.28 — Stage 1 发布预检与端到端验收交接

v1.28 新增 `stage1_release_acceptance.py`、`scripts/stage1_release_readiness.py`（及 PowerShell 包装脚本）：检查当前源码包、依赖、内部导入、真实 PDF 文件 SHA-256，并对 v1.25 PDF 对账、v1.26 SQL 来源链和 v1.27 Gold 评分报告做**跨文件身份一致性复核**。任何不完整来源队列和文档版本漂移都不视为完整证据。新增防误报回归测试，并将旧 `tests/test_loader.py` 改为自生成临时双页 PDF，不再在收集阶段读取缺失的 `input/test.pdf`。

**本版只生成离线验收交接报告，输出 `stage1_production_acceptance: NOT_VERIFIED`；不连接真实 PostgreSQL、不运行 Windows GUI、不确认人审身份或日英/状态机语义。** 本机测试通过不等于企业现场验收通过。请参见 [`docs/ENGINEERING_STAGE1_v1.28_RELEASE_ACCEPTANCE_CN.md`](docs/ENGINEERING_STAGE1_v1.28_RELEASE_ACCEPTANCE_CN.md)。

## v1.27 — 人工 Gold 结构化验收（新增）

新增 `stage1_structural_gold.py`、`scripts/evaluate_stage1_structural_gold.py`：在 v1.26 四层来源链与 v1.25 原生 PDF 对账**均完整**的前提下，生成纯待标注清单；由两位独立声明的人工审核角色补齐来源、章节归属和跨页表格真假标签，再计算 Precision / Recall / F1。新增相邻页几何表格配对全集以测量候选召回率（不包含 PDF 漏检表格），没有正例返回未定义而不是 100%。与已有 `stage1-gold-v3` Requirement/Scope holdout 评分组合使用；没有真实人工标注时验收门槛不会自动通过。

详细见 [`docs/ENGINEERING_STAGE1_v1.27_STRUCTURAL_GOLD_CN.md`](docs/ENGINEERING_STAGE1_v1.27_STRUCTURAL_GOLD_CN.md)。**真实 DCM / 550 PDF、独立人工 Gold、Windows GUI 和 PostgreSQL 实机验收依然未完成；本版不自动认证章节归属、跨页表头、日英等价或 DCM 状态迁移。**


**Stage 1 — 原始 PDF → Pipeline JSON → PostgreSQL Content → Requirement Unit 四层来源交叉核验（只读）**

v1.26 新增 `stage1_native_sql_chain.py` 和 `inspect_stage1_native_sql_chain.py`，在**同一条 PostgreSQL `REPEATABLE READ, READ ONLY` 事务**内复核项目/文档/版本归属、原始 PDF SHA-256、来源回填凭据、SQL 快照和需求偏移，再与**当次重新抽取**的原始 PDF 物理页、Pipeline JSON 章节/正文对账结果合并。重复 PDF 原文、页码漂移、章节不在导出树、SQL Content 不唯一、Requirement Unit 偏移不一致或证据预算截断，都不能形成完整候选来源链。

输出不含文档正文、源文件名、真实文档 ID 或凭据；仅包含哈希、页码、定位框、SQL 数字 ID、Requirement Unit key、审核状态和计数。**四层来源匹配仅指字面来源候选，不证明 DCM 状态迁移、条件/动作作用域、表格表头或日英翻译等价。** 不做数据库迁移或 SQL/Qdrant 写入，不自动批准人审结果。详细用法参见 [`docs/ENGINEERING_STAGE1_v1.26_NATIVE_SQL_CHAIN_CN.md`](docs/ENGINEERING_STAGE1_v1.26_NATIVE_SQL_CHAIN_CN.md)。

### Windows PowerShell（已有只读 PostgreSQL 用户）

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.30_CN_FULL'
# 未设置 DIC_STAGE1_RO_PASSWORD 时，CLI 将安全询问口令。
.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report-out 'C:\authorized\DCM.review.json'
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_native_sql_chain.py `
  --pdf 'C:\authorized\DCM-original.pdf' `
  --json 'C:\authorized\DCM.pipeline.json' `
  --review 'C:\authorized\DCM.review.json' `
  --document-index 1 --project 21MM --database rag_21mm `
  --document-id '从数据库查询的精确文档 ID' --version-id 123 `
  --user '已配置的只读用户' --password-env DIC_STAGE1_RO_PASSWORD `
  --report-out 'C:\authorized\DCM.source_chain.json'
```

如果未设置 `DIC_STAGE1_RO_PASSWORD`，CLI 会使用不可回显的交互式口令输入；也可从企业凭据管理器安全注入环境变量。不要把真实密码写进命令行历史记录。`version-id 123` 只是示例，需要换为真实的 PostgreSQL `specification_versions.id`。

**验收边界：** v1.26 新增 16 项生成 PDF / 模拟 PostgreSQL 事务的回归测试。没有接入实际企业数据库与真实 DCM 原始 PDF，Windows GUI、SQL 端到端实机验收与人工 Gold 仍未完成。本版为完整源码包，不是重编译 EXE。

---

## 既有部署与运维说明

### 部署架构与边界

- 管理工具：Windows GUI；启动时异步确保本地 Backend 运行。Settings 可选启用**独立后台看护**，在 Windows 用户登录后监测并恢复后台；关闭 GUI 不影响已运行的看护进程。
- 用户入口：Open WebUI Docker，本机 `127.0.0.1:3000`；以 **Chat Completions** API 连接 `http://host.docker.internal:18080/v1`，模型 ID `document-intelligence-rag`。
- 私有数据：PostgreSQL（21MM / 24MM / COMMON / QA-BUG）作为权威数据，Qdrant 作为派生向量索引；Ollama 和 BGE-M3 全部使用本地服务。不能公开 18080、5432、6333/6334 或 11434。
- **单机韧性不等于真正多节点高可用。** 登录用户退出、Windows 主机故障、断电、磁盘损坏、Docker Desktop/Ollama 停止时，本版本无法确保持续服务或自动切换。
- `ei_*` 要求级知识图谱是手动、显式迁移和回填的增强层，不会在安装、启动 GUI 或聊天时自动修改你的业务库。

## v1.08 改动（保持向后兼容）

1. **聊天并发准入**：默认同时最多 2 个活跃请求，配置 `DIC_MAX_ACTIVE_CHATS=1..8`；超限立即 HTTP 429 + `Retry-After: 3`，避免 Ollama/GPU 资源耗尽。流式完成、失败或断连均会释放准入租约。
2. **独立 Windows 看护**：`app.enterprise.backend_watchdog`；15 秒检查本机认证模型接口；只重启确认为 `STOPPED` 的后台，不杀死/覆盖其他占用端口的进程；10 分钟最多尝试启动 5 次，避免崩溃循环。单实例文件锁；轮换日志和心跳，不记录私有问答或密钥。
3. **GUI 设置**：在“外部 UI / Open WebUI”下添加独立看护开关及状态按钮；Windows HKCU 登录自启动，可反向关闭、不会自动开放局域网，不需管理员权限。看护进程独立于 GUI，在当前 Windows 登录会话内运行。
4. **运行诊断**：新增授权接口 `GET /api/v1/runtime/metrics`（无问题正文/凭据）、`GET /api/v1/health/ready`（区分 BGE 预热与存活，**不等于完整 SQL/Qdrant/Ollama 实时检查**）；`GET /api/v1/health/dependencies` 保留依赖实际探测。
5. **备份与可恢复性**：`scripts/backup_authoritative_sql.ps1` 对权威 PostgreSQL 数据库执行显式 `pg_dump -Fc`，用 `pg_restore --list` 验证并产生 SHA-256 清单。需要单独规划 Qdrant 重建/备份和恢复演练；多数据库备份不是跨库原子快照。
6. **审计工具**：`scripts/check_enterprise_readiness.ps1`（只读）发现本机私有端口意外全网监听，输出活跃请求指标。注意受防火墙/NAT/公司 Wi-Fi 策略影响，端口审计不等于完整渗透测试。

## Windows 安装 / 升级

保留原 v1.05/v1.07 目录和现有 `%LOCALAPPDATA%\DocumentIntelligenceConsole` 用户配置，不要覆盖仍在运行的源码目录；只允许一个版本绑定 18080。

```powershell
cd 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.30_CN_FULL'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

在工具 **Settings → 外部 UI / Open WebUI → 后台进程管理** 中，可以启用“后台独立看护”。启用后重新登录 Windows 仍能在当前用户会话中自动启动看护进程；原有 GUI 登录自启动开关与之独立。诊断与操作脚本见 [`docs/OPERATIONS_RESILIENCE_v1.08_CN.md`](docs/OPERATIONS_RESILIENCE_v1.08_CN.md)。

要确保独立看护在没有 GUI 的情况下继续工作，也可以执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage_backend_watchdog.ps1 -Action Enable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage_backend_watchdog.ps1 -Action Status
```

原来的 `start_backend.ps1` 仍用于前台诊断，不能在同一个端口重复启动第二个 Backend。

## 企业安全要求

本地模型与本地数据库并**不自动等于完全无数据外传**。Open WebUI 外部提供商 / Web Search / 自定义工具必须关闭；必要时通过企业防火墙控制外连。公司 Wi-Fi 端口共享必须由 IT 授权，并配置内网 HTTPS。现有 Open WebUI 与 Backend 通过共享 API Key 对接，**尚未实现服务端按用户/项目隔离**；如业务数据存在用户级权限，必须补充可信身份传递 + Backend RBAC 后才能正式多人部署。Qdrant 如果仍监听 `0.0.0.0:6333/6334`，请先安全收紧且保留 `qdrant_storage` 数据卷。不可把提示词注入或生成式判断当成来源证据。

## 工程智能数据库迁移

请继续严格按 [`docs/ENGINEERING_INTELLIGENCE_v1.07_CN.md`](docs/ENGINEERING_INTELLIGENCE_v1.07_CN.md) 进行备份、审查、显式 `ei_*` 迁移、分批离线回填、检查和人工审核；v1.08 **不更改数据库版本**，不改变 BGE-M3 向量维度或 Qdrant Collection ID，不自动跑迁移。要求级双端证据、未解析引用和历史版本一致性保护保持原样。

## 验收边界

- Python 静态/选定自动化测试在隔离环境执行。Windows UAC、PowerShell 5.1、真实 PostgreSQL/Qdrant、模型性能、内网 HTTPS、数据恢复演练、EXE 打包和多机故障切换需**在部署环境单独验收**。
- `DIC_MAX_ACTIVE_CHATS` 是单个 Backend 进程的准入上限，不能推导出有几人可并发使用；需要用你电脑的 GPU/CPU 和真实业务数据测试。
- 版本历史/关系的深层语义抽取、Sparse+Dense 混合检索、可恢复并行 DAG 和要求级 QA/BUG 到测试全链路仍属于后续工程，未作为本版本已完成能力发布。
