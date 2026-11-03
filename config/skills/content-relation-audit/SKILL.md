---
name: content-relation-audit
description: 核对跨式样章节引用的双端原文跨度，不得将结构引用视为已批准工程依赖。
---

# content-relation-audit

## 适用阶段
Stage 2。用户提问需要此类工程能力时使用。必须在内部受控环境执行，不向外网传输项目原文。

## 工作流程
1. 识别项目、式样版本、输入来源与明确的目标；缺少必要身份时停止，不自动猜测。
2. 调用项目受控工具 `python scripts/run_skill.py content-relation-audit --input <local-json>`；不要执行模型自造的 SQL、Python 或文件路径。
3. 检查返回的 `status`、`limitations`、`evidence` 与 `production_acceptance`。如有错误/版本不明，输出证据不足。
4. 输出工程、文档、版本、章节、页码、双端原文与审核状态（如果输入实际包含这些已认证信息）。

## 输入契约
JSON 对象包含 `source,target`，详见 `docs/ENGINEERING_v1.49_SKILLS_CN.md`。

## 证据与安全约束
- 只允许 INTERNAL_ONLY 内部处理；不读公网，不写 PostgreSQL/Qdrant，不自动审批。
- 对技术正文严格使用 SPEC_BODY，封面/审批/目录不得充当技术证据。
- 独立 JSON 输入属于操作员提供的离线快照，不代表真实数据库身份已验证。
- 关系候选、相似度、RRF 排名、字面参数锚点均不等于语义结论证明。
- 本技能最高只能返回 `STRUCTURAL_REFERENCE_ONLY`，不能声称企业生产验收通过。

## 回答输出
保留 `status`、`evidence_level`、`findings`、`limitations`、`production_acceptance`，失败时解释阻断原因而非补编事实。

## 分类任务
- 同一工程内读取来源 508 与目标 550 的双端完整原文及哈希。
- 验证来源确实在同一子句中引用目标 SPEC 和 section；目标段落属于指定章节。
- 只输出 STRUCTURAL_REFERENCE，不得从章节引用自动生成要求级依赖、兼容性或因果关系。

## 阻断
- 一端是封面/审批表、跨度不匹配、跨项目、章节歧义、缺少真实目标编号。
