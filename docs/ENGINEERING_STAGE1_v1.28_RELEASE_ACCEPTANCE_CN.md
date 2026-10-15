# Stage 1 v1.28 — 发布预检与正式验收交接

## 目标与边界

本版新增 `scripts/stage1_release_readiness.py` 和纯本地 `stage1_release_acceptance.py`，汇总**源码发布完整性、当前 Python 环境、原始 PDF SHA-256、v1.25 原生 PDF 审查、v1.26 SQL 来源链审查、v1.27 Gold 评分报告**，为 Windows/业务数据库现场验收提供可追溯、无原文的检查清单。

**重要：报告不会把预检通过写成生产验收通过。** CLI 没有访问远端服务或用户数据库，不认证人工审核人身份、不验证 DCM 状态机语义和日英要求等价。旧报告里的 `live_sql_transaction_verified_by_cli` 只是**该旧报告的声明**，不是本次连接数据库的证据。固定输出 `stage1_production_acceptance: NOT_VERIFIED`，须在企业正式验收流程里单独审批。

## 先运行离线预检

Windows PowerShell（已解压 v1.28、安装 venv 后）：

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.28_CN_FULL'
$py = '.\.venv\Scripts\python.exe'
& $py .\scripts\verify_runtime_package.py
& $py .\scripts\stage1_release_readiness.py `
    --out 'C:\authorized\Stage1.v128.local_readiness.json'
```

CLI 使用当前解释器检查 `fitz` (PyMuPDF)、`psycopg`、`ttkbootstrap`、`pytest` 是否存在。当前版本原始依赖清单仍为 `requirements.txt`，项目另有 `requirements-api.txt`。完整安装参考 README；当测试机器尚未安装依赖时，预检将标为 `BLOCKED`，而不是声称 GUI 可运行。`tests/test_loader.py` 不再依赖未提供的 `input/test.pdf`，通过临时生成的双页 PDF 做隔离单元测试。

## 追加真实原件和证据报告

先按照 v1.25、v1.26、v1.27 文档的步骤，用**相同版本**原始 PDF、Pipeline JSON 和实际数据库只读账户生成报告；完成独立人工 Gold 标注后得到评分报告。

```powershell
& $py .\scripts\stage1_release_readiness.py `
  --original-pdf 'C:\authorized\DCM-original.pdf' `
  --native-report 'C:\authorized\DCM.native_audit.json' `
  --chain-report 'C:\authorized\DCM.source_chain.json' `
  --gold-score 'C:\authorized\DCM.structural.score.json' `
  --out 'C:\authorized\Stage1.v128.handoff.json'
```

四个输入必须**同时提供**。其中任何一个源文件哈希不匹配、文档指纹/项目身份冲突或来源队列缺失，均阻止形成完整的报告候选。`gold-score` 的 `measured_human_gold_gates_passed` 仅作为**未认证的评分声明**保留；如果只是旧报告被手工修改为 PASS，它也不能替代对 Gold 标签和 PDF/SQL 数据的独立复核。

输出：`source_package`、`internal_imports`、`dependencies`、`pdf_json_sql_gold_evidence` 以及尚需人工完成的 Windows GUI、数据库、Gold 审批、恢复/性能/安全关卡。原文、文件名、数据库凭据以及源文档 ID 不写入结果。由于内部文件和源 SHA-256 仍具有一定关联性，报告应放在受控目录下。

输出采用独占新建 (`O_EXCL`)，绝不覆盖旧验收报告；输入限制文件大小，拒绝符号链接。退出码 `0` 仅表示报告被创建，并**不表示 Stage 1 已验收**。退出码 `2` 表示输入错误、证据不一致或输出冲突。

## Windows / 实际企业环境的人工验收清单

| 验收项 | 必须实际完成的证据 | 当前源代码环境能否代替 |
|---|---|---|
| DCM、Bluetooth Audio 原始 PDF | 确认 coversheet 与式样本体、修订号、页码、跨页表头、扫描页并附人工标注 | 不能 |
| PostgreSQL SQL 快照 | 21MM/24MM 指定项目、文档版本 ID、原始 SHA-256；只读 REPEATABLE READ 事务 | 不能（本工具仅读历史报告） |
| Requirement / Condition / Action | 专人标注/复核日英混合、跨页、嵌套条件、例外、反例；评估 P/R/F1 | 不能 |
| Windows GUI | 安装 Python 3.12、运行 `run.ps1`、切换项目、PDF 导入、章节/关系审查、无异常退出 | 不能（Linux 模拟不等价） |
| 依赖与完整测试 | 全量 pytest 无收集失败；非任意跳过；明确第三方依赖版本 | 本环境部分可验 |
| 恢复/安全/性能 | DB 备份与回滚、失败注入、300MB 文档处理、权限与隔离、运行日志 | 不能 |
| 审批归档 | 保存报告 SHA-256、批准人、审批日期和运行环境资产清单；由企业流程签收 | 不能 |

## 旧测试阻断项说明

v1.27 环境记录为 651 passed / 2 skipped / 7 collection errors，其中 6 个模块分别依赖 `psycopg`、`ttkbootstrap`，剩余 1 个旧 `tests/test_loader.py` 在 import 阶段硬读 `input/test.pdf`。v1.28 已将该历史测试改为**无需外部文件**的独立测试；若当前环境仍缺少两个 Python 包，则其余模块仍可能因依赖缺失无法收集。请不要仅为使测试“变绿”而把业务 PDF 或账户凭据放进发布 ZIP。

**正式结论：v1.28 是 Stage 1 生产验收准备交付版，不是已经在真实 Windows/PostgreSQL/业务文档环境中获得签收的正式生产版本。**
