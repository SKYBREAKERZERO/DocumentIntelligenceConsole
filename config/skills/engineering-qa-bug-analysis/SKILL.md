---
name: engineering-qa-bug-analysis
description: 从 BUG 原始观测字段抽取现象、前置条件和期望；可展示已核验原文快照上的候选但不裁决符合性。
---

# engineering-qa-bug-analysis

## 适用阶段
Stage 6。用户提问需要此类工程能力时使用。必须在内部受控环境执行，不向外网传输项目原文。

## 工作流程
起票前检查与明确票号的正文溯源已接入在线固定流程：`PRE_TICKET_CHECK` / `TICKET_SPEC_TRACE`。
在线流程由用户问题触发，检索到的文档文字不能改变工具或分支；不调用模型规划。

明确询问式样通/是否符合时，在线 `CONFORMANCE_CHECK` 使用受控的有限条件检查。仅接受经数据库审核的精确软件版本、车型、地域与式样版本映射；实测值与所有例外条件必须明确。参见 [条件检查部署与示例](../../../docs/CONDITIONAL_CHECKS_AND_SYNC_CN.md)。返回“在已核查条件内符合 / 疑似不符合 / 证据不足”，保留原文、章节、PDF物理页、content_id 和哈希；不认证用户观测真实性或整本式样符合。该能力与下述离线快照工具的候选发现结果分开解释。

- 起票前：先查历史票；存在达到阈值的相似候选则停止式样兜底，输出待核对票据，不能认定同一问题。
- 无强匹配：标记覆盖未核准，再检索明确工程的式样；不能将 Top-K 无匹配解释为不存在历史票。
- 票内引用：只解析明确文件名、版本、章节和可用页码；以 SQL 精确核验历史正文，不自动改用最新版。
- 所有符合性结果保持 `UNDETERMINED`，直至具备已审核适用版本及完整双方证据。
- 在线结构化入口：`POST /api/v1/retrieval`，`operation=testing_check`；普通 Open WebUI 对话同样触发。
- 返回 `workflow.state/steps/coverage/conformance`，便于前端展示流程、异常和中断；不输出内部推理过程。

以下独立快照工具仍用于离线检查：
1. 识别项目、式样版本、输入来源与明确的目标；缺少必要身份时停止，不自动猜测。
2. 调用项目受控工具 `python scripts/run_skill.py engineering-qa-bug-analysis --input <local-json>`；不要执行模型自造的 SQL、Python 或文件路径。
3. 检查返回的 `status`、`limitations`、`evidence` 与 `production_acceptance`。如有错误/版本不明，输出证据不足。
4. 输出工程、文档、版本、章节、页码、双端原文与审核状态（如果输入实际包含这些已认证信息）。

## 输入契约
JSON 对象包含 `bug,units`，详见 `docs/ENGINEERING_v1.49_SKILLS_CN.md`。

## 证据与安全约束
- 只允许 INTERNAL_ONLY 内部处理；不读公网，不写 PostgreSQL/Qdrant，不自动审批。
- 对技术正文严格使用 SPEC_BODY，封面/审批/目录不得充当技术证据。
- 独立 JSON 输入属于操作员提供的离线快照，不代表真实数据库身份已验证。
- 关系候选、相似度、RRF 排名、字面参数锚点均不等于语义结论证明。
- 下述离线快照工具最高只能返回 `CANDIDATE_DISCOVERY_ONLY`；在线有限检查也不能声称企业生产验收通过。

## 回答输出
保留 `status`、`evidence_level`、`findings`、`limitations`、`production_acceptance`，失败时解释阻断原因而非补编事实。

## 分类任务
- 现象只从 BUG phenomenon/actual_result/description/summary 提取，不能把 effect_result 当观察结果。
- 从 repro_steps 取触发步骤，从 expected_result 取期望行为；离线快照按词面相似度列出待审候选。
- 如果没有真实受控“软件版本→式样版本”映射，不进行符合性裁决。最高结论为“证据不足”。

## 阻断
- 不是 BUG、式样候选非 SPEC_BODY、原文偏移或哈希漂移、错版本。
