---
name: rag-quality-gate
description: 执行带来源标记的回答发布门禁测试集合，输出合成/人工案例分别计数，不授予生产认证。
---

# rag-quality-gate

## 适用阶段
Stage 7。用户提问需要此类工程能力时使用。必须在内部受控环境执行，不向外网传输项目原文。

## 工作流程
1. 识别项目、式样版本、输入来源与明确的目标；缺少必要身份时停止，不自动猜测。
2. 调用项目受控工具 `python scripts/run_skill.py rag-quality-gate --input <local-json>`；不要执行模型自造的 SQL、Python 或文件路径。
3. 检查返回的 `status`、`limitations`、`evidence` 与 `production_acceptance`。如有错误/版本不明，输出证据不足。
4. 输出工程、文档、版本、章节、页码、双端原文与审核状态（如果输入实际包含这些已认证信息）。

## 输入契约
JSON 对象包含 `cases`，详见 `docs/ENGINEERING_v1.49_SKILLS_CN.md`。

## 证据与安全约束
- 只允许 INTERNAL_ONLY 内部处理；不读公网，不写 PostgreSQL/Qdrant，不自动审批。
- 对技术正文严格使用 SPEC_BODY，封面/审批/目录不得充当技术证据。
- 独立 JSON 输入属于操作员提供的离线快照，不代表真实数据库身份已验证。
- 关系候选、相似度、RRF 排名、字面参数锚点均不等于语义结论证明。
- 本技能最高只能返回 `OFFLINE_EVALUATION_ONLY`，不能声称企业生产验收通过。

## 回答输出
保留 `status`、`evidence_level`、`findings`、`limitations`、`production_acceptance`，失败时解释阻断原因而非补编事实。

## 分类任务
- 使用明确标记 SYNTHETIC_CONTRACT 或 HUMAN_REVIEWED_REAL 的案例表。
- 输出测试数、符合预期数、真实案例数；未经独立审查的来源 SHA/Reviewer 字段不能当客户 Golden 认证。
- 不能通过该技能自动执行生产升级、审批或发布。

## 阻断
- 未标记数据集、案例重名、缺人工审查字段、空样本、无效引用来源。
