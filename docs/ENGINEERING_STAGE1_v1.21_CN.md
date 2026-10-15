# Stage 1 v1.21 — Bluetooth Audio JSON 规则与证据隔离

## 适用范围

基于 v1.20 现有 Pipeline JSON 只读诊断与 Stage1 原生来源证据服务实现；保留既有 SQL/GOLD/审核提案模式，不更改数据库表或自动运行迁移。四项修复均采取**证据先行、歧义显式输出、人工审核后才能确认**的原则。

| 领域 | v1.21 实现 | 无法从处理后 JSON 证明的部分 |
|---|---|---|
| 章节误判 | 标题探测拒绝带编号引用尾缀；已有 JSON 的 section 与来源中显式编号参照行吻合时，输出 `SECTION_NODE_MAY_BE_CROSS_REFERENCE_CELL`（REVIEW） | 无法判断该编号是否同时存在合法章节，不自动删除节点或重新归属内容 |
| PDF 跨页表格 | 同 section、相邻 `page_number` 且两页各有至少两条三列分隔行时输出 `PDF_CROSSPAGE_TABLE_FRAGMENT_REQUIRES_NATIVE_VERIFICATION`；原生 PDF 几何多候选添加 `MULTIPLE_COMPATIBLE_TABLES_REVIEW` | 不确定行/列跨度、表头、条件/列对应关系或跨页同一张表；必须用原始 PDF 的物理网格核验 |
| 日英双语 | 独立计数 `JA`、`EN`、`MIXED`、`OTHER` 规范性来源行；两种语言同时存在时报 `JA_EN_REQUIREMENTS_EQUIVALENCE_NOT_VERIFIED`，同节双语节数单独计数 | 不能由共现推出两种语言的含义相同、优先级或等价要求 |
| 条件动作作用域 | 识别“接続時、”等日文条件；后置例外/多条规范性动作/跨原子条件继承都设置 `UNRESOLVED` 并丢弃误导性动作和条件切片 | 不自动裁定例外优先级，不实现最终规则引擎 |

## 命令与输出

```powershell
Set-Location 'C:\\mission\\Python\\DocumentIntelligenceConsole_Enterprise_v1.21_CN_FULL'
python -m pytest -q tests/test_v121_stage1_bluetooth_audio.py
python -m pytest -q tests/test_v120_stage1_pipeline_review.py
python .\\scripts\\review_stage1_pipeline_json.py `
  --json .\\input\\Bluetooth_Audio_Cover.json `
  --json .\\input\\Bluetooth_Audio_Body.json `
  --report-out .\\output\\review_v121.json
```

请以实际文件路径替换示例名。诊断报告仅含文档哈希、页码、节编号、告警代码、分布计数，不包含正文、文件名或原生 PDF 坐标。JSON 输入应已由可信 Pipeline 输出，错误/重复键/资源超限会拒绝处理。写入已存在的 `--report-out` 会拒绝覆盖。

原生 PDF（仅持有原文件时）沿用 `inspect_pdf_native(path, include_table_geometry=True)` / `suggest_crosspage_tables(report)` 检查列几何；这不会自动建立 header 或 requirement relationship。

## 回归范围与结果

- `tests/test_v121_stage1_bluetooth_audio.py`：11 项通过
- Stage1 + 标题 + 结构分析相关：222 项通过
- 本地可运行其余测试：539 项通过，2 项跳过（另 7 个测试模块因 `psycopg` / `ttkbootstrap` 未安装或 `input/test.pdf` 测试文件未提供，在全量收集时阻断；不是测试断言失败）

**未完成验证**：用真实 Bluetooth Audio 物理 PDF 对照拆页边界，确认合并单元格/重复表头，采集日英 Gold 标注以测量 precision/recall，Windows GUI 启动与 PostgreSQL/Qdrant 端到端读写。本版不因此宣称 Stage 1 已达到最终验收阈值。

## 变更文件

`app/analyzer/title_detector.py`、`app/engineering_intelligence/requirements.py`、`condition_scope.py`、`crosspage.py`、`stage1_semantics.py`、`pipeline_json_review.py`、`tests/test_v121_stage1_bluetooth_audio.py`。

