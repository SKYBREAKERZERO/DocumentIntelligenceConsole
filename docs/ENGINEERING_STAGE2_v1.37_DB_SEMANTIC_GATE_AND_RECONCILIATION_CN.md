# Stage 2 v1.37 — PostgreSQL 服务端引用门禁与提交对账

## 技术范围与信任边界

v1.37 把 `REFERENCES_SECTION` 的**明确规格编号 + 目标章节**语义复核移到数据库写入触发器。旧版本的 Python/GUI 检查仍保留，且 v1.37 客户端在执行审核函数前核验已安装的两个 PostgreSQL 函数的源码 SHA-256。**数据库端的写入门禁只有 DBA 实际执行增量迁移并完成真实数据库复测后才生效。** ZIP 含 SQL，不会自动连接、更改或迁移 PostgreSQL。

服务器允许的仅是保守、有限句法的章节引用（支持 `Refer to Spec 550 section 4.1`、`See Spec 550, Sec. 4.1`、`参照 Spec 550 section 4.1` 的受限形式）。对跨句、多个章节、多个明确 SPEC 或目标章节含多个 Requirement Unit，拒绝将候选审核为 `CONFIRMED`。语义解析拒绝绝不意味着工程关系不存在，应保持 `REVIEW_REQUIRED` 或升级人工核验。**被审核的引用边仍不等于已证明的依赖、冲突、接口约束或版本影响。**

保留 v1.33 既有的登录身份/审核人注册、`SECURITY DEFINER`、版本锁与双端 `SPEC_BODY` 原文、源偏移、SHA-256、backfill receipt、当前版本、同工程身份与审计原子写入约束。新门禁覆盖已安装触发器上的 `CONFIRMED` 状态变更，不能保护被超级用户/表所有者强制停用的触发器。DBA 管理员仍为可信根。

## DBA 部署（每个工程库独立，禁止在 rag_qabug 或未授权环境执行）

1. 对数据库做一致性备份及恢复演练，冻结其他 schema 变更；确认 PostgreSQL **17**、v7 数据表、v1.33 的受控函数/两组触发器、单工程 `project_info`、`dic_review` 函数持有人为受信任 NOLOGIN 角色。
2. DBA 审核 SQL 文件 `database/upgrade_engineering_stage2_v1_37.sql`。该事务要求当前数据库角色就是现有受控函数所有者，且它是非超级、非登录的受限角色；可由受信任超级 DBA *临时* `SET ROLE` 部署。不要给应用角色或普通审核员临时提权。
3. 用 `psql -X -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U <DBA> -d rag_21mm` 登录，审查当前 role 并由 DBA 在受控会话里执行 `SET ROLE <trusted_no_login_owner>;`，使用 `\i '.../database/upgrade_engineering_stage2_v1_37.sql'`。SQL 自带事务、锁/执行超时及前置检查，失败回滚。不要一次性在 QA/BUG 与工程库群组批量执行。
4. 在 DBA 受控会话中执行只读 `database/verify_engineering_stage2_v1_37.sql`，它必须对合成正确/错误引用的测试全部返回成功（异常则拒绝继续）。
5. 用只读审计身份运行 `scripts/preflight_stage2_db.py`（参照 v1.35 文档）；只有报告中 `v137_pinned_relation_guard`、`v137_pinned_reference_helper`、`v137_helper_authority` 及全部旧门禁均 PASS，才进入负向验证。**这仍然不代表生产验收。**
6. 使用非生产受控工程库验证：直接调用原本 v1.33 `dic_review.apply_content_relation_review` 时，错误 spec/section、跨句、多节、多目标 Requirement Unit、并发审核及目标版本变化必须在服务器事务中被拒绝；正确的单一章节引用在已授权审核员、完整本体与 receipt 等全部条件满足时才能生成恰好一条审计事件。对异常回滚时不能有状态变化或孤儿审计。
7. 在 Windows GUI 实测加载、审核前水位审计 ID、密码隔离、手工审核和断线对账；对实际 508→550 源 PDF 本体进行人工 Gold 核验，封面/审批表始终不可作技术依据。

### 只读预检示例

```powershell
$env:POSTGRES_PASSWORD = '<通过安全方式注入审核账号凭据，不建议留存历史>'
python .\scripts\preflight_stage2_db.py --database rag_21mm --project 21MM --reviewer stage2_reviewer --app-role app_user --audit-user audit_login
```

不得把上面的占位符当成真实密码。建议使用受控秘钥仓库及进程级环境变量，执行后清理环境。

## 网络超时或 COMMIT 响应丢失时的只读对账

1. **提交前**保存候选 ID、预览 `last_audit_id`（初始为 0）、预期旧状态、新状态、双端完整 SHA-256，及审核理由经 `.strip()` 后的 UTF-8 SHA-256。切勿保存原文和密码在非授权工单中。
2. 断线后**不要直接重试**。同一审核账号重新连接，执行 `scripts/reconcile_stage2_review.py`。脚本使用 `REPEATABLE READ READ ONLY`，对照双端原文、当前状态、最后审计 ID、提交前水位之后至多两条事件、数据库认证人和审核理由哈希。
3. 仅 `MATCHING_COMMIT_OBSERVED` 表示**在当前只读快照中观察到与原操作匹配的一次已提交审计**；`NO_COMMIT_VISIBLE_AT_SNAPSHOT` 仅说明**本次查询快照中未见提交**，不能断言事务已回滚或后来不会提交。`INDETERMINATE_*` 表示人工调查并暂停审批；任意结果都不允许自动重试。
4. 对于已经发生多个状态转换、源版本发生变化、审计缺失/不一致、身份或原因不匹配的情形，不应借由匹配当前状态来推断原操作成功。

```powershell
# 使用同一个 reviewer LOGIN，提供提交前保存的真实字段。以下为占位示意。
python .\scripts\reconcile_stage2_review.py `
  --database rag_21mm --project 21MM --user stage2_reviewer `
  --candidate-id 12 --before-audit-id 7 `
  --expected-status REVIEW_REQUIRED --intended-status CONFIRMED `
  --source-sha256 <SOURCE_64_HEX> --target-sha256 <TARGET_64_HEX> `
  --reason-sha256 <STRIPPED_REASON_UTF8_64_HEX>
```

PowerShell 的 `<...>` 在真实命令中须替换为无尖括号值；可通过 `python -c "import hashlib; print(hashlib.sha256(input('Reason: ').strip().encode('utf-8')).hexdigest())"` 在本地计算理由哈希。

## 回滚与取证

不得仅因回滚应用版本就假设已经撤销 DB 迁移。若需降级，必须由 DBA 在变更窗口明确恢复之前审查过的 v1.33 guard 函数定义，并保留审计，回滚后 v1.37 应用客户端的 DB 指纹门禁必然拒绝审核。**不要**为让旧服务工作而撤销 trigger、删除审计或放宽 `GRANT`。生产恢复方案需事前演练和签字。

## 不能宣称已通过的测试

仓库中的 SQL 结构与客户端对账测试使用模拟游标/静态检查；不具备 PostgreSQL Server 17、真实 IAM/LOGIN 权限、Windows、原始 PDF 的执行证据。不能宣称迁移已部署、并发死锁验证通过、Windows GUI 实测通过或 508/550 真实业务引用已经证明。
