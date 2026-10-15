# Stage 1 v1.25 — 原始 PDF ↔ Pipeline JSON 逐页文本层对账

## 目的与范围

在 v1.24 的 JSON/SQL 来源指针基础上，提供 **不连接数据库、不修改 SQL/Qdrant、不调用大模型** 的本地原始 PDF 对账命令。它用 PyMuPDF 读取原始 PDF 的真实文本层和页数，再与指定的一份 Pipeline JSON 文档逐条比对；输出脱敏字符哈希、页码与物理文本行定位，不输出源文本、文档名或原始文档 ID。

**注意：文本匹配不是解析验收，更不是语义验收。** 同一原文即使恰好出现在声明页，也不能证明 section/title、表格表头、单元格合并、跨页同一张表、日英翻译等价或 DCM 通信状态迁移正确。不能从命中的文本行反推 SQL 记录真实性、Pipeline 生成链身份或 Requirement Unit 已审批。所有输出均明确标记 `semantic_acceptance_complete=false`。

本版所用的自动化 PDF 样本均为**程序生成的合成跨页文件**。当前交付包没有用户真实 DCM/Bluetooth Audio PDF，因此没有真实业务文档验收结论。

## Windows 操作流程

在独立解压的 `DocumentIntelligenceConsole_Enterprise_v1.25_CN_FULL` 目录执行（安装 `requirements.txt` 中的 PyMuPDF 依赖）：

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.25_CN_FULL'

# 1. 导出原有的 Pipeline JSON 文件后，生成一次性只读诊断报告
.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report-out 'C:\authorized\DCM.stage1_review.json'

# 2. 验证诊断报告未脱离原 Pipeline JSON
.\.venv\Scripts\python.exe .\scripts\verify_stage1_pipeline_evidence.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report 'C:\authorized\DCM.stage1_review.json'

# 3. 读取相同版本的原始 PDF；只做字面来源对账（包含可选表格几何）
.\.venv\Scripts\python.exe .\scripts\audit_stage1_pdf_fidelity.py `
  --pdf 'C:\authorized\DCM-original.pdf' `
  --json 'C:\authorized\DCM.pipeline.json' `
  --review 'C:\authorized\DCM.stage1_review.json' `
  --document-index 1 `
  --pdf-table-geometry `
  --report-out 'C:\authorized\DCM.native_audit.json'
```

- `--document-index` 使用从 1 开始的文档序号，**不是**文档数据库 ID。多文件 `--json` 可以重复指定，顺序必须与生成 `review` 时完全一致，否则验签拒绝。
- `--pdf-table-geometry` 为可选参数，调用 PyMuPDF `page.find_tables()`，只增加待复核几何候选；某些 PDF 不包含可识别表格线条，几何分析也可能不可用。
- 默认比较前 2,000 条 `contents`、最多列出 400 条明细、最多 100 条跨页几何候选；所有截断数量写入报告，绝不把采样视为全覆盖。
- 输入路径禁止符号链接；输出文件以独占模式创建，不覆盖任何旧结果；失败信息不泄露源文件名或文本。对包含真实业务数据的报告按内部保密资料处理。
- 命令不使用 OCR。扫描 PDF 如果没有文本层，报告会明确提示 `native_pdf_has_no_text_layer=true`，不得判定内容已核对。

## 字面匹配结果

| 状态 | 含义（均需人工审核） |
|---|---|
| `UNIQUE_NATIVE_LINE_CANDIDATE` | 指定 `contents` 原文与 PDF 的单条文本行唯一对齐，且页码相同 |
| `UNIQUE_NATIVE_MULTILINE_CANDIDATE` | 原文与 PDF 提取顺序中最多四条相邻文本行唯一对齐，且页码相同 |
| `EXACT_NATIVE_TEXT_ON_DIFFERENT_PAGE` | 仅其他页出现字面匹配，可能存在页码漂移 |
| `AMBIGUOUS_REPEATED_NATIVE_TEXT_REVIEW` | 相同字面原文对应多个物理位置，**不选择其中任何一个** |
| `NO_EXACT_NATIVE_TEXT_SEQUENCE_REVIEW` | 未找到严格字面序列，不能直接断言原文漏提（可能发生布局/换行差异） |
| `WEAK_NATIVE_TEXT_ANCHOR_REVIEW` | 过短、过多数字或符号，不适合当成唯一来源锚点 |
| `LONG_SOURCE_NEEDS_MANUAL_ALIGNMENT` | 内容超出安全对账文本长度，需要人工检查或经授权拆分 |
| `PIPELINE_PAGE_UNRESOLVED` | JSON 页码缺失或不合规 |
| `PIPELINE_PAGE_OUT_OF_RANGE` | 页码超过原始 PDF 物理页数 |
| `SECTION_NOT_IN_EXPORTED_TREE` | 来源内容的章节标识未出现在同份 JSON 章节列表中，**与文本匹配独立报警** |

文字标准化只将 Unicode 规范组合形式 NFC 和连续空白用于字面比较；**不会**自动大小写合并、翻译、改写规范、同义词融合或截掉标点。页面文本层的提取顺序不一定等于视觉阅读顺序，因此失败应人工对照实际 PDF 版面，而非直接修改原数据库。

`native_locator` 的 `page`、`first_block`、`first_line`、`last_block`、`last_line`、`bbox` 是 PyMuPDF 提取时的几何定位，**不是**经过认证的要求/章节绑定。

## 跨页表格、日英及 DCM 验收边界

- 跨页候选：只有打开 `--pdf-table-geometry` 并成功识别上下页几何网格才会输出。列数、页面邻边距离和 x 边界符合时会产生 `POSSIBLE_CONTINUATION_REVIEW_ONLY`。即使只有一个候选，也**不**继承表头、合并实际表格或批准动作条件。
- 日英要求：v1.21–v1.24 的词面双语信号与作用域诊断继续存在，但此命令不新增翻译对照或自动判定等价。需要独立人工标注 Gold。
- DCM：初期/通信前后/成功失败/更新后保持是独立观察线索；只有 PDF 文本层对账通过也不能证明前后状态机、时间顺序或 APP/Telematics 显示规则。
- SQL：v1.24 的 `inspect_stage1_dcm_sql_trace.py` 是独立的只读数据库来源检查。本版不会自动将 native 对账结果写入 SQL，也不会把 PDF hash 当成绝对认证凭据。

## 后续真实验收输入

需要授权提供同一版本的原始 DCM/Bluetooth Audio PDF、其 Pipeline JSON 和审查报告，以及人工复核的章节、跨页表头、日英要求、条件例外和 DCM UI 状态标签。确认使用的是**确切原件而不是 coversheet 的另一个版本**。报告可作为 v1.26 SQL 实机溯源和 v1.27 Gold 评测的待审证据队列。
