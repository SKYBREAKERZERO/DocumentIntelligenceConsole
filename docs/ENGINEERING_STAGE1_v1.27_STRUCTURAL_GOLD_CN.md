# Stage 1 v1.27 — DCM / Bluetooth Audio 结构 Gold 人工验收

## 目的与已有能力

此版本是在 v1.26 原始 PDF → Pipeline JSON → PostgreSQL Content → Requirement Unit **只读四层来源链候选**的基础上，建立离线人工标注及评估。原有 `stage1-gold-v3` 继续负责 Requirements、条件、例外、动作的精确字符跨度与 holdout 测试；本版新增 `stage1-structural-gold-v1`，专门度量来源链判断、章节树成员资格代理和候选跨页表格的精确匹配情况。

**不要把自动标识的章节树成员资格当成章节划分正确；不要把跨页几何连续当成表头/语义连续；不要把 Requirement/条件的跨度正确当成日英等价或 DCM 状态机批准。** JSON/SQL 来源的哈希对齐本身也不认证运行来源，须从可信原始资产重新执行审查。评估报告始终输出 `stage1_production_acceptance = NOT_ESTABLISHED_BY_THIS_TOOL`。

## 必要输入

1. 经 v1.25 原始 PDF 对账所得的完整 `native_audit.json`（需要 v1.27 的 `crosspage_pair_universe` / `_complete` 字段）。旧审查文件没有新增字段，必须用 v1.27 源码重新运行 `scripts/audit_stage1_pdf_fidelity.py --pdf-table-geometry`，或者由可信审查程序以相同输入重新生成报告。
2. v1.26 `scripts/inspect_stage1_native_sql_chain.py` 得到的 `chain.json`，与上面的 PDF/Pipeline 版本、SHA-256 一致；请使用**只读** PostgreSQL 用户。
3. `stage1-gold-v3` 人工复核的 holdout 标签文件（建议覆盖 PDF_TEXT / PDF_TABLE、日英混合、嵌套条件、例外、负例和跨页案例）。使用 `scripts/prepare_stage1_gold_v3.py` 生成初始待标注文件、人工补齐跨度及独立复核后，用 `scripts/evaluate_stage1_gold.py` 单独评分。

两个 JSON 报告需来自同一个文档/版本，不得从任意不同版本的导出拼接。评估只在来源队列、跨页建议队列及**所有被发现的相邻页 PDF 几何表格配对全集**均未截断时开放。全集由相邻页提取到的表格候选做笛卡尔积，含没有被启发式规则推荐的负例；否则无法真实计算该全集中的 Recall。**没有被 PDF 几何探测器发现的表格不在全集内**，其漏检率仍需独立标注与验收。

## Windows PowerShell：建立结构 Gold 清单

在已安装 v1.27 依赖的 Python 环境中，先完成 v1.26 的只读 SQL 来源核对、v1.27 的原生 PDF 对账。然后：

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.27_CN_FULL'
$py = '.\.venv\Scripts\python.exe'

& $py .\scripts\evaluate_stage1_structural_gold.py `
  --prepare `
  --native-report 'C:\authorized\DCM.native_audit.json' `
  --chain-report 'C:\authorized\DCM.chain.json' `
  --dataset-id 'DCM-21MM-v1-review-001' `
  --confirm-local-review `
  --out 'C:\authorized\DCM.structural.pending.json'
```

本地待标注文件不含原文句子、源文件路径和数据库凭据；仍包括原始内容 SHA-256、索引、页码与表格索引，应按企业内部敏感资料管理。**不会自动填入任何真假 Gold 标签**。针对 `sources` 中每条记录，人工确认 `literal_source_correct` 和 `section_assignment_correct`（严格布尔 `true/false`）；针对 `adjacent_table_pairs` 每一项人工确认 `true_continuation`。没有足够证据的条目不得猜测填值，应保持 `null` 并暂不评分，或退回补充原始 PDF 复核。

独立复核人员与最初标注人员应使用不同标识，并在 `review` 中写入 `annotation_state: VERIFIED`、`annotator_id`、`reviewer_id` 与非空的 `review_note`。所有内容完成后使用以下命令预览校验摘要（不打印内容）：

```powershell
& $py -c "import json; from app.engineering_intelligence.stage1_structural_gold import reviewed_label_digest; f=r'C:\authorized\DCM.structural.reviewed.json'; print(reviewed_label_digest(json.load(open(f,encoding='utf-8'))))"
```

将打印的 SHA-256 记录到 `review.reviewed_label_sha256`。这只是**标签一致性摘要**，不能证明审核人真实身份或其审核质量。

## 评分与门槛

```powershell
& $py .\scripts\evaluate_stage1_structural_gold.py `
  --evaluate `
  --native-report 'C:\authorized\DCM.native_audit.json' `
  --chain-report 'C:\authorized\DCM.chain.json' `
  --gold 'C:\authorized\DCM.structural.reviewed.json' `
  --v3-gold 'C:\authorized\DCM.requirement_v3.reviewed.json' `
  --min-literal-f1 0.90 --min-section-proxy-f1 0.90 `
  --min-crosspage-f1 0.90 --min-requirement-f1 0.90 --min-scope-f1 0.85 `
  --min-source-cases 10 --min-crosspage-pairs 2 --min-positive-labels 1 `
  --min-holdout-cases 10 `
  --confirm-local-review `
  --out 'C:\authorized\DCM.structural.score.json'
```

退出码：`0` = 所指定测量门槛通过，`2` = 门槛未通过或命令/审核约束不满足。默认门槛是示例值，并非公司正式验收 SLA。分别输出 TP、FP、FN、TN、Precision、Recall、F1；没有正例时 Recall/F1 为 `null`，不能报告为 100%。**门槛通过不等于生产验收通过。**

## 限制 / 下一阶段

- `section_membership_proxy` 只是“导出章节 ID 存在于章节树”对人工正确归属标签的代理分类性能，不代表完整章节切分 Precision/Recall。
- `crosspage_geometry_candidate` 的 Recall 仅针对**被 PDF 几何识别的全部相邻页表格配对**，不包括原生解析漏掉的表格。
- `stage1-gold-v3` 中 `MULTILINGUAL` 标签可度量日英来源中的 Requirement 检测；并**不评价双语句对等价准确率**。
- 人审 identity 只做声明检查，没有第三方身份验证，也没有真实 DCM/550 Gold 基准语料随 ZIP 提供。
- 无法替代 Windows GUI、真实 PostgreSQL、原始 PDF/截图和大文件性能实机验收；它们仍是 v1.28 生产验收前的必需工作。
