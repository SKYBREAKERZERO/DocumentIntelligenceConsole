# 文档智能控制台 v1.03

`v1.03` 延续新的 1.x 应用发布线。此前 5.x 功能已合并到 1.x，后续继续按 v1.04、v1.05 顺序发布。

## v1.03 RAG 健康状态治理

- PostgreSQL、Qdrant、Ollama 连接正常但向量索引落后时，RAG Runtime 现在显示 `WARNING · Index sync required`，不再误报服务 `ERROR`。
- SQL 当前数据为 0 且对应 Qdrant Collection 尚未创建时，按合法空范围 `SKIPPED` 处理，例如当前 COMMON=0 的场景。
- Vector Dimension、Distance、Point Identity、Payload 类型/必填字段等契约损坏仍保持 `ERROR`。
- QA/BUG 健康检查明确显示 `records -> chunks` 的结构化字段级 fan-out；例如约 12 chunks/record 属于当前 Canonical QA/BUG RAG 设计，不等于记录重复。
- QA/BUG/式样书 SQL 与 Qdrant 数量差异作为派生索引 backlog 管理，建议执行增量 SQL -> Qdrant Sync，不要求强制全量重建。

## v1.02 核心修复：式样书本体优先

同一个 Spec No. 可以同时存在式样书本体、Cover Sheet、Check Sheet、Test Sheet、翻译件、附件等物理文件。v1.02 将它们统一视为一个逻辑 `Specification Entity`，并明确区分：

- `PRIMARY_CONTENT`：式样书本体，作为 RAG、工程关系、版本历史和版本比较的权威正文。
- `METADATA_SUPPORT`：Cover Sheet 等辅助文件，仅用于版本/标题/状态等元数据或审计，不得覆盖本体。

因此，例如数据库同时存在 `550 ... Spec.pdf` 与 `550 ... Cover Sheet.pdf` 时，AI 查询 550 的内容、要求、工程关系或历史版本，均优先读取本体；Cover Sheet 不再因为关键词相似度或旧数据库 `current_version_id` 污染而成为唯一证据。

## 工程关系

- `relation_search` 先解析目标 Spec No.，再选择该 Spec 的 `PRIMARY_CONTENT` 版本。
- 自动关系读取该本体版本的最新 `READY` extraction；不再盲信旧库中可能指向 Cover Sheet 的 `current_version_id` / `is_current`。
- 手工关系 `manual_spec_relations` 使用同一套本体优先规则。
- AI 输出 `RELATION_GRAPH` 时与工程关系图采用相同的 `EngineeringRelationGraphService` 主关系过滤规则。

## 历史版本与版本比较

历史版本比较同样只允许：

```text
PRIMARY_CONTENT(old) -> PRIMARY_CONTENT(new)
```

Cover Sheet、Check Sheet、Test Sheet、TRANSLATION、METADATA_ONLY、ISSUE_REGISTER 不进入业务版本下拉列表，也不能作为 diff 端点。旧数据库里曾被误登记为 `specification_versions` 的 Cover Sheet 仍保留审计记录，但会从 v1.02 的业务读模型隐藏，不删除历史数据。版本前后关系会按本体重新计算，因此不会出现 `本体 → Cover Sheet → 本体` 的伪历史链。

## PostgreSQL / Qdrant 兼容修复

v1.02 新增统一 `PrimarySpecificationDocumentPolicy`，并应用到：

- Specification Lookup
- Current RAG SQL view
- Qdrant Current Sync
- Qdrant 运行时 Evidence 过滤
- SQL Exact Search
- Engineering Relations
- Version History
- Version Compare / Change Timeline
- AI Version Search

同时，“一键配置企业数据库”会对旧库中明显的 CoverSheet/CheckSheet/TestSheet 等文件补齐：

```text
document_role = SUPPORTING
evidence_role = METADATA_SUPPORT
qa_index_policy = EXCLUDE
```

这是非破坏式修复：不会删除式样书、历史版本、关系证据或业务数据。若旧 `specifications.current_version_id` 指向 Cover Sheet，一键升级会把这个**可变 Current 指针**修复到同一逻辑式样书的首选本体版本；仅有 Cover Sheet、没有本体的旧逻辑条目不会再进入业务式样书目录。旧版补录逻辑也不再把 Cover/Check/Test Sheet 新建成业务版本。

### 从旧版升级后建议执行一次

1. 在 **系统管理 / Settings** 中，对正在使用的 21MM、24MM、Common 数据库执行一次“一键配置/升级数据库”。
2. 对对应范围执行一次 **SQL -> Qdrant 增量同步**。旧 Cover Sheet point 会被清出 Current 集合，原本因旧 current 指针未进入 Current 的式样书本体 chunk 会被补入。v1.02 查询端同时会对旧 Qdrant 集合先扩大候选集再过滤 Cover Sheet，避免 Cover Sheet 占满 Top-K 后把真正本体挤掉。
3. 不需要删除 PostgreSQL 数据库；不需要重新导入全部原始文档。

> 当前应用发布版本为 `v1.03`。PostgreSQL 内部 `RAG Schema v6` 是数据库兼容契约版本，继续保留，不等同于应用版本。

## 主要能力

- PDF / DOCX / PPTX / XLSX / TXT / 图片导入与批量文件夹导入
- PostgreSQL 文档、章节、Section、Chunk、版本历史与关系证据持久化
- Current-only Qdrant 同步与 Data Quality / Payload Index Governance
- Ollama 企业 AI 工作台与 Agentic RAG
- Specification Catalog / Version History / Version Compare / Change Center
- Engineering Relations / Relation Evidence / Graph / Manual Relations
- 独立 QA/BUG PostgreSQL + Qdrant RAG
- 企业配置、超时、Keep Alive、Streaming 与审计

## 启动

推荐 Windows PowerShell：

```powershell
.\run.ps1
```

AI 服务相关启动可使用：

```powershell
.\start_ai.ps1
```

依赖见 `requirements.txt`。
