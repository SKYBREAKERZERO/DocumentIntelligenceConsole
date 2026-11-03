# v1.50 — 历史版本 / QA-BUG / 式样 / 工程关系的跨域 Skill 优化与质量门禁

> 交付性质：**离线代码回归通过的研发升级，不是生产认证**。适用本地 FastAPI + Ollama + PostgreSQL 17 + Qdrant + Open WebUI；系统仍按 INTERNAL_ONLY 约束运行。部署本包不会自动迁移、写入 PostgreSQL/Qdrant，也不会自动批准工程关系。

## 1. 本次实际完成的可运行增强

| 层次 | 实现位置 | 机制 | 边界 |
|---|---|---|---|
| 分类规划 | `app/skills/live_workflows.py` | 按用户原始问题启用 QA/BUG、SPEC、历史、关系领域的固定只读工具集合 | 不信任模型生成 SQL、动态 Skill 代码；不扩展未授权项目库 |
| Agent 调度 | `app/ai/agent.py` | LLM 漏选必要工具时强制加回；超 8 工具时优先保留必需工具；跨域额外加入授权的 QABUG | 不引入 LangGraph；仍是有界顺序执行 |
| 证据覆盖 | `EvidenceGovernor.govern(..., required_facets=...)` | 在 Top-K 中尽可能保留 QA/BUG、SPEC_BODY、VERSION、RELATION 各至少一条**已返回**证据 | 证据类型覆盖不证明版本适用或正确关联 |
| BUG 现象补查 | `retrieval.py` + `issue_symptom_queries` | 第一轮无正文时，从可信 PostgreSQL BUG 的 phenomenon/actual_result/summary 等提取至多 2 个 SQL 词面补查 | 不使用 effect_result；仅查询词，不是 BUG→要求的正式关系 |
| 工程回答输入 | `rag_service.py` | 按整字段预览有权威来源的 BUG 记录，保留软件版本、实际现象、预期/触发条件；过长字段整项省略并标注不完整 | 部分字段省略时不能当成全部 BUG 事实 |
| 发布门禁 | `app/skills/live_release.py` 与 `live_evidence.py` | 未审定软件→式样版本映射与双端结论，禁止直接发布“符合/不符合”结论；仅有结构引用时禁止称已证实依赖 | 关键词规则为安全补充，**不是语义推理证明器** |
| 条件绑定 | `app/ai/claim_anchor.py` | 对部分可明确拆开的 ON/OFF 数值子句检查阈值是否错配 | 含糊条件、嵌套时序与复杂表格仍需人工/Golden |
| 版本编号误识别 | `retrieval.py` | 不从 `BUG-123` 里的 123 猜测规格编号；只保留明确的 SPEC 标识或独立工程编号 | 未校验版本生效期，不能当作版本适用证明 |
| 可重复评测 | `scripts/evaluate_v150_skill_workflows.py` | JSONL 合同回放：Skill 路由、必需工具、覆盖缺失诊断 | 合成数据不能代表真实检索/回答质量 |

## 2. 工程级回答形式与禁止项

**BUG 符合性问答** 至少区分：

1. 问题单的实际现象、触发步骤、期望及软件版本（需要真实 SQL 证据）。
2. 软件版本→生效 SPEC_BODY 版本的**人工批准映射**；不得默认使用当前版。
3. SPEC_BODY 相关要求原文、版本、章节、PDF 页码、完整条件/例外与哈希。
4. 已审核关系与仅用于发现的未审核候选分别呈现。
5. 输出：符合 / 不符合 / 未规定 / 含糊或冲突 / 证据不足。**本版本没有可信的审核结论输入时，只准对实际符合性回答“证据不足”。**

**历史关系问答** 必须分离：“508 引用了 550 某节” → “550 某节发生了正文变更” → “508 原关系是否仍成立（须审核）”。单独的结构引用或正文 diff 不得得出影响结论。

**跨域回答质量** 要求逐类证据说明缺失项；一个模型分数、高位 Top-K、RRF 数值、原文哈希或同一文档名称均不能证明工程结论正确。

## 3. 本地运行与可重复验证（PowerShell）

```powershell
python -m pytest -q tests\test_v150_live_engineering_skills.py
python -m pytest -q
python .\scripts\evaluate_v150_skill_workflows.py `
  --cases .\examples\skills\v150_workflow_cases.jsonl `
  --output .\output\v150_skill_routing_report.json
```

> 评测脚本完全离线、只读取显式指定的 JSONL，不调用数据库/网络，不执行模型或系统命令。命令中的 `output` 必须先存在。

## 4. 开源设计参考（仅借鉴方法，不复制源码）

- **Graphiti**：关系的时间效力、事件来源与更新时间分离。参考它的 temporal edges 思路，但不推导本项目尚未审核的关系版本有效性。https://github.com/getzep/graphiti
- **Kotaemon**：答案引用应能找到实际文档来源与位置。保留 DIC 的可信证据 ID、页码与 PostgreSQL 原文认证。https://github.com/Cinnamon/kotaemon
- **Phoenix**：将追踪/评测/样例回放分开，为真实 Golden Dataset 留接口。当前没有集成 Phoenix 服务，也未复制其源码或引入其许可证义务。https://github.com/Arize-ai/phoenix
- **DeepEval**：使用失败反例和可重复基准作为修复门禁；本轮实现 Python 原生合同回放，不引入依赖。https://github.com/confident-ai/deepeval

## 5. 为什么仍是 NO-GO

本次没有接通客户 PostgreSQL 17 / Qdrant / 本地 4B 或 8B 模型 / Windows GUI。没有真实 21MM/24MM/508→550/QA-BUG Golden 样本、审核后的软件版本映射、跨版本依赖有效性审核、p50/p95 延迟/CPU 内存及并发验收。

建议后续最先执行的现场门禁：真实 BUG 软件版本→SPEC_BODY 历史版本审核表核验；逐问题覆盖率与正确页码引用；结构关系和已确认关系的严格分离；故障降级；专家标注 5 分类的误判率和证据引用准确率。**通过单元测试 != 通过这些门禁。**
