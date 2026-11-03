# v1.31 · Stage 2 内容级工程关系：双端证据审查操作规程

## 范围与版本

本次使用的真实完整源码基线是 GitHub `main` commit `2a41f0c` / v1.30。v1.31 为一个干净的增量完整源码包。**不能覆盖不同代码谱系的 v2.4.22**；若要将这段修改移植到更新的本地目录，应先进行三方 diff 和逐项回归。

Stage 2 的对象是**段落/要求 ↔ 段落/要求**关系，不是扩大语义向量检索。每条关系必须展示双方可定位原文（PostgreSQL `contents` 原始文本 + `ei_requirement_units` 原始段落）、文档及版本、页码、章节、Unicode 起止偏移、文本哈希、候选原因、人工审核状态/理由。跨文档仅有概括性引用而没有唯一目标段落时，只生成 unresolved anchor，不能凭语义相似度制造确定关系。

本版加强已有 `ei_content_relations`、`ei_relation_review_audit` 的审核/AI 检索，不新增 SQL 表、不回写源文档，也不修改 Stage 1。输入文档角色遵循既有 `PrimarySpecificationDocumentPolicy`：具体例子 `Bluetooth Audio function spec v2.52.pdf` 是本体；`550_Bluetooth Audio Function Spec_Cover_Sheet_Ver.2.50改(電子承認)` 是 Coversheet，不可作为技术要求的关系终点。

## 启动前置

- 只在完成 PostgreSQL 备份和版本/项目检查后操作。企业 `ei_* v7` 与 `Stage 1 v2` schema 必须已在目标规格库安装，当前项目对应的版本必须已经**完整**回填并有相匹配的 receipt。
- 使用专用、最小权限 PostgreSQL 角色和受审计的终端；**不要将普通 AI/查询应用角色授予对 `ei_content_relations`、`ei_relation_review_audit` 的直接写权限**。本脚本尚未实现可信个人身份签名、强制数据库端受控函数或防超管篡改的不可变审计，因此操作者的 `--actor` 值仅是声明。
- 现有 v7 schema **不保证**阻止拥有表写权限的其他进程绕过 CLI 直接改变关系状态或审计；不能将本版标成不可抵赖审计或合规审计完成。
- 统一在相应项目数据库操作（例如 `rag_21mm`），**禁止 `rag_qabug`**。设置 `POSTGRES_PASSWORD` 环境变量或使用交互式密码询问，切勿在命令行打印密码。

## Windows PowerShell（先只读，再手动复核）

以下命令需在 v1.31 项目根目录执行，使用项目现有 Python 3.12 venv。

```powershell
# 1. 列出尚待人工审核的双端关系（不写数据库）
.\.venv\Scripts\python.exe .\scripts\review_engineering_relation.py `
  --database rag_21mm --project 21MM --user <授权只读用户> --list --limit 50

# 2. 选用队列中的实际 ID；打印原始完整双端原文、页码、精确偏移及 SHA-256
.\.venv\Scripts\python.exe .\scripts\review_engineering_relation.py `
  --database rag_21mm --project 21MM --user <授权只读用户> --candidate-id 123

# 3. 审核人员检查 508、550 两端的实体、版次、章节、条件和技术逻辑后显式批准
# 必须提供审核写权限；如状态已变化则中止，不会自动覆盖或重复追加审核
.\.venv\Scripts\python.exe .\scripts\review_engineering_relation.py `
  --database rag_21mm --project 21MM --user <授权审核用户> --candidate-id 123 `
  --expected-status REVIEW_REQUIRED --status CONFIRMED `
  --actor "reviewer-identifier" --reason "Manually verified both authoritative source paragraphs and linkage" --apply
```

以上 `123` 为**示例 ID**；在实际队列确定前不可视为存在记录。`--actor` 不等于已经完成统一身份认证。`--reason` 的命令行参数可能进入本机 shell 历史或进程查看工具；禁止填入内部正文/凭据/个人隐私材料，只写最小化审核理由。`--list --status CONFIRMED` 可查看当前已确认的条目；状态撤回必须明确 `--expected-status CONFIRMED --status REVIEW_REQUIRED`、理由及 `--apply`。拒绝状态也必须先返回 `REVIEW_REQUIRED` 才能重新批准。

## 强制护栏

| 门禁 | 未满足时的行为 |
| --- | --- |
| 项目数据库身份、双端项目、当前规格主版本匹配 | 不准审核；AI 无效关系不返回 |
| 单侧不是正式正文/对应版本原始 `document_id` | 不准审核；AI 查询使用双端 SQL 主文档谓词 |
| 原始 `contents.content` 与候选 `ei_requirement_units` 的偏移/原文/SHA-256 不相等 | 拒绝；不根据模糊相似度修补 |
| Stage 1 完整回填凭据缺失、旧状态没有相应审核事件 | 拒绝新的审批 |
| 候选未审核/已拒绝/已过期 | 不得进入 AI 内容关系证据 |
| CONFIRMED 但缺失最后一条确认审核事件 | AI 排除，提示运维排查，不自动修复 |
| 审批状态冲突、重复状态或试图跳过重开复审 | 拒绝修改，不生成虚假审核记录 |

AI 问答显示的单侧原文最多 1800 个 Unicode 字符；出现 `[TRUNCATED]` 表示**只能参考片段**。完整原文只在受控 PostgreSQL 源与审核预览中获取，不能认为模型已验证截断之后的内容。

## 测试与尚未通过的企业验收

- 本地已进行 Python 全量离线回归，包括真实语句模板的静态断言、模拟 PostgreSQL 游标、Coversheet 与真实本体文件名反例、证据污染、跨项目、历史版、并发冲突、审计链不匹配与拒绝状态转换。
- 尚未完成：真实 PostgreSQL 17 事务/权限/锁竞争；Windows 11/PowerShell 5.1；GUI/Open WebUI 真正的多用户隔离；真实 508→550 物理 PDF 双端审核和关系验收；关系规模/P95；受控数据库函数、角色分权与不可抵赖身份审计。**上述事项未完成，不得发布为“企业生产闭环验收通过”。**
- 新审核模块不会自动写入旧的 v2.4.22 项目；若那一版已有 Stage 2 图谱与审核，应首先做数据字典/SQL 与新版冲突比对。
