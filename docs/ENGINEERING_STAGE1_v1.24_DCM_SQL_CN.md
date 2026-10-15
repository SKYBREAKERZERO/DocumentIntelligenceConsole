# Stage 1 v1.24 — DCM JSON → SQL 原文证据绑定（中文）

## 边界与目标

本版本在 v1.23 的 DCM 词面诊断基础上增加一个**操作员显式运行的只读绑定工具**。可以审核：已有 Pipeline JSON 来源指针是否与选定 PostgreSQL 版本所拥有的 `contents` 记录**全文、章节、页码完全一致**；其指针对应的字符范围是否恰好是一个已入库 `ei_requirement_units` 的原始范围；若是，可显示未批准的原子/作用域候选数量。

该工具不能据此证明原始 DCM PDF 的文本提取、跨页表头、解析章节、日英语义等价、通信前后状态迁移、更新后 APP 保持或条件动作适用范围正确。`ACTION_ONLY` 和 `EXPLICIT_PREFIX_CANDIDATE` 均是词面候选，**不是工程规则的自动确认**。未包含真实用户 DCM PDF 或实机数据库结果。

## 运行条件

1. 使用 `review_stage1_pipeline_json.py` 生成未修改的 `stage1-pipeline-review-v1` 报告，并使用 `verify_stage1_pipeline_evidence.py` 离线复核通过。**确保 JSON 和报告均来自同一份实际导出。**
2. 用数据库中真实的 `specification_versions.id`、该版本 `document_id` 以及完全匹配的原始 PDF/DOCX（未编辑、未另存）运行工具。数据库 `rag_21mm` 或 `rag_24mm` 需有 `project_info` 的正确项目码、`documents.source_hash`、完整 v7 / Stage1 v2 回填；v3 的来源快照摘要验证可用。
3. 用限制为 SELECT 的 PostgreSQL 账号连接；口令使用环境变量 `POSTGRES_PASSWORD` 或交互提示。**不要把口令写入命令行或报告。** 源 JSON/PDF/报告均需已获本地处理许可。
4. 先在生产数据库的只读副本验证，如需在正式数据库运行，确认 SQL 读取权限、备份策略和并发负载。代码无写操作，但来源一致性检查可能扫描大量行。

## Windows PowerShell

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.24_CN_FULL'
.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report-out 'C:\authorized\v124_review.json'

.\.venv\Scripts\python.exe .\scripts\verify_stage1_pipeline_evidence.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report 'C:\authorized\v124_review.json'

# 请将文件路径、document-id、version-id、用户名替换为实际记录；不覆盖已有输出。
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_dcm_sql_trace.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report 'C:\authorized\v124_review.json' `
  --file 'C:\authorized\DCM-original.pdf' `
  --project 21MM --database rag_21mm `
  --document-id 'ACTUAL_VERSION_DOCUMENT_ID' --version-id 123 `
  --user 'rag_reader' `
  --report-out 'C:\authorized\v124_trace.json'
```

如果处理 24MM，将 `--project` 改为 `24MM`，`--database` 改为 `rag_24mm`，并使用该项目自己的版本和文档。连接参数可使用 `--host`、`--port`、`--password-env`。**JSON 报告和 SQL 溯源报告中不含原文内容或来源文件名**，但有文档哈希和数值 SQL ID，不应向未经授权的第三方公开。

## 结果解释

- `EXACT_SQL_CONTENT_ONLY`：指定 JSON 内容与 SQL `contents` **全文、章节、页码精确匹配且唯一**；不代表 PDF 提取真实性。
- `PAGE_MISMATCH` / `SECTION_MISMATCH`：禁止将不一致的 SQL 记录用作绑定依据。
- `SQL_CONTENT_NOT_FOUND`：全文不匹配；**不会使用相似度或模糊查询兜底**。
- `AMBIGUOUS_SQL_CONTENT` / `AMBIGUOUS_PIPELINE_SOURCE`：重复来源导致歧义，需人工查看原始文档和真实 SQL 版本。
- `EXACT_SQL_UNIT_REVIEW_ONLY`：该指针范围与一个 SQL Requirement Unit 的原始范围相等；其后只提供原子、条件作用域候选计数。
- `NO_SQL_REQUIREMENT_UNIT` / `SQL_UNIT_OFFSETS_NOT_EXACT`：无法绑定单个精确需求单元，不能据此认定需求遗漏或错误。
- `dcm_evidence_complete=false`：报告预算截断、DCM 采样未完成或指针不完整，**不能作为完整 DCM 覆盖率**。

程序入口的数据库操作均在 `REPEATABLE READ, READ ONLY` 事务内，只查询源文件关联、版本、回填凭据、来源快照和 Requirement Unit。`sql_rows_authenticated_by_caller=true` 表示本次执行曾在单一数据库只读事务中完成上述检查，**不是第三方数据库认证或语义验收**。不更改 `ei_*`、业务库、Qdrant、审核历史或版本关系。

## 实际验收尚缺

- 真实 DCM PDF → Pipeline JSON、章节、原子单元及物理页面人工核对；
- Windows 机器/目标 Python 3.12 依赖安装、GUI 正常启动；
- 授权 PostgreSQL 的只读实机联通、回填收据与版本一致性检查；
- 条件动作作用域、更新后保持状态以及日英对照的人工 Gold 评价。

本版的 21 项新增测试使用**合成数据及模拟 SQL 游标**，没有代替这些实机验收。
