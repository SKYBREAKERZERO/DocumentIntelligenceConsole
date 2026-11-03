---
name: spec-version-impact
description: 对同一式样的新旧 SPEC_BODY 进行来源保留的要求映射，变更相关判断必须人工复核。
---

# spec-version-impact

## 适用阶段
Stage 3。用户提问需要此类工程能力时使用。必须在内部受控环境执行，不向外网传输项目原文。

## 工作流程
1. 识别项目、式样版本、输入来源与明确的目标；缺少必要身份时停止，不自动猜测。
2. 调用项目受控工具 `python scripts/run_skill.py spec-version-impact --input <local-json>`；不要执行模型自造的 SQL、Python 或文件路径。
3. 检查返回的 `status`、`limitations`、`evidence` 与 `production_acceptance`。如有错误/版本不明，输出证据不足。
4. 输出工程、文档、版本、章节、页码、双端原文与审核状态（如果输入实际包含这些已认证信息）。

## 输入契约
JSON 对象包含 `old,new`，详见 `docs/ENGINEERING_v1.49_SKILLS_CN.md`。

## 证据与安全约束
- 只允许 INTERNAL_ONLY 内部处理；不读公网，不写 PostgreSQL/Qdrant，不自动审批。
- 对技术正文严格使用 SPEC_BODY，封面/审批/目录不得充当技术证据。
- 独立 JSON 输入属于操作员提供的离线快照，不代表真实数据库身份已验证。
- 关系候选、相似度、RRF 排名、字面参数锚点均不等于语义结论证明。
- 本技能最高只能返回 `VERSION_TEXT_MAPPING_ONLY`，不能声称企业生产验收通过。

## 回答输出
保留 `status`、`evidence_level`、`findings`、`limitations`、`production_acceptance`，失败时解释阻断原因而非补编事实。

## 分类任务
- 仅比较同一项目与式样编码的两份 SPEC_BODY 快照；保存双端的版本 ID 和原文范围。
- 优先使用相同文本/章节的确定性映射；其他对齐标记 REVIEW_REQUIRED。
- 不从文本变化直接推导旧关系失效或 BUG 影响；需要真实数据库版本生效时间和独立人工审查。

## 阻断
- 跨工程、跨式样、同一版本或缺少两端版本身份。
