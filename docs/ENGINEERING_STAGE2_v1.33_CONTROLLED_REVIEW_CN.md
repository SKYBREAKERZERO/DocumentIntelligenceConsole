# v1.33 Stage 2 人工审核数据库授权与双端证据闭环

## 变更目标

v1.32 只读图谱 → v1.33 独立本地 GUI + **PostgreSQL SECURITY DEFINER 受控状态转换**。审核人由登录数据库的 `session_user` 确定；不再接受 CLI 传入的 `--actor` 声明。仍保持每条关系：双端完整原文、文档、版本、章节、页码、Unicode 偏移、SHA-256、关系类型、理由、人工审核状态。仅当前正文 SPEC_BODY/PRIMARY_CONTENT 的真实来源可批准。历史关系不自动继承批准。浏览器 HTML 始终只读。

**本次没有修改数据库，也没有安装自动迁移**；DDL 需要 DBA 按库在维护窗口审核运行，并检查影响。原有应用直写 API 在新增受控流程中已停用；未经 DBA 独立权限检验禁止正式使用。

## 认证审计来源与历史数据隔离

本次迁移为 `ei_relation_review_audit` 新增 `identity_provenance`：此前审核事件被标为 `UNVERIFIED_LEGACY`，数据库受控函数生成的新事件标为 `DB_SESSION_USER`。**RAG 只读取有数据库认证身份标记、最后状态 CONFIRMED、审核人身份一致、并且受控函数及两道触发器启用的关系**。如果数据库还没迁移，关系图不会作为技术回答证据；用户其他正文查询不因此自动获得关系证据。迁移不会替旧审批背书，亦不会自动改变历史数据。

对旧版 `CONFIRMED` 关系，持权审核人须先在保留原始审计的前提下显式撤回到 `REVIEW_REQUIRED`，复验当前原文、版本、来源后再通过受控函数确认；不能批量假定旧审批可信。离线 HTML 即使显示 `CONFIRMED` 也不能作为实时 RAG 授权证明。撤销经认证角色的新权限不会追溯撤回旧的已完成审核；账号泄漏须单独按流程撤回可疑边。

## 数据库维护（PostgreSQL 17）

1. 对 `rag_21mm`、`rag_24mm` 分别执行 `pg_dump -Fc` 备份与恢复演练；绝不要在 `rag_qabug` 或无明确工程身份的混合库运行。
2. DBA 用**可信 DDL 所有者**（推荐 `NOLOGIN` 专用数据库 owner 角色，而不是应用运行账号），在**一个**已确定的工程库中执行 `psql -v ON_ERROR_STOP=1 -f database/upgrade_engineering_stage2_v1_33.sql`，再运行 `database/verify_engineering_stage2_v1_33.sql`。检查 `project_count=1`、`enabled_guards=2` 与 SQL 函数存在。
3. 为每名审核员单独创建 PostgreSQL `LOGIN` 凭据，**不能与应用、向量索引、WebUI、导入器、超级用户共享账号**。DBA 明确授权对应数据库、表读取及执行受控函数，不给予任何直接 `UPDATE/DELETE` 或审计表 `INSERT/UPDATE/DELETE`。
4. DBA 执行以下经过用户名替换且审查后的授权 SQL（须是可靠 DDL owner）：

```sql
-- 用真实 DBA 审核过的、无角色继承/无 superuser 权限的 login 替换示例名。
INSERT INTO dic_review.authorized_reviewers(login_name,approved_by,justification)
VALUES ('stage2_reviewer_01', 'dba-admin', 'Spec body content-review authorization approved by DBA');
GRANT USAGE ON SCHEMA dic_review TO stage2_reviewer_01;
GRANT EXECUTE ON FUNCTION dic_review.apply_content_relation_review(
  BIGINT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT) TO stage2_reviewer_01;
GRANT SELECT ON public.project_info, public.ei_requirement_units,
  public.ei_content_relations, public.ei_relation_review_audit,
  public.ei_version_backfill_receipts, public.specifications,
  public.specification_versions, public.documents, public.contents,
  public.ei_stage1_enrichment_receipts TO stage2_reviewer_01;
REVOKE INSERT, UPDATE, DELETE ON public.ei_content_relations,
  public.ei_relation_review_audit FROM stage2_reviewer_01;
-- 如果角色通过 group/继承仍取得表写权限，必须由 DBA 撤销对应继承/组授权。
```

**注意**：上述示例只描述授权意图，不能替代 `pg_roles` 组继承、权限持有、DDL 所有权和 superuser 审查。已存在的直接写权限不会因安装迁移而自动全面撤销；受控触发器会拒绝普通角色直接修改审核表，但受信任 DB owner / superuser 仍可特权修改。因此审计不具备防 DBA 篡改、电子签名或第三方不可抵赖性。

### 紧急撤销

```sql
UPDATE dic_review.authorized_reviewers
SET disabled_at=clock_timestamp() WHERE login_name='stage2_reviewer_01';
REVOKE EXECUTE ON FUNCTION dic_review.apply_content_relation_review(
  BIGINT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT) FROM stage2_reviewer_01;
```

停用审核员不撤回其历史合法审批；若该账号发生泄漏，安全管理人员必须另行审查并显式撤回相关边，经重新人工批准才能重新进入 RAG。对于现有 `CONFIRMED` 数据，部署前应验证其历史审计完整性、来源版本和权限来源，必要时隔离。

## 人工审核示例（专用用户名）

从完整源码根目录启动：

```powershell
.\.venv\Scripts\python.exe .\scripts\review_stage2_gui.py

# CLI 先只读预览：抄录双端完整 64 位 sha256
.\.venv\Scripts\python.exe .\scripts\review_engineering_relation.py `
  --database rag_21mm --project 21MM --user stage2_reviewer_01 --candidate-id 123

# 接着带人工确认结果、旧状态、两端 SHA-256、审核原因显式提交
.\.venv\Scripts\python.exe .\scripts\review_engineering_relation.py `
  --database rag_21mm --project 21MM --user stage2_reviewer_01 --candidate-id 123 `
  --expected-status REVIEW_REQUIRED --status CONFIRMED `
  --source-sha256 <SOURCE_64_HEX> --target-sha256 <TARGET_64_HEX> `
  --reason '双端原文与文档实际章节已逐一核对；引用位置明确。' --apply
```

上面的尖括号 SHA-256 是占位符，使用前替换为真实完整十六进制摘要；不可把尖括号原样传入 PowerShell。

GUI 默认只读列表，需先加载一条候选并查看完整原文，明确抄录 ID + 双端 SHA-256 前 12 位，填写充分理由，选择合法的新状态，再经二次确认使用独立 PostgreSQL reviewer LOGIN 提交；数据库复验 SHA-256、来源、项目、版本、Stage 1 receipt 和最后审核事件。两次查询之间可能修改，数据库乐观状态和行锁保证更新失败时不产生审计成功事件。

## 生产前必须完成（本包不包含真实环境测试结果）

- `psql` 真正安装/回滚迁移 SQL，并以只读账号、应用账号、审核账号进行 CRUD 权限负例测试；验证私有登录的授权/撤销与 DB owner 例外。
- 真实数据核验 508 → 550 蓝牙音频正文（本体 `Bluetooth Audio function spec v2.52.pdf`，不能把 v2.50 Coversheet 当本体）；必须有实际 PDF 原文、SQL 字符偏移、章节/页码的一致性人工证据。
- 注入式 SQL 权限负例：未经授权账号尝试调审核函数/直接 UPDATE；同时修改关系、切换当前版本、回滚/重试、异常中断，审计只 append，状态不半提交。
- 真 PostgreSQL 17 Unicode/NFKC 特殊空白归一化与 Python parity（某些 Unicode 空白语义可能不同，数据库只会拒绝，不得为绕过而关闭门禁）；Windows GUI 权限边界、内网部署与运行耗时。
- 数据库管理员/表所有者是信任根，HASH/审计表不是外部数字签名；要求不可抵赖时需外置 WORM 或签名、可信时间戳。

本版本的本地回归均为离线模拟；不得宣称生产级权限、Windows 或实际 PostgreSQL 审核已通过。
