# Stage 1 v1.22 — Pipeline JSON 可复核来源定位与完整性再验证

本版基于 **v1.21 完整源码**，推进 v1.20/v1.21 的 Bluetooth Audio 离线诊断。目标是把“提示有风险”补充为“审查人员能够通过确定性的 JSON 内容索引和 Unicode 字符偏移回到同一条证据”。本版仍处于 Stage 1，不会自动批准工程规则、双语等价或跨页表格。

## 已完成内容

| 主题 | v1.22 实现 | 不具备的证明能力 |
|---|---|---|
| 章节/引用误判 | 对匹配节号 + 标题 + `参照` / `see section` 的导出行建立来源指针；一次遍历所有正文行，不再为每个章节反复扫描全文；重复 section ID 标明 BLOCK | 不能以导出 JSON 反向确定真实 PDF 字号或物理标题身份；不修改数据库章节 |
| PDF 跨页扁平表格 | 两个相邻 PDF 页各有至少两行三列分隔文本时，保存首行的**双页** JSON 来源指针；预算不足时两条指针一并省略并登记截断 | 不能证明同表、表头一致、纵横合并或条件沿用；不能恢复丢失的 PDF 几何 |
| JA/EN 要求 | 为候选规范性行分别保留 `LANGUAGE_JA_UNVERIFIED`、`LANGUAGE_EN_UNVERIFIED`、`LANGUAGE_MIXED_UNVERIFIED` 等标签和精准字符范围 | 共现不意味着翻译等价、约束相同或优先级相同；不生成双语批准配对 |
| 条件动作作用域 | 对 `UNRESOLVED` 的原子候选建立独立来源指针；识别异常时保留人工复核信息 | 不继承跨原子条件；不把作用域候选生成为可执行规则 |
| 数据安全 | 任意文本 section ID 只输出 `opaque:<hash>`，原本的合法小数字节号可保留；非法页码、未识别的 source format 不原样输出；新增章节/标题/文件名数量和长度预算 | 本地查看未经脱敏的原始 JSON 仍需遵守数据权限；哈希不等于完全匿名化 |
| 验证 | 新增一个**只读**命令重新执行所有诊断、核对报告及每条证据指针的源内容 SHA-256 与字符范围；文本改变、重排、改动指针时拒绝 | 只能证明与所提供的 **Pipeline JSON** 一致，不能认证原始文件 SHA、SQL 行、人工身份或业务语义 |

## 建议的 Windows PowerShell 工作流

1. 将 v1.22 完整源码解压在**新文件夹**，按 `README.md` 建立 Python 3.12 环境。无需为此步骤连接 SQL、Ollama 或 Qdrant。
2. 对经授权的**已有**导出 JSON 生成只读诊断报告。`--report-out` 必须是一个尚不存在的文件，不会覆盖旧报告。
3. 人工复核时，可使用 `evidence_queue` 的 `document_hash`、`content_index`（**从 1 开始**）、`start_offset`、`end_offset`、`content_sha256` 和 `evidence_sha256` 在本地比对原文本。偏移为 Python 字符（Unicode 码位），**不是** PDF 页内像素位置、字节偏移或 UTF-16 单元。
4. 在源 JSON 没有变化的前提下复核整个报告。再次导入的新数据版本应生成新报告，不建议继续沿用旧的审核指针。

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.22_CN_FULL'

.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\550_Cover.pipeline.json' `
  --json 'C:\authorized\Bluetooth_Audio.pipeline.json' `
  --report-out 'C:\authorized\stage1_v122_review.json'

.\.venv\Scripts\python.exe .\scripts\verify_stage1_pipeline_evidence.py `
  --json 'C:\authorized\550_Cover.pipeline.json' `
  --json 'C:\authorized\Bluetooth_Audio.pipeline.json' `
  --report 'C:\authorized\stage1_v122_review.json'
```

验证成功时仅输出 `PIPELINE_JSON_EVIDENCE_RECHECKED`、指针计数及 `source_native_file_verified: false`；不输出原文、文件名、用户身份或数据库密码。未匹配/被改动时退出码为 `2`，报告 `NOT_VERIFIED`，**不能**把失败当作审核通过。

`evidence_queue` 的每条记录固定为 `stage1-pipeline-source-pointer-v1`，包含 `review_key`（稳定哈希标识）、来源定位、内容与片段 SHA-256，以及 `decision: UNREVIEWED`、`native_source_verified: false`、`requirement_relationship_approved: false`。`evidence_queue_truncated > 0` 意味着预算达到上限；**未出现指针不等于该页或条款无风险**。所有来源指针只相对于同一份导出 JSON 有意义。复核工具会重跑整份诊断报告、对比字段及完整性，不通过则拒绝宣称复核成功。

## 回归记录（2026-10-14）

- v1.22 新增 **15 项**合成数据测试通过，涵盖 Unicode 区间、隐私安全、JSON 修改后拒绝、日英独立来源、条件未决、PDF 双页指针、预算截断、章节扫描与离线命令。
- Stage 1 关联定向集：**233 passed**。
- 全项目本环境可收集的测试：**554 passed、2 skipped**。另有 **7 个测试模块在收集时被阻断**：缺少 `psycopg` / `ttkbootstrap` 或历史用例所需的 `input/test.pdf`。因此不能称全量测试全部通过。
- `scripts/verify_runtime_package.py` 内部模块完整性自检通过。本版不构建 EXE、不执行 Windows GUI、PostgreSQL 权限/并发或真实 Bluetooth Audio 原生 PDF 对照，也**没有**正式 508/550 Gold precision/recall 验收结果。

## 后续 Stage 1 真实验收条件

需要授权的原始 PDF/DOCX/XLSX、可信 SQL 入库收据、带复杂跨页/合并表头的正式人工 Gold、以及 Windows + PostgreSQL 双人审核记录。只有完成物理 PDF 几何证据和真实标注指标核验后，才能判断 Stage 1 是否可结束；这些前提不能由 JSON SHA 或合成测试替代。
