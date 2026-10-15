# Engineering Intelligence — Stage 1 v1.17：真实人工评估与分层验收

> **本版仍在 Stage 1**。以下开发代码已通过合成/回归测试，不表示 508/550 真实标注、复杂表格逻辑或 Windows PostgreSQL 实际审批已验收；不能把标注人员的声明等同于强身份认证。Gold 原文仅限授权本地保存，严禁上传公网服务或提交 GitHub。

## A. 解决 v1.16 的不足

旧 `stage1-gold-v2` 可以衡量原子要求、条件/例外/动作的精确区间，却难以回答“数据是否完成复核、是否混入了开发样本、合并表格等困难样本是否被覆盖”。v1.17 提供 `stage1-gold-v3`；旧 v1/v2 继续可评估，但结果标为 `unspecified_legacy`，不能因此宣称已经获得独立复核的留出集。

每条样本强制要求以下字段：

- `project`：仅 `21MM`、`24MM`；同名 `document_id/content_id` 在不同项目中不合并。
- `source_kind`：`DOCX_TEXT`、`DOCX_TABLE`、`PDF_TEXT`、`PDF_TABLE`，由有权限的工程人员根据来源文件明确填写。
- `split`：`development` 或 `holdout`；**只统计 holdout 作为对外报告**，保留开发集用于防止泄漏、检查来源快照。
- `complexity_tags`：`NORMAL` / `MERGED_HEADER` / `CROSS_PAGE` / `NESTED_CONDITION` / `EXCEPTION` / `MULTILINGUAL` / `NO_REQUIREMENT` / `NESTED_TABLE`。复杂场景必须在抽样时真实覆盖，不能只填 `NORMAL`。
- `annotation_state` 必须为 `VERIFIED`；其 `independent_review` 包含不同的 `annotator_id`、`reviewer_id`、明确的复核说明和覆盖标签+来源归属的 `reviewed_label_sha256`。**只验证声明与摘要的一致性，不证明人员真实身份、审核独立性或标注正确性。**
- `dataset.source_fingerprint` 固定样本项目、来源 ID、原文 SHA、子集划分及复杂度标签。变动后必须重新选集和复核，不能静默重算替代复核。

报告包含：要求级 Precision/Recall/F1、条件/例外/动作独立统计、按 DOCX/PDF 正文/表格、复杂度以及 21MM/24MM 分组的质量指标。对完全没有已标注正例的分组，F1 是 `null`，并保留误检计数，**不是 1.00**。

产生漏检/误检时输出不超过 40 个 `source_identity_sha256_prefix`、来源类型及字符偏移，不记录完整原文/审核人备注。使用 Hash 代替原始文件名和 Content ID，并限制每项坐标数量，便于授权人员回到受控系统核查。

## B. 本地标注/复核操作（PowerShell）

首先从你**已经授权读取的正式 PostgreSQL 原文及可信 PDF/DOCX** 提取本地 `stage1-source-items-v1`（此步骤必须自行获得适当权限），并在每条来源上显式填写 `project`、`source_kind`、`split`、`complexity_tags` 等元数据。输入 JSON 示例：

```json
{
  "schema": "stage1-source-items-v1",
  "cases": []
}
```

空示例**不是可测量标注集**，且不会通过预生成检查。真实源数据应包含实际 `document_id`、正整数 `content_id`、`section_id`、原始 `text`、可选匹配的 `source_sha256`、上述工程来源元数据。不要编造内容或上传公司文件。

```powershell
# 只创建未标注的、含私有原文的本地副本。绝不覆盖既有人工标注文件。
.\.venv\Scripts\python.exe .\scripts\prepare_stage1_gold_v3.py `
  --source-json 'C:\authorized\stage1_source.json' `
  --out 'C:\authorized\stage1_gold_v3.json' `
  --dataset-id '21MM-508-550-audit-2026' `
  --confirm-local-sensitive-copy
```

人工标注人逐项核对实际版本、正例/负例、字符区间与作用域。将完成项目的 `annotation_state` 暂设为 `READY_FOR_REVIEW`。**另一名有权限的审核人**先查看实际文件、标签和分组归属，再运行如下只读指纹打印工具，核对摘要，在 JSON 中手动录入 `annotator_id` / `reviewer_id` / `review_note` / `reviewed_label_sha256`，并显式修改状态为 `VERIFIED`；工具不会自动签核、写数据库或生成模拟答案。

```powershell
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_gold_v3_digests.py `
  --gold 'C:\authorized\stage1_gold_v3.json'

# 评分只读，默认没有所谓“已经企业验收”的推断。
.\.venv\Scripts\python.exe .\scripts\evaluate_stage1_gold.py `
  --gold 'C:\authorized\stage1_gold_v3_reviewed.json' `
  --min-holdout-cases 40 --min-negative-cases 8 `
  --require-source-kind DOCX_TEXT --require-source-kind DOCX_TABLE `
  --min-source-kind-cases 5 --min-requirement-f1 0.90 --min-scope-f1 0.90
```

上述 40 / 8 / 5 / 0.90 **仅用于演示由企业自行制定的门槛**，不是已达标或通用行业标准。必须覆盖真实表格、跨页、合并、异常及多语言样本。`--min-source-kind-f1` 可追加要求每个被指定的来源类别均达到最低 Requirement F1；没有正例的分组无法达到此 F1 门槛，必须重新抽样。未达到门槛返回 exit code **2**，无法评分或来源漂移则抛错退出。

可再强制验证**困难样本覆盖和分角色准确率**，例如：

```powershell
.\.venv\Scripts\python.exe .\scripts\evaluate_stage1_gold.py `
  --gold 'C:\authorized\stage1_gold_v3_reviewed.json' `
  --require-complexity-tag MERGED_HEADER --require-complexity-tag CROSS_PAGE `
  --require-complexity-tag EXCEPTION --min-tag-cases 5 `
  --min-condition-f1 0.85 --min-exception-f1 0.85 --min-action-f1 0.90
```

只有人工复核的 `holdout` 样本才参与门槛。某个角色没有真实正例时，其 F1 为 `null`，如果对该角色指定最低 F1，**验收失败**；不会借总体 Scope F1 掩盖例外规则。用户可再使用 `--min-tag-f1 0.85`，要求所有已指定复杂度标签的 Requirement F1 达标。`--min-tag-f1` 但未指定对应标签、或旧 v1/v2 使用 v3 特有门槛均直接拒绝，避免参数被静默忽略。


本地管理员桌面审核窗口新增 **Gold v3 质量报告** 标签：选择本机 JSON，在后台离线测量并展示聚合结果，不发送给 FastAPI、Ollama、Qdrant 或公网，也不向 PostgreSQL 提交审查结论。评价不负责核验审核者身份、样本是否随机、训练集是否泄漏或文本的业务真实性。

## C. 后续 Stage 1 验收缺口

1. 使用真正的 508/550 PDF/DOCX 及人工双人标注，分别覆盖两个项目、多个版本与困难表格；留出集不能用于反复调 Prompt 或微调规则。
2. 对合并/跨页表头与条件继承做正式工程语义审核，不能把物理位置匹配或高 F1 自动解释为规则已经成立。
3. 在真实 Windows + PostgreSQL 中验证迁移、数据库最小权限、并发审核与 GUI 操作，并执行 P50/P95 真实性能测试。
4. 完成至少一个可重现的真实全链路导入→抽取→核验→审核→评估流程后，才能考虑宣布 Stage 1 验收完成并进入 Stage 2。

---

# Engineering Intelligence — Stage 1 v1.16：人工标注质量门槛与物理表头证据矩阵

> 本版是 v1.15 的增量完善，**仍属于 Stage 1**。没有真正运行企业 508/550 的人工标注验收，没有完成 Windows PostgreSQL 实机权限/并发测试；不能宣称达到全部生产验收指标。以下工具都是离线、本地、只读（标注模板会写本机显式指定的文件，但不写 SQL）。

## A. 新增能力：真实人工 Gold v2（要求 + 工程作用域分项）

v1.14/v1.15 的 `stage1-gold-v1` 只能评价原子要求的准确字符边界。v1.16 新增 `stage1-gold-v2`：

- **原子要求**：沿用精确范围匹配 Precision / Recall / F1；不把“部分重合”当作正确。
- **条件/例外/动作**：由人工对每一条正例要求填写 `scope_annotations`，明确 `CONDITION_GOVERNS_ACTION` / `EXCEPTION_OVERRIDES_ACTION` / `ACTION_ONLY` / `UNRESOLVED`，分别填写绝对字符区间。指标独立计算 exact-span P/R/F1，不用第一个指标冒充第二个指标。
- **未解决情况**：只允许 `UNRESOLVED` 且没有确定的作用域边界；不生成虚假的正例。当整套 Gold 没有任何人工标注的有效 scope 正例时，`scope_f1` 返回 `null`，而不是伪造 100%。
- **严格源证据**：保留真实 `document_id`、`content_id`、`section_id`、SQL 文本及其 SHA-256。重复来源、字符越界、重叠 Gold、未完整标注、来源摘要漂移全部拒绝。
- **可选质量门槛**：`--min-requirement-f1` 和 `--min-scope-f1`。达不到门槛时 exit code 2，便于本地自动验收；不代表部署环境已达到门槛。
- **向后兼容**：仍接受人工填写的 `stage1-gold-v1`，但旧集没有作用域标签，**不能**用来测量 Scope F1。

示例结构（**格式说明，不是真实数据或验收成绩**）：

```json
{
  "schema": "stage1-gold-v2",
  "cases": [
    {
      "document_id": "由实际来源提供的ID",
      "content_id": 100,
      "section_id": "实际章节",
      "text": "ECU shall notify.",
      "source_sha256": "必须是该 text 的真实 sha256",
      "expected_requirement_spans": [[0, 17]],
      "scope_annotations": [
        {
          "requirement_span": [0, 17],
          "status": "ACTION_ONLY",
          "action_span": [0, 17]
        }
      ]
    }
  ]
}
```

这里的数字只用于解释数据结构；不要将示例伪装为 508/550 人工标注。偏移按 Python Unicode **码位**（非字节/UTF-16）计算，包含条件后的逗号、动作开始的空白等精确原文。`expected_requirement_spans` 可以空（完整标注的负例），但**整套数据必须包含至少一条人工确认的正例**才会产出 Requirement 质量指标。

Windows PowerShell：

```powershell
# 已经得到经授权的原文片段本地 JSON（schema=stage1-source-items-v1），再显式生成待标注副本；
# 输入 cases 必须包含 document_id、content_id、section_id、text，非 AI 预测结果。
.\.venv\Scripts\python.exe .\scripts\prepare_stage1_gold_v2.py `
    --source-json "C:\authorized\stage1_source_rows.json" `
    --out "C:\authorized\stage1_gold_to_annotate.json" `
    --confirm-local-sensitive-copy

# 人工填完每条要求和作用域后，才能真实评价；不必访问公网或模型。
.\.venv\Scripts\python.exe .\scripts\evaluate_stage1_gold.py `
    --gold "C:\authorized\stage1_gold_reviewed.json" `
    --min-requirement-f1 0.90 --min-scope-f1 0.90
```

上面 `0.90` 是**用户自行配置的示例验收门槛，不代表已达标或最终工程标准**。标注集应覆盖 508/550、不同历史版本、中日英混合、表格/合并/跨页、没有要求的段落、嵌套条件和 NA 例外。记录每个子集的误报、漏报和代表性原文差异，再决定是否进入 Stage 2。标注应由至少两名有权限的工程人员独立复核有争议的条款；此处不自动假定所有标注正确。

⚠️ `prepare_stage1_gold_v2.py` 产生的是**未标注、含私有原文的本机副本**。程序拒绝覆盖同名文件，不能直接使用此副本计算“完成验收”，不得上传云端或存入 GitHub。Windows 文件权限取决于目标目录 ACL，应放在授权的受控目录。ZIP 内 `examples/stage1_gold_v2_template.json` 刻意为空。

## B. 新增能力：物理表头—列—数据行的显式映射预览

继承 v1.15 的 **可信 PDF/DOCX SHA → SQL Content ID → 原文偏移 → Stage 1 审核提案** 检查，本版允许审查者明确指定某一张物理表及其表头行，输出：

- 每列实际已绑定的表头原生单元格（`row`、`column`、`anchor_key`、`content_id`、SQL 起止偏移、SHA）。
- 同一列实际已绑定的数据行单元格；对于跨列/纵向合并、短文字不能唯一定位、无可靠表头、不完整映射、PDF 几何或跨页，保留明确的审查标记。
- **不自动选择表头**、不自动合并跨页表格、不把物理列对应视为正式业务约束。`status` 永远为 `REVIEW_ONLY`；结构信息只能作为人工审核辅助材料。

只读 CLI（必须使用真实项目、版本和可信文件）：

```powershell
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_sql_binding.py `
    --file "C:\authorized\550.docx" --project 21MM --database rag_21mm `
    --version-id 123 --user stage1_reader `
    --review-proposal-key '<真实64位审核提案Key>' `
    --header-review-table 'DOCX:0' --header-rows '0,1' `
    --max-table-details 20
```

`123`、`DOCX:0`、行号均是占位示例，必须由原文件证据决定。`--header-rows` 使用**物理零基行号**，不是式样书页码。支持多个明确选择的表头行（如 `0,1`），但合并、遗漏和前置行仍需要人工解释；不会因此解除结构审核标记。GUI 的 `PDF/DOCX ↔ SQL 精确证据` 标签也有“人工选择表头物理行”，只针对原文已**全部唯一对应**的一张 TABLE 候选生成预览。

所有输出默认避免工程原文，包含标识、偏移和哈希；打开 GUI 查看原文仍需有授权和本地屏幕安全措施。

## C. 兼容性与尚未通过的验收

- **无新增数据库迁移**：保留 v1.13 的 `ei_stage1_review_*` 账本，既有 `ei_*` 审核结果、PostgreSQL/Qdrant、Open WebUI、Ollama 均不自动修改。
- **Stage 1 未完成**：当前仍缺少 508/550 的真实人工标注集、复杂跨页/合并表头的正式语义确认，以及 Windows PostgreSQL 权限/事务实机验证；这些必须由持有正式式样书权限的人员在目标设备执行。
- **工程准确性优先**：文本精确相同只证明物理来源，不证明具体工程含义；人工审核的任何状态也不能被 Agent 当成对所有潜在依赖的确定结论。
- 所有离线新增节点受文档、字符、表格单元格和 SQL 映射预算约束。真实大文档处理时间仍需在用户的机器上基准测试。

---

# Engineering Intelligence — Stage 1 v1.15：人工审核与原生文件证据双栏对照

## v1.15 实际新增（保留 v1.13/v1.14 全部审核规则）

- 本地管理员打开 **Settings → PostgreSQL Actions → Stage 1 工程证据人工审核**，选择项目、真实版本、数据库审查账户及审核提案。
- 切换到 **PDF/DOCX ↔ SQL 精确证据** 标签，选择已获授权的原始 `.docx` / `.pdf`。该操作是**显式、只读**的，不自动扫描本地文件或读取其他项目。
- 程序在同一个 PostgreSQL **REPEATABLE READ READ ONLY** 快照中，重新读取该审核提案及审核事件版本，校验 `project_info` 项目身份、version 所属项目、可信 `documents.source_hash` 与真实文件 SHA-256、版本回填收据、原始 SQL 全文，再使用原有精确来源绑定器建立唯一候选。
- 左侧显示 SQL 原始文本片段，并对经过一致性校验的候选区间高亮；右侧显示已匹配 DOCX 正文段落、物理表格单元格，或 PDF 原生行/几何单元格位置，及未解决的结构限制。**高亮只能表示原文位置候选，不代表规范性条件、表头或跨页继承已经通过审核。**
- 如果文件版本不一致、SQL 漂移、回填不完整、超出资源预算、候选多义、缺失原生证据，会报错或显示待审核状态，不会自动批准，也不会更新审核事件。
- 人工审核入口及追加式 CAS 审核账本保持原样。提交仍需要审核人 ID、理由、备份确认及授权数据库账号；这次无需新增 SQL 迁移。
- 本版仍为 **完整 Python 源码 ZIP**，不是 EXE。请勿把当前 EXE 与此版本误认为同一构建。

### 无 GUI 的只读提案对照（Windows PowerShell）

适合管理员复核特定 `SCOPE`/`TABLE` 审核候选。先使用既有 `review_stage1_v4.py list/show` 获取当前版本的真实 64 位 proposal key；命令行默认仅输出 SHA、原文匹配状态、单元格坐标和截断标记，不打印源文件全文或数据库凭据。

```powershell
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_sql_binding.py `
  --file "C:\path\to\authorized-508.docx" `
  --project 21MM --database rag_21mm --version-id 123 `
  --user stage1_reader `
  --review-proposal-key '<实际的64位proposal key>' `
  --max-table-details 20
```

必须使用真实文件、真实项目/版本和已获得访问授权的只读数据库角色。`123` 和 `<...>` 均为占位示例。如果缺少可信源文件哈希、v7/v3/v4 审核回填或文件内容出现漂移，命令拒绝给出通过结论。该操作不替代人工审核，也不会写任何正式关系。

### 使用前提与限制

1. 原始 PDF/DOCX 的二进制 SHA-256 必须和所选工程版本中可信 SQL 记录一致。若旧数据没有真实原文件 `source_hash`，拒绝建立精确映射，不允许改填任意哈希绕过。
2. 只读对照需要该数据库账号拥有读取版本元数据、`contents`、`ei_requirement_units`、来源收据和审核提案的相应权限。正式审批仍需单独最小权限角色，不能把 Open WebUI 共享 Token 当数据库审查权限。
3. PDF 几何连续性、DOCX 合并表头、条件/例外优先级无法仅凭位置自动证明；不清楚的关系必须选 `NEEDS_CLARIFICATION`，不得标为已验证。
4. **没有真实 508/550 的经人工标注数据，Stage 1 准确率仍未验收**。Windows GUI/真实 PostgreSQL 事务权限及多审核人并发也尚未在你的主机实际执行。
5. 此功能不修改 Qdrant、既有 PostgreSQL `ei_*` 记录、Open WebUI 聊天接口，也不调用云端模型或在聊天中重新解析大文件。

---

# Engineering Intelligence — Stage 1 v1.14：人工审核桌面窗口、原生表格绑定与真实标注评估

## 本次已交付的新增能力

- **本地 Tkinter 人工审核窗口**：Settings → PostgreSQL Actions → `Stage 1 · 工程证据人工审核（独立窗口）`；也可直接运行 `python -m app.gui.stage1_review_window` 或 `scripts/review_stage1_gui.py`。窗口只运行在管理员电脑，不新增任何 WebUI/FastAPI 审核 API。
- **只读队列**：用户需明确输入 `21MM/24MM`、合法 version_id 和数据库只读用户及密码，支持 50 条/页、SCOPE/TABLE 分类、完整收据检查、SHA256 防漂移校验、原始证据 JSON 和历史审核记录。
- **人工确认**：只有填写 Reviewer ID、审核理由、明确选择决定、备份确认及最终确认弹窗，才在 SERIALIZABLE 事务中通过原有 `record_review_event` 追加事件。条件/例外语义不明时仍然不能确认。审核列表不能替代实际式样书的原文核对。
- **原生表格—SQL 证据预览**：可在 SQL 来源绑定检查后加 `--native-table-review`，查看 DOCX 物理单元格与 `contents.id` / 字符偏移 / Requirement Unit Key 的唯一匹配；PDF 只能基于唯一包含在几何单元格内的原生文本行提供保守候选。合并单元格、表头以及跨页继续保持待审核。默认不输出原文，输出细项有条数上限。
- **真实样本评估**：`scripts/evaluate_stage1_gold.py` 支持 `schema: stage1-gold-v1` 的人工标注测试集，只有 SHA256 与精确 Unicode 字符偏移均有效时才计算 Requirement Candidate 的 Precision/Recall/F1。模板在 `examples/stage1_gold_template.json`，**模板为空，不是测试通过的假数据**。
- **版本兼容**：继续使用 v1.13 数据库 `ei_stage1_review_*` 表，无新增迁移、不覆盖人工审核、不修改 Qdrant / Ollama / Open WebUI 的业务查询接口。

## 快速使用（Windows PowerShell）

前提：Python 3.12 虚拟环境已安装项目 `requirements.txt`；数据库已备份、Stage1 v2/v3/v4 迁移及回填由管理员按下文步骤完成。**不要用 WebUI 的共享 Token 或日常问答账号审批**。建议数据库安全团队配置最小权限 `stage1_reviewer` 与 `stage1_reader` 账号，不应默认使用 `postgres` 超级用户。

```powershell
cd "C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.14_CN_FULL"
.\.venv\Scripts\python.exe .\scripts\review_stage1_gui.py
```

在审核窗口：输入项目、version_id、DB 连接和专用审查账号；按“加载候选”；选中候选核对其原始内容与历史；只有确实完成审核时才提交。密码仅供当前窗口连接，不写入项目配置文件。

有本地授权的文档源和数据库身份时，可查看原生表格证据（**只读**）：

```powershell
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_sql_binding.py `
  --file "C:\path\to\authorized-spec.docx" `
  --project 21MM --database rag_21mm --version-id 123 `
  --native-table-review --max-table-details 20
```

验证人工标注要求位置（**只读，不联网**）：

```powershell
.\.venv\Scripts\python.exe .\scripts\evaluate_stage1_gold.py `
  --gold "C:\path\to\stage1_verified_gold.json"
```

标注 JSON 结构：`{"schema":"stage1-gold-v1","cases":[{"document_id":"真实文档标识","content_id":101,"section_id":"实际章节","text":"源数据库 content 原始文本","source_sha256":"对 text 字符串的 UTF-8 SHA256","expected_requirement_spans":[[0,10]]}]}`。这是**字段格式示意，不是可直接运行的准确率标注**。偏移按 Python Unicode 字符计数，不能直接使用 Excel 的 UTF-16 索引；必须由人工在原始内容上标注真实要求边界。若标注集为空、摘要改变或越界，会拒绝给出评估结果。

## 已知限制与 Stage 1 下一步

1. 未拿到经过授权、人工标注的真实 508/550 样本，因此**准确率无正式结论**。合成测试用于代码逻辑验证，不代表生产域精度。
2. native↔SQL 只读表格报告还没有和 `ei_stage1_review_proposals` 中全部 Markdown/Word/PDF 表格语义实现统一关联；复杂合并表头与跨页条件继承均不得自动确认。
3. 当前 GUI 只能显示候选提案 JSON 与审核历史，尚不是所见即所得 PDF 页面高亮及双栏可视化审批；真正源文件预览和细粒度表格审批仍需后续建设。
4. Linux 测试环境未连接你的 Windows PostgreSQL，不能宣称真实 SQL 迁移、并发冲突、权限隔离或 GUI 交互已经实机验收。
5. 仅限**已登录 Windows 用户会话的桌面管理**；本地审核窗口不开局域网端口。

---

## v1.13 数据库迁移与人工审核命令（仍适用）

## 本次范围与安全性质

基于 v1.12 **原生来源绑定**、v1.10 **原子候选与 Markdown 表格**，新增：

1. 将已完整回填的版本中每个规范性原子候选转换为 **SCOPE** 审核提案：保留原始段落、条件/例外/动作的逐字文本与绝对字符偏移、Section、页码、SHA-256、分析器版本、`UNRESOLVED` 标记；规则优先级始终初始为 `UNDETERMINED`。
2. 将已完整回填的 v3 表格证据转换为 **TABLE** 审核提案：保留表行、单元格、列号、原文偏移和潜在表头；**不自动确认跨页表头、跨行条件或合并单元格含义**。
3. 新增 **追加式审计事件**。只有人工调用并提交明确身份证明、状态、理由、原文 SHA、当前事件 ID 才可确认。新事件不修改旧事件。冲突、漂移、未完整回填、缺证据的语义断言全部拒绝。
4. 数据库独立新增 `ei_stage1_review_proposals`、`ei_stage1_review_receipts`、`ei_stage1_review_events`。触发器禁止 `UPDATE/DELETE`，不会修改 `ei_requirement_units`、`ei_atomic_candidates`、`ei_table_evidence`、原有已审核关系、Qdrant 或 Open WebUI API。
5. 提供 **只读预演 / 显式回填 / 审核候选清单 / 人工审核** 终端脚本，默认不在聊天时运行，也不调用 LLM 或云服务。

注意：`CONFIRMED` 是**人工对特定证据与明确关系类型的确认**，不是 AI 的自动推理结论。对于多条件嵌套、跨段继承、缺失表头、错列、未校验合并单元格等问题，应标记 `NEEDS_CLARIFICATION`，本版本不允许把 `UNRESOLVED` 直接批准为确定工程规则。强约束条件/例外优先级必须人工阅读原文后显式确认。

## 数据与事务前置条件

仅在授权的 `rag_21mm` 或 `rag_24mm` 使用；`rag_qabug` 不运行此迁移。务必先完整备份 PostgreSQL，并按此前文档完成、校验以下迁移与回填：

- `database/upgrade_engineering_intelligence_v7.sql`
- `database/upgrade_engineering_stage1_v2.sql`
- `database/upgrade_engineering_stage1_v3.sql`，对应版本 `ei_stage1_v3_receipts` 完整
- 新增：`database/upgrade_engineering_stage1_v4.sql`（**仅在管理员明确执行时**创建新表）

此迁移包含 Append-only 触发器。请在数据库中使用独立最小权限操作账户分别授予“仅查询提案”和“只追加审核事件”权限；别让普通 WebUI 用户或共享 Backend API Key 持有审批权限。迁移建议专人执行，备份还原演练另行安排。

## 保留的 v1.12 原始版面检查

本版仍保留前一轮的离线检查脚本；这些操作只输出来源证据及审核候选，不建立正式工程依赖：

```powershell
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_native_layout.py --file "C:\your-authorized-spec.docx"
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_native_layout.py --file "C:\your-authorized-spec.pdf" --pdf-table-geometry
```

严格 SQL 来源绑定需要真实文件 SHA-256、已完成的 `ei_version_backfill_receipts`、正确的项目/版本和只读数据库账号：

```powershell
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_sql_binding.py `
    --file "C:\your-authorized-spec.pdf" `
    --project 21MM --database rag_21mm --version-id 123
```

同样不能把 PDF 文字坐标、Word 合并单元格或几何连续性直接视为已确认的业务表头或要求作用域。SQL 检查拒绝模糊文本匹配和来源哈希不一致。

## Windows PowerShell 操作：先预演，后迁移/回填

解压到独立 `v1.13` 目录并安装 `requirements.txt`、`requirements-api.txt`。把版本 ID `123` 换成真实 ID。密码从 Windows 的可信密码管理器/环境中输入，不要保存进日志或脚本。

```powershell
$env:POSTGRES_PASSWORD = '<仅示意；实际从密码管理器安全提供>'
.\.venv\Scripts\python.exe .\scripts\backfill_stage1_v4.py `
    --project 21MM --database rag_21mm --version-id 123
```

如果预演通过且已经完成备份，**管理员独立执行** v4 迁移 SQL（不可借助 Agent 执行）；然后显式回填：

```powershell
.\.venv\Scripts\python.exe .\scripts\backfill_stage1_v4.py `
    --project 21MM --database rag_21mm --version-id 123 `
    --apply --backup-confirmed
```

查询候选（仅元数据，默认不输出工程原文）：

```powershell
.\.venv\Scripts\python.exe .\scripts\review_stage1_v4.py list `
    --project 21MM --database rag_21mm --version-id 123
```

如果操作者有文档数据授权，需要查看某个候选的原文证据：

```powershell
.\.venv\Scripts\python.exe .\scripts\review_stage1_v4.py show `
    --project 21MM --database rag_21mm --version-id 123 `
    --proposal-key '<64位提案KEY>' --show-source
```

人工确认单条件作用域示例：必须从上一步 `show` 获取真实 `evidence_digest`、`expected_event_id`，确认与本地原文一致。不得直接复制示例哈希用于生产。`review` 在 SERIALIZABLE 事务提交审核事件；若发生并发冲突或 40001 serialization failure，需要重新读取最新事件编号和来源后再决定。

```powershell
$requestId = [guid]::NewGuid().ToString()
.\.venv\Scripts\python.exe .\scripts\review_stage1_v4.py review `
    --project 21MM --database rag_21mm --version-id 123 `
    --proposal-key '<真实提案KEY>' `
    --expected-digest '<show返回的evidence_digest>' `
    --expected-event-id 0 --request-id $requestId `
    --reviewer 'engineering-reviewer-id' `
    --reason '已比对指定版本原文及条件、动作边界' `
    --status CONFIRMED --assertion CONDITION_GOVERNS_ACTION `
    --backup-confirmed
```

不同类型：`ACTION_ONLY`、`CONDITION_GOVERNS_ACTION`、`EXCEPTION_OVERRIDES_ACTION` 仅用于匹配已锚定的作用域；`HEADER_ROW_APPLIES` 必须附加 `--target-unit-key '<实际表头行unit_key>'`。不确定时用 `NEEDS_CLARIFICATION` 或 `REJECTED`，且不得附加断言。完成后清理会话中密码环境变量：

```powershell
Remove-Item Env:POSTGRES_PASSWORD
```

> 警告：`show --show-source` 会在本地终端显示工程原文，应遵守企业终端日志/截图/录屏和访问权限要求；平时只用 `list`/`show` 不带 `--show-source` 的元数据模式。

## 一致性与性能边界

- 回填前核对 v7 来源收据、v3 派生收据、逐条旧证据和真实 SQL 全文哈希。新的提案由原文绝对偏移、来源哈希和提取版本构造不可变 ID。
- 回填过程必须在单版本 `REPEATABLE READ` 事务完成，分批追加，完成时验证候选数量、整体摘要和不可变收据；中途错误则事务回滚。
- 审核采用 `SERIALIZABLE` 事务及 CAS 事件序号，数据库从不允许“自动盖章”。同一版本所有提案总数不符合回填收据，禁止审核。
- 使用上限约束（默认最多 40,000 个来源要求、100,000 个原子候选、10,000 个表块、120,000 个审核提案）；超限报错，不返回看似完整的部分结果。
- 这是一次**离线派生**工作，不增加正常 Open WebUI 聊天时的模型调用；实际 PostgreSQL 写入与查询时间仍必须在 Windows 实机测试。

## 尚未完成与 Stage 1 验收限制

1. **正式 508/550 文档的人工标注准确率与复杂 PDF/DOCX 跨页表头审批**。本轮模拟单测不能替代。DOCX 原生的合并/嵌套单元格不能自动等价于已扁平化 SQL Table 结构。
2. **完整 GUI 审批界面与双人复核制度**。目前只有带权限约束的命令行操作、追加事件及状态查询；事件未作为新工程规则自动推送到 Stage 2 图谱。
3. **实际 PostgreSQL 迁移、只读备份校验、Windows 多用户审批并发与模型调用性能验收**。当前容器未接企业数据库，因此不能宣称运行级验证完成。
4. 条件跨章节作用域、例外优先级冲突、复杂跨页表格语义仍需人工确认或经授权的新证据；不可凭语言模型猜测。

**当前仍处于 Stage 1；不要把候选及人工确认入口当成完整工程语义规则引擎。**
