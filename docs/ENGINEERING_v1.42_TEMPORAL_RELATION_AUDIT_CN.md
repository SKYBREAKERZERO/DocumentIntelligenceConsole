# v1.42 Stage 3：历史关系的要求映射核验（只读）

## 新增能力

`scripts/audit_stage3_relation_timeline.py` 接收**现有** Stage 2 已确认关系 ID 和**明确**的新目标正文版本 ID，使用 PostgreSQL `REPEATABLE READ READ ONLY` 快照检查：

1. 关系状态确为 `CONFIRMED`；源与旧目标均属于同一工程数据库项目的 MAIN 分支 **SPEC_BODY**，并与各自版本的真实 `document_id` 一致；源与旧目标原文的 Unicode 偏移、原文内容、规范化 SHA-256 均有效，且均非 `REJECTED`。
2. 新目标版本属于旧目标的同一 specification，仍是 MAIN 分支和 SPEC_BODY，且 `version_seq` 严格向前。
3. 源、旧目标、新目标三个版本的 v7 回填凭据存在且文档 ID、内容块数、最大块 ID 和要求总数与当前库快照一致。**该门禁不重新计算所有源块 SHA-256；若需完整指纹审计，应另外运行 v1.41 的索引完整性工具。**
4. 存在唯一的 `READY` MAIN 旧→新版本比较记录，以及唯一可用的旧要求→新要求 v7 映射。映射必须为 `DETERMINISTIC` 或 `APPROVED`，新要求还需独立验证文档 ID、版本、原文偏移和哈希。
5. 仅在上述条件齐全时给出一个目标侧的**抽取式文本状态**，并保留证据 ID、哈希与错误代码。

## 状态语义

| 结果 | 准确含义 |
|---|---|
| `TARGET_TEXT_CONTINUITY_VERIFIED` | 旧要求与新要求原文及哈希一致；**不等于旧工程关系仍成立** |
| `TARGET_MOVED_REVIEW_REQUIRED` | v7 已审定/确定性映射为 MOVED，正文没有改变；需要复核章节及引用 |
| `TARGET_CHANGED_REVIEW_REQUIRED` | v7 映射为 MODIFIED，新旧原文经过独立核验且确实不同 |
| `TARGET_REMOVED_REVIEW_REQUIRED` | v7 映射表标记 REMOVED，且有三版本回填完整性凭据；**不代表证明引用关系失效** |
| `INSUFFICIENT_EVIDENCE` | 没有充分证明完整、唯一、真实的版本对应；`issues` 返回原因，禁止自动结论 |

**所有状态**都带有 `relation_validity=UNDETERMINED`、`human_revalidation_required=true`。旧来源式样版本是否在目标变化时生效还没有可靠的业务发布时间映射，因此固定 `source_version_alignment=NOT_ESTABLISHED`（建立 READY 对比后）。没有自动继承原关系 `CONFIRMED` 审核，没有生成 `STALE`，也没有更新数据库。

## 使用（PowerShell）

安装项目依赖、准备独立工程项目数据库的受限只读账号，明确识别历史关系 ID 和新版本 ID：

```powershell
$env:POSTGRES_PASSWORD = '<由你的密钥管理器提供>'
python .\scripts\audit_stage3_relation_timeline.py `
  --database rag_21mm --project 21MM `
  --user dic_reader `
  --relation-id 123 --new-target-version-id 456 `
  --output .\temporal-audit-123.json
Remove-Item Env:\POSTGRES_PASSWORD
```

`123`、`456` 是演示 ID，**不是**实际数据库中的 508→550 关系或版本 ID。脚本先核实 `public.project_info` 中项目与所连接数据库一致，采用事务本地 5 秒锁超时、90 秒 SQL 超时；通过后不提交事务，只输出 JSON（若路径存在，不会覆盖）。`INSUFFICIENT_EVIDENCE` 返回码 2；可核验但仍需人工判断的目标文本状态返回 0。

## 发布及实测限制

- 本功能是**运维只读审计 CLI**，尚未接入 Agent 的自动答案或 GUI 审核按钮；需要操作人员提供明确关系 ID 和新版本 ID。普通在线历史差异检索仍沿用 v1.39–v1.41 路径。
- 离线合成与拒绝性单元测试通过，不代表真实 PostgreSQL 17 SQL、角色权限或压力测试已经通过；实际数据库上还需运行 v1.33/v1.37 门禁检查、v1.41 全量索引审计和 v1.42 CLI。
- 本工具不检查全部内容源块 SHA-256，不自动验证软件发行版本到式样生效时间的映射；一个要求原文没变化也可能存在功能环境/其他依赖变化。不得把文本连续当作已确认的工程依赖。
- Stage 4 BGE-M3 Sparse/RRF/Reranker 尚未因本版本而完成，也未创建 Qdrant 索引；无 DDL/数据迁移。
