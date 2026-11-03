# v1.38：BUG 现象 → 历史 SPEC_BODY 要求的离线候选索引

## 能力范围与证据边界

- 本轮交付的是**候选发现**，并非符合性裁决，也没有引入 LangGraph、Sparse、RRF、独立 Reranker 或经人工审定的五分类判断。纯 Python 多语种词面排序可在笔记本离线执行；4B 结构化提取仍是后续可选增强，当前只从真实 BUG 字段按确定性规则提取观察、触发与期望行为。
- 已存在的 `public.qabug_records.affected_version/software_version` 被准确使用；`bug_details.actual_result/phenomenon` 是实际现象，**`effect_result` 属修复确认，绝对不能当实际现象**。
- `QABUG` 为独立权威数据库。新增映射、候选表都放在 QA/BUG 数据库中，工程式样本体保留于各 SPEC 数据库，不跨库建立伪外键，也不复制为“已确认关系”。
- 候选状态只有 `UNREVIEWED/REJECTED/STALE`。无符合/不符合裁决值；代码中保守默认 `证据不足`。分数是候选排序不是合规概率。
- 笔记本实时 AI 推理**不读取候选作为证据**。GUI 独立列出未审核候选，非流式本地 API 在 `rag.candidate_suggestions` 展示，不合并进最终模型上下文、`EvidenceGovernor`、正式引用或证据强度。
- 在线对候选独立重查 PostgreSQL 的历史 `spec_version_id+version_label+document_id+scope+spec_code`，复核 `SPEC_BODY` 角色、原文字符偏移及 SHA-256。任何 BUG/映射或式样漂移拒绝展示，错误绝不降级成最新版本式样。

## DBA 显式部署

**必须首先备份** QA/BUG 库，确认目标 DB 是 `rag_qabug`，使用拥有该库专用维护权限的账号执行：

```powershell
psql -X -v ON_ERROR_STOP=1 -d rag_qabug -f database/upgrade_bug_spec_candidates_v1_38.sql
psql -X -v ON_ERROR_STOP=1 -d rag_qabug -f database/verify_bug_spec_candidates_v1_38.sql
```

脚本内置 `qabug_records` 存在且 `specifications` 不存在的身份门禁；不运行时不会修改数据库。迁移不会自动授予任何运行时写权限。DBA 应针对**各不相同**的角色执行最小 GRANT（示意，替换真实角色名）：

```sql
GRANT SELECT ON public.qabug_software_spec_versions, public.qabug_spec_candidates TO <runtime_reader>;
GRANT SELECT ON public.qabug_software_spec_versions TO <offline_worker>;
GRANT SELECT, INSERT, UPDATE ON public.qabug_spec_candidates TO <offline_worker>;
GRANT USAGE, SELECT ON SEQUENCE public.qabug_spec_candidates_id_seq TO <offline_worker>;
```

这里只写明授权模板，**不要把尖括号占位符直接执行**。审批映射的 INSERT/UPDATE 只能由 DBA/专属授权者完成，不能给在线应用或离线候选 worker。

## 软件版本 → 式样版本映射是强制前置条件

DBA/工程审核人先从真实版本发布记录核对：车型 `model`、市场 `destination`、版本字段类型（`software_version` 或 `affected_version`）、原样软件版本字符串、规格库范围、SPEC 编号、实际历史版本 ID、版本标签和 SPEC_BODY 的 `document_id`。所有维度精确匹配，不使用 `LIKE`、模糊猜测或“当前版”回退。

示例的字段布局如下，**示例值不是业务事实，禁止直接当真实映射导入**：

```sql
INSERT INTO public.qabug_software_spec_versions
(issue_version_field,software_version,model,destination,spec_scope,spec_code,
 spec_version_id,spec_version_label,spec_document_id,status,approved_by,approved_at,approval_reason)
VALUES ('affected_version','<真实软件版本>','<车型>','<市场>','21MM','550',
        <SPEC版本ID>,'<式样版本>','<SPEC_BODY document_id>',
        'APPROVED','<审核人>',now(),'<发布记录与审批依据>');
```

对于 Bluetooth Audio，`Bluetooth Audio function spec v2.52.pdf` 是 550 本体文件，`Bluetooth Audio Function Spec Cover Sheet` v2.50 只是封面/审批资料，**不得**使用封面内容进行候选排序。真实库的 `spec_version_id`、`document_id` 必须现场核对。

## 离线批处理（默认 dry-run）

在隔离终端分别设置专门的连接串环境变量：`DIC_QABUG_DSN`、`DIC_SPEC_DSN_21MM`、`DIC_SPEC_DSN_24MM`、`DIC_SPEC_DSN_COMMON`（只需所用范围）。勿把凭据写入 CLI 参数、日志或提交到 Git。SPEC 连接应该为独立的只读账号。

```powershell
python scripts/build_bug_spec_candidates.py --issue-key AUDIO-123
python scripts/build_bug_spec_candidates.py --issue-key AUDIO-123 --top-n 8 --apply
```

`AUDIO-123` 只是语法示例。dry-run 不会生成任何数据；`--apply` 仅在映射已 `APPROVED`、正确历史 SPEC_BODY 与整段原文校验通过、实际 BUG 观察存在时，在 QABUG 库 upsert 未审核候选，过期未审核候选可标为 `STALE`。被拒绝的候选不会因重建自动恢复为未审核。

在线明确问 `BUG AUDIO-123 是否符合 550 式样` 时，Agent 可只读查候选表，经两库原文实时核验后，在 GUI 候选区展示双端原文、页码、章节、版本、候选得分；**不会自动输出符合性判定**。普通自然语言症状在没有 BUG ID 时不会猜出 BUG 关联；现阶段需要先检索到具体 BUG 并经后续路由完成多步推理。

## 生产验收条件（未通过）

1. 已在真实 QABUG 数据库执行迁移及双角色最小授权，并实测连接拒绝路径。
2. 历史 BUG 的 `affected_version/software_version` 由实际发布记录映射，并复核历史 SPEC_BODY 页面/原文。
3. 逐项试验映射撤销、BUG 修改、源 Requirement 文本变更、Coversheet、跨库权限和多版本同码歧义；输出应为零候选/失败关闭。
4. Windows GUI 测试候选栏，确认候选不会进入 Ollama Prompt；实际 PostgreSQL 17 并发、事务和性能测试。
5. 建立独立人工审核/五类结论流程及 Golden Dataset 后才能宣称工程符合性分析能力。

本版本只通过离线自动化测试；不声称已安装至你的 PostgreSQL、Qdrant 或完成上述任何验收。
