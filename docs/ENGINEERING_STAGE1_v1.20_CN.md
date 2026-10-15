# Stage 1 v1.20：已处理 Pipeline JSON 的本地工程质量审查

## 本轮依据

用于开发验证的两份授权导出样本分别是：21MM Bluetooth Audio 550 Cover Sheet 的 XLSX Pipeline JSON，以及同一主题 v2.52 PDF 的 Pipeline JSON。**原始 JSON 内容不随发布 ZIP 分发，也不会写入内置测试夹具。**

本次真实样本发现以下需要核查的问题：

1. Cover Sheet 已标为 `SUPPORTING` / `qa_index_policy=EXCLUDE`，但解析了大量“21MM Spec List”及修订历史记录。这些是辅助元数据，不能当作 550 式样书本体要求。
2. Cover Sheet 文件名指向 v2.50，而表内一条明确的 `SpecID` / `Ver.` 行指向 550 / v2.52；必须提示冲突，不能自动以任一项作为最终版本。
3. PDF 文件的 `BLUETOOTH_AUDIO` 分类可用，但正式 `specification_code` 仍未取得权威证据。即使两文件项目、主题与版本一致，也只能生成待人工审核的 `550` 身份候选。
4. PDF 中 `2.9 Now Playing 参照` 被旧解析器误当作新的 Section，导致后续内容归属被污染。新版本在原生文档重新解析时拒绝这种编号引用标题；**对已生成 JSON 或 SQL 不自动修改**。
5. 多页表格被扁平化到文本流，日文、英文、列头及单元格可能跨 Chunk；只凭导出的字符顺序不能恢复真正的物理行列、合并表头或表格业务含义。
6. 针对 PDF 的现有 Stage 1 行级提取，85 个 Chunk 产生约 2,635 个来源行级单元，但只有 62 个带明确规范性措辞的候选。这是**低规范性线索覆盖率警告**，不能据此宣称式样只有 62 条要求，也不能将此比例当作 Recall 或准确率。旧式样书中无 `shall/すること` 的动作句、双语内容以及多行表格可能造成漏检；需用人工标注核查。

## 本地运行（PowerShell）

在新的 v1.20 目录、安装 Python 依赖后：

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.20_CN_FULL'

.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\cover_pipeline.json' `
  --json 'C:\authorized\primary_pipeline.json' `
  --report-out 'C:\authorized\stage1_review_metadata_only.json'
```

工具仅检查本地原生 Pipeline JSON，不接 PostgreSQL、Qdrant、Ollama、WebUI；默认输出不包含原始正文、文件名、URL、邮箱或原始文档标识。输出报告仍应由使用者复核后再分享，**隐私模式不等于完成脱敏认证**。

此命令接受标准 Pipeline JSON 结构 `document`、`metadata`、`chapters`、`sections`、`contents`，不同于旧版人工脱敏片段格式 `stage1-filtered-samples-v1`。后者仍可使用 `inspect_stage1_filtered_json.py` 单独审查。

报告字段：`documents[].issue_counts`、`line_level_source_units_sampled` / `normative_signal_units_sampled`（仅诊断计数，不是准确率）、`findings`（仅规则代号、Section ID、页码）、`identity_candidates`（仅待审核 Code/Version）、`source_verified=false`、`quality_metrics_available=false`。输入大小、文档数、正文字符数以及报告 Finding 数均受限制。输出新文件采用独占创建，禁止覆盖。

## 企业级验收边界

- 现有数据库 / Qdrant 均不变；未升级文档不自动重建或修复索引。
- 本轮真正修复的是**未来重新解析原生 PDF 时**的章节引用误判。已有导出 JSON 的变更需要在用户本机重新导入原文后核验。
- 不能因为辅助 Cover Sheet 表内有 `550 / v2.52` 就自动修改 PDF 的 `specification_code`。
- 表格、双语要求、章节归属的 Precision/Recall/F1 需要原始 PDF/XLSX 或经人工审核标注的片段；不能用两份 Pipeline JSON 的内部一致性代替准确率。
- Windows GUI 启动、PostgreSQL 实机导入、用户级 RBAC 等仍需用户部署环境验收。本轮没有新增 DB 迁移和 EXE。
