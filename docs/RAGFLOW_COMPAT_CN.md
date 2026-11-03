# RAGFlow 风格增量适配（2026-10-28）

## 基线与启用

用户确认使用 `DocumentIntelligenceConsole_Enterprise_v1.54_ALL_IN_ONE.zip`，不是未提供的 PRO_RAG_SKILLS_UPGRADE 包。保留原顶层目录、桌面入口、配置、数据库结构、原接口与七项 Skills；不迁移、不覆盖、不创建生产知识库，不执行 schema migration，不安装 RAGFlow 引擎。

在完整解压的新目录中使用原项目 Python 环境。依赖按原 README 与 `requirements-api.txt` 安装。先正常停止已运行的独立后端，避免端口冲突，再在 PowerShell 中运行：

```powershell
$env:DIC_RAGFLOW_COMPAT = '1'
python -m app.server
```

打开 `http://127.0.0.1:<原 API 端口>/compat/ragflow`，输入原 `local_api_token.txt` 中的令牌。可以使用原 `scripts/copy_local_api_key.ps1` 辅助复制。端口以 AI / RAG 设置为准，用户配置和令牌不自动复制或更改。

新页面需要独立 FastAPI 后端；桌面内嵌 `app/ai/api_server.py` 不提供新页面，不自动切换桌面 UI。

回退：停止独立后端，设置 `$env:DIC_RAGFLOW_COMPAT = '0'` 后重启，新入口返回 404；不涉及数据库回滚。也可从原 ZIP 的独立解压目录启动旧版本。

## 支持的 API 子集

基址 `/compat/ragflow/api/v1`，数据接口均要求 `Authorization: Bearer <原令牌>`。成功返回 `{code:0,message:"success",data:...}`；失败使用 HTTP 状态及原 FastAPI `detail` 格式。客户端必须先检查 HTTP 状态。

| 路径 | 已实现能力 |
| --- | --- |
| GET `/capabilities` | 机器可读边界、内部-only、Skills 清单 |
| GET `/datasets?page=1&page_size=30` | 既有已配置 scope 映射，不探测在线状态、不伪造文档数 |
| POST `/retrieval` | `question,dataset_ids,document_ids?,page?,page_size?`，原生 Hybrid / QA BUG 候选检索 |
| GET `/chats` | 每个已配置范围对应一个固定聊天预设 |
| POST `/chat/completions` | `chat_id,question,stream:false,history?`，原生回答、引用、证据门禁和并发限制 |
| GET `/workflows/template?dataset_id=dic-21mm` | DIC 固定工作流模板 |
| POST `/workflows/validate` | 校验 DIC JSON，不持久化、不执行 |
| POST `/workflows/run` | `workflow,question`，校验后运行原生聊天链路 |

检索请求示例：

```json
{"question":"Bluetooth Audio 的超时要求是什么？","dataset_ids":["dic-21mm"],"page":1,"page_size":20}
```

原 `/api/v1/retrieval` 的请求/响应不变，不接收上述 dataset 格式。只有支持自定义基址及该字段子集的客户端才可手工对接新前缀。

### 检索边界

dataset ID 为本地稳定别名 `dic-21mm`、`dic-24mm`、`dic-common`、`dic-qabug`，仅列出已配置范围，不是真 RAGFlow UUID。chunk ID 为范围、文档、content_id、evidence_id 的复合哈希。`dic` 保留原分数、引用、页码、章节、版本和原身份。

返回原检索器的有限候选，并再次按 scope 和 document_id 过滤、去重。`total` 只是本批过滤候选数；分页也只在本批候选中进行，不是全库命中数。document_ids 是后置过滤，可能遗漏召回窗口之外的相关文档。多库按输入库顺序、再按原候选顺序排列，不进行跨库排名归一化。

`similarity` 为 null：原 SQL、向量与融合分数不能冒充同一种相似度。未实现 similarity_threshold、rerank_id、metadata_condition 等控制；多余 JSON 字段返回 422，不静默假支持。doc_aggs 暂为空列表。

### 聊天及工作流边界

聊天沿用原 AUTO 路由。dataset 是范围选择提示，**不是租户隔离或 ACL**；跨域问题仍可调用原生受控跨域工具。系统仍使用原单机操作员令牌。

会话不落库。历史由客户端提交，仅接受 user/assistant；拒绝 system、session_id、stream:true，限制单条/总长度。页面仅在内存保留最近四轮，并控制历史总长度；重载、切库、断开会清空。reference.chunks 暂为空，不伪造引用结构，引用保留在原回答文本，dic.native_rag 提供原检索诊断。

工作流版本 `dic.ragflow-style.workflow.v1`，仅允许固定步骤 `internal-retrieval → evidence-governance → local-answer`。这是原生工程 RAG 流程示意；通用寒暄可由原路由跳过私有检索。Skills 列表只作能力说明，真正执行哪些工具仍由原用户问题与 Skills 路由决定，修改列表不等于更改执行计划。导入不执行 Python、SQL、HTTP、自定义节点或 Markdown 指令。**不支持直接导入 RAGFlow Canvas JSON。**

## 与真正 RAGFlow 的距离

研究参考（2026-10-28）：[官方 HTTP API DEV](https://ragflow.io/docs/http_api_reference)、[官方源码](https://github.com/infiniflow/ragflow)。DEV 文档随时可能变化，不构成固定版本 SDK 一致性承诺。借鉴知识库、检索测试、聊天预设、流程分层组织方式，未复制引擎代码。

未实现 DeepDoc、原生 dataset/document/chunk 管理、上传/解析任务、RAGFlow 存储/索引、完整 session/SSE、Canvas DSL/Agent 节点、用户租户 ACL、官方 UI 直接对接、官方 SDK 全量兼容。仅支持本页列明的字段与路径外形。未来若需真 RAGFlow，应另起隔离实例，固定版本并补单向导出与官方契约测试；本包不执行迁移。

此次改造不改变模型本身能力，不证明实际回答质量提升。PostgreSQL 仍为权威来源，Qdrant 仍为派生索引，Ollama 与原证据门禁继续负责回答。

## 安全和验证

默认关闭；保留回环绑定、Host 校验，无新增 CORS、CDN 或运行时外网调用。静态壳不含令牌/文档，数据接口均认证；不使用 localStorage/cookie 保存令牌，数据采用 textContent 渲染。分块传输也按实际字节限制到 2MiB。复用原并发准入，失败响应不回显凭据。

测试使用 Mock runtime，不连接生产 PostgreSQL/Qdrant/Ollama。覆盖默认关闭、原接口、认证、Host、过大请求、范围过滤/去重、未知参数、工作流拒绝、聊天历史、并发和异常释放。相关回归覆盖回答门禁、Skills、多轮 PDF。没有完成真实服务联调、内部 Golden 质量验收或官方 SDK 验证。

另修复 `scripts/run_skill.py` 在日文 Windows cp932 下输出中文失败：JSON 使用 ASCII 转义，json.loads 后原文完整还原，Skills 执行语义不变。

```powershell
python -m unittest discover -s tests -p test_ragflow_compat.py -v
python -m pytest tests/test_ragflow_compat.py tests/test_v149_engineering_skills.py tests/test_v150_live_engineering_skills.py tests/test_v154_multiturn_pdf_and_content.py -q
python scripts/verify_runtime_package.py
```

本次完整性清单为 `RELEASE_RAGFLOW_COMPAT_MANIFEST.sha256`，验证及修改列表见 `RELEASE_RAGFLOW_COMPAT_VALIDATION.json`。原 RELEASE_v1.54 文件保留为历史，不是增量版本的完整性断言。
