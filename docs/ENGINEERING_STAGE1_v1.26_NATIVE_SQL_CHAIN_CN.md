# Stage 1 v1.26 — DCM 原始 PDF / JSON / SQL / Requirement Unit 四层来源对账

## 目的与信任边界

本模块将 v1.24 的 PostgreSQL 来源精确追溯，与 v1.25 的原始 PDF 物理页文本层审计合并为一次**只读**检查。成功匹配表示存在**同一 Pipeline JSON 文字内容的来源候选**，不证明内容的工程意义准确。尤其不可将“初期无按钮”、“通信后显示”、“更新后保持”等不同句子自动生成状态机。表头连续、日英要求等价、例外优先级仍需人工审核。

检查流程：授权的原始 PDF → 当次 PDF SHA-256 / 文本行、物理页 → 原版 Pipeline JSON / v1.22 来源偏移 → 单条 `REPEATABLE READ, READ ONLY` PostgreSQL 事务 → `specification_versions` 文件哈希所有权 → 回填凭据和内容 digest → version 对应 `contents` → `ei_requirement_units` 的原文字符 offset → 形成脱敏四层候选报告。

## 环境和输入

Windows PowerShell、Python 3.12 虚拟环境、安装项目 requirements 中的 PyMuPDF、`psycopg`；PostgreSQL 账号至少对已有表/视图有 SELECT 权限，且不拥有写权限。数据库必须已经按照此前 Stage 1 文档明确迁移、完成回填并维护有效版本来源凭据。本命令**不会运行迁移**，也不能修复缺失 schema 或失效的 receipt。

1. 原始 PDF：必须为被 `specification_versions.document_id` 引用的 `documents.source_hash` 所对应的原始字节。不能用扫描后再保存/重压缩的文件替代。
2. JSON：对应 PDF 的完整 Pipeline 标准导出（不是脱敏摘录），并包含正确的 `document_id`、项目和页码。
3. 先用 `scripts/review_stage1_pipeline_json.py` 原封不动导出匹配报告。
4. `--document-index` 从 1 开始，对应本次提供的 JSON 文件序列中的文档；与 `--document-id`、`--project` 精确相等。

## PowerShell 命令

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.26_CN_FULL'
.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report-out 'C:\authorized\DCM.stage1_review.json'

.\.venv\Scripts\python.exe .\scripts\inspect_stage1_native_sql_chain.py `
  --pdf 'C:\authorized\DCM-original.pdf' `
  --json 'C:\authorized\DCM.pipeline.json' `
  --review 'C:\authorized\DCM.stage1_review.json' `
  --document-index 1 --project 21MM --database rag_21mm `
  --document-id '替换为实际 SQL 文档 ID' --version-id 123 `
  --host 127.0.0.1 --port 5432 --user '替换为最小权限只读用户名' `
  --password-env DIC_STAGE1_RO_PASSWORD `
  --report-out 'C:\authorized\DCM.stage1_chain.json'
```

未提供 `DIC_STAGE1_RO_PASSWORD` 环境变量时，将在终端交互询问口令，不会回显。不要将密码写入命令行参数、日志、代码或 ZIP。报告使用独占创建；存在同名结果时直接拒绝，不覆盖旧版审计资料。`--pdf-table-geometry` 是可选项，会增加物理网格候选计算，但不确认跨页表头。

## 输出状态解释

- `FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY`：该 DCM 来源指针在 PDF 物理页、导出章节、SQL 原文及 Requirement Unit 文字 offset 四处均唯一；依然**只是候选**。
- `NATIVE_ANCHOR_NOT_UNIQUE`：找不到唯一的 PDF 文本层行/多行候选；重复文字、页码漂移、扫描件无文本层都在其中。
- `EXPORTED_SECTION_NOT_RESOLVED`：Pipeline Content 引用的章节 ID 不在导出的章节树里。
- `SQL_CONTENT_NOT_EXACT_UNIQUE`：数据库文字、章节或页码不能唯一匹配来源。
- `SQL_REQUIREMENT_SPAN_NOT_EXACT`：SQL 中找不到该 JSON 证据指针对应的精确 Requirement Unit 范围。
- `NATIVE_CONTENT_NOT_SAMPLED`：本次 PDF 审计预算不足，不能赋予候选 SQL ID。
- `source_review_queue_complete=false`：任一阶段抽样/证据队列截断；即使有部分字面候选也不允许作出整个文档验收完成的结论。

输出 `all_four_layer_literal_candidates` 表示*本次抽取出的 DCM 证据*是否都形成字面候选，不是对文档所有业务要求的全面语义验收。`live_sql_transaction_verified_by_cli` 与 `native_pdf_ownership_hash_checked_by_cli` 只在命令成功完成受约束的只读事务后置为真。`semantic_relationships_approved`、`human_gold_acceptance_complete` 永远为 `false`。

## 已知限制与验收清单

- 仅支持 PDF 文本层。扫描件无 OCR 时不能建立物理文本指针；DOCX 的原生 OOXML 网格属于其他已有 Stage 1 审查路径。
- 页面文本换行比较使用 NFC 和空白归一化；**不**执行 NFKC、大小写折叠、语义替换或机器翻译，且不能将物理行顺序当成逻辑阅读顺序。
- 对账默认最多检查前 2,000 条 Pipeline contents、100,000 个 PDF 物理文本行和 500 个 DCM 证据条目；超限不得宣称完整，超出 hard limit 将拒绝。所有运行使用显式受权的只读用户；生产环境中还应在 DB 层设置 statement_timeout、监控内存。
- 用户现场仍需提供合法的 DCM 原始 PDF、对应 Pipeline JSON、所属真实 SQL 版本及只读账号。执行本命令并人工审核来源结果之后，继续在 Stage 1 Gold 集上标注条件/动作/表格结构与双语需求，生成 Precision/Recall/F1 验收报告。
- 当前提供的回归是通过合成 PDF 与模拟数据库事务完成，不构成真实客户数据或 Windows PostgreSQL 的生产验收。
