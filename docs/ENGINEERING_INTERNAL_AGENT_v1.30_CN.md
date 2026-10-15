# v1.30 — Internal-Only Intelligent Agent（Stage 2 前置工程）

## 为什么先做它

旧 Open WebUI 截图表明，在用户询问“21MM BT 试样书”时，模型可能仅凭字面或模型知识回答“无法读取数据库”，而没有展示一次真实的 PostgreSQL / Qdrant 检索记录。**不应要求用户输入完整文件名、问题单编号或项目内部术语。** v1.30 先建立内部知识调度、检索失败诊断与拒答基础，再进入原计划的 Stage 2。

## 已实现的源码能力

1. `app/ai/internal_query.py`：受控、确定性的日中英/错别字查询候选，包括 `BT/蓝牙/Bluetooth`、`试样书/式样书/仕様書/specification`、DCM、BLE、HFP、连接/接続、配对/ペアリング。**只扩展查询，不把候选定义为同一份式样书。** 缩写不会被错误当成 SQL 主键或确认事实。
2. `FastIntentRouter`：含式样和 QA/BUG 的明确跨域问题进入工程 Agent；只有 QA/BUG 的问题仍优先专用问题库。精确 SPEC 编号保持原来查询路径；模糊文件名才新增 SQL `specification_lookup` 候选与 Qdrant 语义复核。
3. `QueryPlanner`：保留授权项目/版本，固定只读工具白名单；不接受模型提议的 `web_search` / `execute_sql` 等可执行工具，且使用安全的多语言改写作为候选；多轮指代仅携带用户曾明确给出的项目和技术关键词。
4. `EnterpriseRAGService`：**企业问题没有证据时直接返回固定拒答，不调用 Ollama 生成事实**。本地检索异常与正常零命中返回不同信息。有部分证据但有服务异常时标注降级；所有低级错误提示经过脱敏。
5. PostgreSQL SQL 精确搜索和专用 QA/BUG 搜索：以前吞掉的关键异常改为报告不可用，避免“数据库连不上”被混淆为“没有任何数据”。Qdrant 指定 collection 均不存在时也明确报错。
6. `GET /api/v1/health/knowledge` 与 `scripts/inspect_internal_knowledge_v130.py`：**授权、只读、无正文和凭据**地报告每个范围 PostgreSQL 可达性、Qdrant 集合缺失/空集合/已有数据/无法连接；禁用环境变量代理。现有 `GET /ready` 仅证明后台启动，不替代本接口。
7. v1.30 保留 FastAPI OpenAI 兼容端点的既有 **API schema v1.20**，保留旧客户端和原模型标识 `document-intelligence-rag`。没有迁移/重建 SQL 表或 Qdrant collection，也不触碰 `qdrant_storage` 卷。

## Windows 使用：先验证以前的知识库仍存在

在新版本根目录安装完依赖，确认 PostgreSQL 17/Qdrant/Ollama 已在本机启动：

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.30_CN_FULL'
.\.venv\Scripts\python.exe .\scripts\inspect_internal_knowledge_v130.py
```

输出按 21MM、24MM、COMMON、QABUG 报告：

- `postgres=REACHABLE`：只读 `SELECT 1` 成功；**不代表表或业务记录存在**。
- `qdrant=AVAILABLE`：指定 collection 存在且报告 points_count 大于 0；**不保证索引与 SQL 同步**。
- `qdrant=EMPTY`：集合可访问，但没有点；不能假定旧资料已经加载。
- `qdrant=COLLECTION_MISSING`：当前配置的集合不存在，需检查配置名称、原有卷和索引作业；**不要直接重建覆盖**。
- `UNREACHABLE` / `DRIVER_MISSING` / `NOT_CONFIGURED`：连通性或配置故障。
- `summary=DEGRADED` 并不代表数据丢失。CLI 退出码 2 代表需要排查，不执行修复/写入。

后台可通过 `GET http://127.0.0.1:18080/api/v1/health/knowledge`（使用原后端 Bearer Token）取得同样状态。不公开该令牌。该接口属于只读管理诊断，不应直接暴露给局域网浏览器。

## Open WebUI：必须选择受控的 RAG 模型

**关键部署限制：即使 Backend 只能内部检索，用户若在 Open WebUI 直接选择本地 Qwen/Ollama 模型，仍可绕开 Backend。** 因此应用内约束不是整个 Open WebUI 的安全边界。要让内部用户只访问受控知识，应在 Open WebUI 管理设置中：

- 设置 OpenAI-compatible base URL `http://host.docker.internal:18080/v1`，使用后端生成的 `local_api_token.txt`，模型 ID 只使用 **`document-intelligence-rag`**。
- 对目标用户禁用直接 Ollama 模型、Web Search、外部 Tools/Functions 和自定义外部知识源；禁用不需要的其他 OpenAI 提供方。
- 安排 Windows/Docker 主机防火墙或出站访问白名单，阻断非授权公网出站（这属于部署运维控制，本 ZIP **不会**自动修改 Windows 防火墙）。
- 运行只读检查：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\inspect_openwebui_route_v130.ps1
```

脚本能验证本机网关可达并展示少量 Open WebUI 环境标志，但**无法认证持久化 UI 内的全部管理员配置或物理网络隔离**。必要时需人工复核该 UI 设置并作实际出站隔离测试。

## 验收测试

1. “查一下21MM BT相关试样书” → 不要求完整标题，产生含 Bluetooth / 仕様書 的受控候选，但只引用真实命中的标题、版本、章节。
2. “查21MM BT式样书对应的QA BUG” → 原始项目与 QA/BUG 独立库跨域，不能推断不存在的关联。
3. “BUG-编号的状态” → 精确检索专用问题库，不随意进入式样书库。
4. 模型伪造工具 `web_search` / `execute_sql` → 拒绝，不产生互联网调用或执行模型 SQL。
5. SQL 连接失败、Qdrant collection 不存在 → 显示服务错误，**不能声称没有文档**。
6. 全部内部证据为空 → 直接拒答，验证 `Ollama.chat_stream` 完全未被调用。
7. 数据库/向量健康接口只读、无正文、无凭据，不能对任意用户开放。
8. 真实现场：用目标机现有 21MM、24MM、QABUG 数据跑查询，核对每一条引用。仅本地合成回归通过，不代表真实检索召回率、准确率已经验收。

## 已知边界

- 这是**内部知识问答安全基线和模糊检索入口**，不是 GPT-6 级别的大模型替代；Qwen 4B/8B 的意图理解和多步规划能力有限。
- “只凭内部证据”通过无证据拒答、只读工具和来源提示约束业务回答；**有证据时 LLM 生成内容仍需引用审核与质量评测**，无法仅用提示词数学证明每句话都准确。
- PostgreSQL/Qdrant 的真实数据未从你的 Windows 电脑转移到本执行环境；因此无法宣称原集合数量、文档召回准确率已在此处实机验证。
- Stage 1 的原始 DCM/Bluetooth Audio PDF、真实 PostgreSQL 和人工 Gold 验收仍待目标机完成。
- 暂不自动创建新向量集合、重新嵌入、执行数据库迁移、写入 QA/BUG 或调用外网搜索。

## 顺序

**v1.30（本版）先做内部受控智能体 → 目标机核对 SQL/Qdrant/Open WebUI 路由 → 原定 Stage 2（混合排名、多轮复杂关系、版本 Diff、QA/BUG 关联、人工评测）。**
