---
name: spec-requirement-extraction
description: 从可信 SPEC_BODY 正文快照逐行抽取可追溯 Requirement Unit；保留偏移、哈希、时序、参数和表格线索。
---

# spec-requirement-extraction

## 适用阶段
Stage 1。用户提问需要此类工程能力时使用。必须在内部受控环境执行，不向外网传输项目原文。

## 工作流程
1. 识别项目、式样版本、输入来源与明确的目标；缺少必要身份时停止，不自动猜测。
2. 调用项目受控工具 `python scripts/run_skill.py spec-requirement-extraction --input <local-json>`；不要执行模型自造的 SQL、Python 或文件路径。
3. 检查返回的 `status`、`limitations`、`evidence` 与 `production_acceptance`。如有错误/版本不明，输出证据不足。
4. 输出工程、文档、版本、章节、页码、双端原文与审核状态（如果输入实际包含这些已认证信息）。

## 输入契约
JSON 对象包含 `document_id,content_id,section_id,text,file_name,document_role`，详见 `docs/ENGINEERING_v1.49_SKILLS_CN.md`。

## 证据与安全约束
- 只允许 INTERNAL_ONLY 内部处理；不读公网，不写 PostgreSQL/Qdrant，不自动审批。
- 对技术正文严格使用 SPEC_BODY，封面/审批/目录不得充当技术证据。
- 独立 JSON 输入属于操作员提供的离线快照，不代表真实数据库身份已验证。
- 关系候选、相似度、RRF 排名、字面参数锚点均不等于语义结论证明。
- 本技能最高只能返回 `REQUIREMENT_SPANS_ONLY`，不能声称企业生产验收通过。

## 回答输出
保留 `status`、`evidence_level`、`findings`、`limitations`、`production_acceptance`，失败时解释阻断原因而非补编事实。

## 分类任务
- 将整段 SPEC_BODY 来源按完整物理行抽取为源句/要求/引用/表格行；保留原文、Unicode 偏移及页码。
- 输出 condition/exception/timing/quantity/identifier 线索，不由模型自行改写阈值或推断表格列标题。
- 表格跨页、脚注、多个前置条件必须留待独立 Stage 1 原文覆盖审计。

## 阻断
- Coversheet、没有明确 SPEC_BODY 角色、超限正文、丢失来源跨度。
