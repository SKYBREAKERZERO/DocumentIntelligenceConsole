# 核心后台与 Skills 优化说明

## 交付范围

基于用户提供的 v1.54 ALL_IN_ONE 及上一轮增量改造。保留原顶层目录、数据库结构、已有接口和业务功能。此次重点是可实际使用的管理入口、式样和 QA/BUG 的完整入库编排、阶段回执、Skills 可见性，以及真实解析测试发现的问题。

管理员使用桌面管理工具完成配置和入库，用户使用 Open WebUI；独立 FastAPI Backend 继续提供问答服务。桌面关闭后，已启动的独立 Backend 仍可供 Open WebUI 使用；尚未结束的入库任务不具备跨进程队列接管能力。

## 新增或改变的行为

| 部分 | 行为 |
| --- | --- |
| 默认首页 | 改为核心后台：配置、入库、数据浏览、历史版本、工程关系 |
| 导航精简 | 低频 SQL 编辑、ER 图、可视化查询、内嵌聊天诊断、旧 JSON 补导放入“高级工具”；保留代码与回退能力 |
| 式样一键入库 | JSON 与 SQL 完成后自动执行现有增量向量同步，再做 SQL/Qdrant 质量校验 |
| QA/BUG 一键入库 | 复用独立 QA/BUG SQL 和向量域，统一阶段回执；存在失败行/失败文件时跳过自动向量同步 |
| 配置一致性 | 完整入库锁定操作员选定的数据库/端口；向量模型、维度、式样集合取已保存 AI 配置；恢复页核心模式默认使用相同配置 |
| 阶段回执 | 原子写入本地回执，记录文件结果、转换/SQL、向量和校验状态，区分完全完成、部分完成、取消 |
| JSON 输出 | 完整入库每次使用独立 `core_runs/<运行ID>` 子目录，避免不同次运行覆盖 JSON |
| Skills 中心 | 显示既有七项 Skills，可加载合成示例并执行离线快照验证 |
| Skills HTTP | 认证后的能力发现和离线评估，不赋予 Skill 数据库写入权限 |
| PDF 修复 | Common 无可识别结构时保留过滤后的逐页文字，标记生成容器、未验证章节结构；式样无结构正文时标记警告并排除普通向量索引 |
| 离线向量模型 | SentenceTransformer 只加载本地已有模型文件，不在运行时下载 |
| 打包资源 | 补充 Skills 定义与示例的 PyInstaller 数据资源声明；本次不交付已验收 EXE |

这是入口收拢，尚未将所有旧页面改成延迟构建，也不宣称启动速度或内存占用已有实测提升。

## 一键配置与服务边界

继续使用原有连接配置、密码凭据机制、连接测试、非破坏性建库/升级和 Wi-Fi 共享按钮。初始化针对当前明确选择的数据域，不会擅自初始化全部数据库，不会自动重置已有数据。PostgreSQL、Qdrant、Ollama 和 Open WebUI 仍需事先安装并运行；本包没有偷偷安装服务、配置 Wi-Fi 或执行生产初始化。

Wi-Fi 只共享 Open WebUI；Backend、PostgreSQL、Qdrant、Ollama 保持本机访问。涉及系统网络设置的现有操作仍由管理员在界面主动触发。

完整入库的“CONVERT_JSON_SQL”阶段由原格式流水线执行，回执按这个组合阶段记录，不声称具有每个内部子步骤的独立事务。JSON 可能已生成但 SQL 写入失败，SQL 也可能在后续失败之前部分提交；不会伪造全链路事务或自动回滚。

QA/BUG 继续固定独立集合 `rag_qabug_current_chunks`，防止与式样域混用；AI 设置若将它改成其他名字，完整入库会要求先修正。普通式样的集合可按原 AI 配置管理。

## 文件支持与保真

保留原支持格式：式样/普通文档 PDF、DOCX、PPTX、旧 PPT、XLSX、TXT、PNG/JPG/JPEG；QA/BUG 表格 XLSX、XLSM、CSV、TSV、TXT。

本次实际使用合成文件运行 PDF、DOCX、XLSX、PPTX、TXT 的解析和 JSON 输出。旧 PPT 仍依赖 Microsoft PowerPoint COM；图像 OCR 及扫描 PDF 的效果不在此次验收范围，不能声称所有格式已达到相同质量。

Common PDF 的 fallback 只在最终没有任何正文且保留页含文本时触发：使用 `__UNSTRUCTURED_PAGES__` 生成容器，页码来自 PDF 原页。容器不是原章节，元数据显式标记 synthetic / structure_verified=false，并给出警告。已有部分正文时不会自动混入所有页面，避免把封面/目录充当技术证据；因此这不是覆盖所有局部漏解析的修复。

21MM/24MM 式样无结构正文时不套用上述 Common 兜底，标记 `PDF_NO_STRUCTURED_BODY`，作为待审核的 metadata-only 记录，不能凭此回答技术细节。复杂版面、合并表格、跨页表格、OCR 和企业真实式样仍需独立验收。

## 回执与恢复

回执位置为用户配置目录下 `ingestion_runs/<运行ID>.json`，只保存文件名、目标库/集合、阶段和错误类型，不保存原文、密码或令牌。时间使用带时区的 UTC，便于机器比较。

| 回执 | 含义与操作 |
| --- | --- |
| COMPLETED + PASS | 转换/SQL、向量同步及该质量检查完成；不是业务问答质量或人工生产验收通过 |
| PARTIAL / REVIEW_FAILED_FILES_THEN_SYNC | 某些文件失败；先查失败文件和 SQL 状态，再使用增量同步 |
| PARTIAL / VECTOR_SYNC_ONLY | SQL 可能已提交；在增量同步页预览并恢复，不必重跑成功文件 |
| PARTIAL / QUALITY_CHECK_ONLY | 同步结束但质量检查失败；先重跑校验 |
| PARTIAL / QUALITY_REVIEW | 质量结果 WARN/FAIL，检查缺点、孤儿点、哈希、范围及模型等问题 |
| CANCELLED | 已在受支持的安全点取消；已经完成的提交保留 |
| UNCONFIRMED_RUNNING | 磁盘上最后记录是 RUNNING；不能据此确认进程仍活着，需操作员检查 |

不自动恢复或重放转换，避免重复导入和误修改历史。当前增量同步沿用原项目 reconcile 规则，可能清理已过期的派生向量点，SQL 历史保留；完整重建仍是独立的高级操作。回执不是分布式任务队列，不能实现断电后从任意解析位置续传。

## Skills 的实际作用

| Skill | 核心业务用途 |
| --- | --- |
| spec-requirement-extraction | 从带来源的正文快照抽取需求单元 |
| content-relation-audit | 校验内容之间的关联证据 |
| spec-version-impact | 对比版本文本与变更映射 |
| engineering-hybrid-retrieval | 检查 SQL/Dense/Sparse 候选融合 |
| engineering-qa-bug-analysis | 分析票现象与式样候选依据 |
| rag-evidence-verification | 引用、参数、版本字面门禁 |
| rag-quality-gate | 离线质量门禁评估 |

既有 live_workflows / live_release / EvidenceGovernor 继续负责原生问答中的工具路由与发布控制。新增 Skills 中心及 HTTP 接口是独立的离线快照评估入口，不表示七项离线执行器会在每次聊天中全部运行，也不是将全局 Codex Skill 或 RAGFlow Canvas 自动安装进 Open WebUI。

管理员接口仍用原 Bearer token：

- GET `/api/v1/skills`：发现七项固定能力。
- POST `/api/v1/skills/{skill_name}/evaluate`：提交该 Skill 契约的 JSON 快照，返回 `database_access: NONE`。
- GET `/api/v1/operations/receipts?limit=20`：读取最近入库回执。

输入受到大小、条数和既有 Skill 契约限制，并复用后台并发准入。未知 Skill 返回 400，不能从 Markdown/JSON 注册代码、文件路径、SQL 或 HTTP 操作。

## 开源项目参考与实际落点

以下是设计借鉴；没有拷贝这些项目的引擎或声称全量兼容：

- [Haystack 官方 Pipeline 文档](https://docs.haystack.deepset.ai/docs/creating-pipelines)：参考将入库和查询分开组织。本次落实为原格式解析与后续派生索引阶段的明确编排和结果判断，没有换成 Haystack 框架。
- [RAGFlow 官方源码与 FAQ](https://github.com/infiniflow/ragflow/blob/main/docs/faq.mdx)：参考解析、向量化、存储和任务状态的分层呈现。本次落实为核心后台与阶段回执；没有移植 DeepDoc。
- [Qdrant 官方 Points 文档](https://qdrant.tech/documentation/concepts/points/)：参考稳定身份和幂等写入的恢复方式；原项目已有稳定身份与增量同步，本次复用并从一键流程接入，不将这些旧能力算作新实现。
- [Open WebUI 官方连接文档](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)：继续通过原 OpenAI-compatible `/v1` 连接后台，用户选择 `document-intelligence-rag`。没有在用户前端新增数据库管理操作。

官方文档研究日期为 2026-10-28，依赖仍以随包 requirements 为准。

原 Windows 安装脚本现在同时安装 `requirements.txt` 与 `requirements-api.txt`，避免只装桌面依赖后独立 Backend 缺少 FastAPI/uvicorn。没有在当前生产环境执行安装或更新依赖。

## 验证与限制

实际测试结果见 `RELEASE_CORE_BACKEND_VALIDATION.json`。覆盖阶段失败/取消/恢复提示、QA/BUG 失败行阻断、数据库目标锁定、模型配置一致性、Skills API 认证/限流、格式转换、PDF 保守兜底，以及原版本/关系/聊天回归。另在隔离配置中实际启动桌面首页和执行 Skills 合成示例。

未连接生产 PostgreSQL/Qdrant/Ollama，未执行生产建库、数据导入、Wi-Fi 改动；没有完整真实问答 Golden 验收、多用户压力测试、Open WebUI 实例端到端验收或 EXE 构建验证。此次交付为完整源码 ZIP。

保留旧完整导航：在启动窗口执行 `$env:DIC_CONSOLE_MODE='advanced'` 后启动；默认核心模式。RAGFlow 风格适配继续默认关闭，详见原兼容说明。

原 `RELEASE_v1.54_*` 和 `RELEASE_RAGFLOW_COMPAT_*` 均为历史验证记录；本次包以 `RELEASE_CORE_BACKEND_MANIFEST.sha256` 和 `RELEASE_CORE_BACKEND_VALIDATION.json` 为准。
