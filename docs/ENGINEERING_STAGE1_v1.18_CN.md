# Engineering Intelligence — Stage 1 v1.18：过滤 JSON 离线诊断与后续分析准备

> **Stage 1 尚未完成真实式样书准确率验收。** 这次专门解决没有权限分享 508/550 原件时，如何在**脱敏或合成示例**上安全重现并定位要求抽取、原子拆分、条件/例外作用域、表格识别的问题。没有真实原件、可信绑定和人工标注，不能证明生产准确率。

## 1. 与 v1.17 的关系

保留原有 `stage1-gold-v1/v2/v3`、人工审核账本、DOCX/PDF 物理证据、API/Open WebUI/SQL/Qdrant/Ollama 和所有既有迁移。新增两个纯离线文件：

- `app/engineering_intelligence/filtered_json_diagnostics.py` — 在现有 Stage 1 抽取器上运行离线证据诊断；
- `scripts/inspect_stage1_filtered_json.py` — Windows/Linux 命令行，只写元数据级报告，不向网络、数据库或模型发送正文。

**本次无数据库迁移。** 扫描的字符位置始终相对于所提供的 JSON `text` 字段。原始文件经过脱敏/替换/截取后，不能将该字符位置写成正式 PDF 坐标或 PostgreSQL Requirement 原文位置，即使其 `offset_policy` 声称长度保留。

## 2. 建议未来提供的过滤 JSON 格式

使用 `examples/stage1_filtered_samples_v1_template.json` 格式，所有值必须来自你已检查过的过滤后数据；以下示例只展示字段结构，**不是正式式样书数据**：

```json
{
  "schema": "stage1-filtered-samples-v1",
  "manifest": {
    "transformation": "REPLACED",
    "offset_policy": "CHANGED"
  },
  "cases": [
    {
      "project": "21MM",
      "document_id": "filtered-example-001",
      "content_id": 1,
      "section_id": "4.2",
      "source_kind": "DOCX_TEXT",
      "complexity_tags": ["NESTED_CONDITION"],
      "text": "If A is active, ECU shall notify B.",
      "source_sha256": "此处填写以上text按UTF-8编码的真实小写SHA256"
    }
  ]
}
```

**必需元数据：** `project` 限定为 21MM/24MM；`content_id` 为正整数，可用过滤样本内部重新分配的序号（与真实 SQL ID **无关联**）；`document_id` 和 `section_id` 可以使用不含敏感信息的代号；`source_kind` 为 `DOCX_TEXT / DOCX_TABLE / PDF_TEXT / PDF_TABLE`；`source_sha256` 是**过滤后的 `text`** 的 SHA-256，绝不能直接复制原文 SHA 作为过滤内容的摘要。

`manifest.transformation` 可为 `MASKED / REPLACED / EXCERPTED / SYNTHETIC`；`offset_policy` 可为 `PRESERVED / CHANGED / UNKNOWN`，均不会因此产生原文可信绑定。只读诊断也接受已经具备上述字段的 `stage1-source-items-v1` 格式，但仍将其视为**来源未经核验**的诊断文本，不会直接计算 Gold 准确率。

> 使用 PowerShell 计算过滤文本的真实 SHA（以下为示例文本）：
>
> ```powershell
> .\.venv\Scripts\python.exe -c "import hashlib; print(hashlib.sha256('If A is active, ECU shall notify B.'.encode('utf-8')).hexdigest())"
> ```

过滤时保留可以重现解析错误的**语法结构**（判断条件、词序、分隔符、表格换行/列号），但用中性符号代替工程机密。若过滤改变字符长度或换行，原文位置与抽取行为就可能发生改变；这也是为什么报告必须标注 `FILTERED_TEXT_OFFSETS_APPLY_ONLY_TO_SUPPLIED_TEXT`。

## 3. 离线诊断命令

```powershell
cd 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.18_CN_FULL'
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_filtered_json.py `
  --json 'C:\authorized\filtered_stage1.json' `
  --report-out 'C:\authorized\filtered_stage1_report.json' `
  --max-cases 500 --max-chars 3000000 --max-findings-per-case 10
```

- 输出的 JSON 仅包含项目类型、来源类别、**哈希化**来源标识、原文相对字符区间、匿名状态/风险计数和整体数量；不输出文本片段或明文文件名。
- 原子要求只是 `REQUIREMENT_CANDIDATE`，范围不明的条件仍会显示 `UNRESOLVED`；不自动批准工程规则。
- `quality_metrics_available=false`，不会生成 Precision、Recall、F1。准确率测量依然需要由工程人员在授权本地构建并双人审核的 Gold v3 数据集。
- 输入文件超过 12 MB、过多用例/字符、SHA 不匹配、重复范围身份、不明项目或来源格式，**拒绝诊断**。输出使用安全新建策略，已有结果不会覆盖。
- 仅对邮件、IP、Windows 路径、凭据标签进行粗略风险扫描，**不是脱敏完成证明**。哈希及字符偏移仍可能带来关联风险；分享任何 JSON 前必须由你人工确认符合公司数据政策。
- **不要把原始数据库的敏感 `document_id`、来源路径、真文件名、原文 Token 或 QA/BUG 个人信息直接复制到本模板。**

## 4. 在缺少原始式样书时仍能完成的检查

1. 从合成的跨段/跨条件、中文/日文/英文、Markdown 表格等样本构建回归测试；逐项验证原文未损坏、字符偏移完全对应、候选不会被自动批准。
2. 用过滤 JSON 的报告了解哪些规则产生 `UNRESOLVED`、哪些表格缺少结构来源、哪些长段落超过预算；报告不代表正式数据精度。
3. 由你在本地人工核实哪些脱敏测试能等价复现真实解析缺陷。之后可以把**已经再次检查可对外分享的过滤 JSON 或匿名诊断报告**交给我，在不索取原件的情况下针对明确错误修改 Stage 1 抽取逻辑。
4. 最终上线前必须对保留在授权主机的真实式样进行 Gold v3 留出集和 Windows/PostgreSQL 审核验收；没有这些只能算离线研发就绪，**不能宣布 Stage 1 企业级验收完成**。

## 5. 验收与边界

- 当前容器已运行 452 项选定版本回归通过、2 项缺少真实样本而跳过；Python 源码编译通过。全量测试收集因原工程缺少 psycopg、ttkbootstrap、测试 PDF 等既有条件而出现 8 项阻断，**未通过全量验收**。
- 已修复旧要求线索中中文“应当”内的“当”被误识别为独立条件的问题。新增安全/偏移/隐私/异常/CLI 回归测试覆盖这一诊断通道。
- 本版本不连接网络服务、不会写 PostgreSQL、Qdrant，不改变原有模型问答，也不创建正式 QA/BUG 关联。
- 已有 v1.17 审核与评分能力保持原样。下一阶段工作应优先依据可合法提供的过滤样本修复真正可重现的要求/作用域/表格误判；若无法取得样本，则保持“未实测”，不能靠重复增加验收模板宣布完成。
