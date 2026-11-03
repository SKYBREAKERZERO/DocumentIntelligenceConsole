# v1.43 — Stage 3 Agent 历史关系核验与 PG17 只读预检

## 1. 新增查询格式与安全限制

示例（Windows GUI 中的企业工程 Agent 模式）：

```text
21MM relation_id=31 new_target_version_id=303 旧关系在新版本里是否继续成立？
21MM 关系ID 31 新目标版本ID 303 需要复核哪些目标要求？
```

- 两个标记字段必须在**本轮原始提问**中明确且分别只有一个。不能用 `550 v2.52` 猜测数据库自增 ID；也不能用 LLM 产生/补全 ID。版本 ID 是工程 `specification_versions.id`，不是印在封面上的版本字符串。
- 选择唯一项目库（21MM / 24MM / COMMON）。如果搜索范围同时包含多个项目，问题必须指定唯一项目；跨工程库查询、QABUG 库查询直接拒绝。
- 必须使用 `dic_reader` 一类独立受限 PostgreSQL LOGIN，不得使用 `postgres`/`root` 或公共 `rag`/`rag_qabug` 数据库。
- 执行器强制 `REPEATABLE READ READ ONLY`、5s lock timeout 和 30s statement timeout；核对真实数据库名、`project_info`，然后调用 v1.42 原有的双端原文/正文角色/版本、v7 要求映射校验。无 UPDATE/INSERT/DELETE、无自动审核、无数据库迁移。
- 精确审计路径不会把一般语义检索或历史版本猜测拼接为结论，不触发弱证据广泛补查。

## 2. 输出解释

- `TARGET_TEXT_CONTINUITY_VERIFIED`：目标新旧**文本与哈希相同**，但不代表工程依赖、旧版本有效期、QA/BUG 合规或影响判定。
- `TARGET_CHANGED_REVIEW_REQUIRED`、`TARGET_MOVED_REVIEW_REQUIRED`、`TARGET_REMOVED_REVIEW_REQUIRED`：只描述已核验映射的变化，需人工判断关联。
- `INSUFFICIENT_EVIDENCE`：正文、凭据、比较或映射链任一步失败时，不产出积极结论。
- 所有情况下 `relation_validity=UNDETERMINED`、`human_revalidation_required=true`。这不是发布审批，无法替代人工关系重审。v1.42 只核对 source digest 字段与计数存在，不执行 Stage 1 全文指纹扫描；需另用 v1.41 全量索引审计。

## 3. PostgreSQL 17 本机验收（需 DBA/运维显式执行）

先在隔离测试库安装先前的 v7/Stage 2 迁移及真实数据，创建只读 `dic_reader`，运行已有的 v1.33/v1.37 受控审核 DBA 门禁；迁移不由本包自动执行。

```powershell
$env:POSTGRES_PASSWORD = Read-Host 'PG reader password'
python .\scripts\verify_stage3_postgres_runtime.py `
  --database rag_21mm --project 21MM --user dic_reader `
  --output .\stage3-pg-runtime-check.json

python .\scripts\audit_stage3_relation_timeline.py `
  --database rag_21mm --project 21MM --user dic_reader `
  --relation-id 31 --new-target-version-id 303 `
  --output .\stage3-temporal-audit-31-303.json

python .\scripts\audit_stage2_relation_index.py `
  --database rag_21mm --project 21MM --user dic_reader `
  --spec-code 550 --output .\stage2-550-full-index-audit.json
```

输出均要求目标路径不存在，JSON 中不含数据库密码。`verify_stage3_postgres_runtime.py` 在角色可写表或列、表所有者角色继承、管理员角色、非 PG17、对象缺失时返回 `BLOCKED`（退出码 2）；连接失败、SQL 错误以非零失败，不能视作通过。只读权限预检的通过不等于真实负向事务、业务引用完整性、并发以及 Windows 端到端通过。

### 必做现场负向与性能验收

- 禁止 Coversheet 承担 SPEC_BODY 技术证据；测试原文 Unicode 偏移、哈希漂移、歧义要求映射、跨项目版本 ID 和非 MAIN 分支。
- 针对实际 QA/BUG 所在软件版本和式样版本做时间生效关系验证；当前工具没有软件发布基线映射，不得判断 BUG 是否违反新版式样。
- DBA 在测试库验证 `dic_reader` 不能直接修改关系/审核表；执行 v1.33、v1.37 DB 函数/触发器与锁冲突审计；保留原 SQL 审计。不得用离线 Mock 代替真实 PG。
- 在 Windows 主程序中验证 Agent 路由、会话上下文以及受限账号；按真实 508→550 SPEC_BODY 文档做双端人工 Gold，并记录召回质量/延迟。现场验收报告应独立签字。

## 4. 后续阶段

完成现场闭环后再为 Stage 4 建立 Dense/Sparse/RRF/Reranker 的 Golden Dataset 和定量质量基线。v1.43 无 Stage 4 全功能交付或 LangGraph 替换。
