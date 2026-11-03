# v1.47 — 企业回答质量阻断项修复与部署验收

2026-10-23 · Release classification: `DEVELOPMENT / OFFLINE_TESTED / PRODUCTION_NOT_APPROVED`

## 适用范围与已落实措施

- **P0-01：Dense 原文真值**。Dense Qdrant 命中（包括带全文 Payload 的旧向量点）仅提供候选 `content_id` 和相似度。每个命中必须按当前工程数据库 `rag_current_chunks` 与 `documents` 的正文规则回查 SQL，然后核对工程、文档、文件名、式样编号、版本及双侧 `content_hash`。SQL 原文 SHA-256 必须等于 SQL 中保存的 digest 和 Qdrant 索引 digest，缺字段、库连接异常、正文漂移、Coversheet 均拒绝。QA/BUG 也强制 SQL 回取并校验原文哈希。Sparse 再补 SQL 内容摘要及工程归属校验。
- **P0-02：引用发布门禁**。企业域采用先完整收集模型输出、再使用可信上下文组装器直接记录的已接纳证据编号（不从来源文本中解析编号）、校验引用 ID、空引用和无引用事实段落/句子的发布策略。无效文本用固定、无模型自由生成的“证据不足”提示替代。**此校验不证明主张与引用之间的语义蕴涵，也不能用于生产签收。** `DIRECT_CHAT` 普通非企业对话仍可按原方式实时流式输出。
- **P1：页码和原文跨度预算**。SPEC_BODY 等正文证据头记录 PDF 页码（缺失写 `PAGE_UNKNOWN`）、content_id、原始文件和 SHA-256；不再把被裁切的长工程条款当作完整要求发送。条款无法完全装入上下文即跳过；如果检索证据被跳过，用户回答显示“证据范围不完整”。不代表完整的条件、表格脚注、状态和时序覆盖检查已解决。
- **SQL 原文摘要**。SQL 精确正文也核查内容原文 SHA-256 与数据库 content_hash 是否一致，否则剔除。此处不重新解释整个历史表结构及版本生效时序。
- **P2 API**。本地网关请求体最大 1 MiB、最多 50 条、单条最长 16,000 字符、累计 64,000 字符，模型必须属于本地模型列表。未经授权不得请求模型；即使来源内容伪造 `[999] | source=...` 证据头也不会进入可信引用 ID 允许列表。内部异常不再向 HTTP 返回原始 `str(exc)`。

## 部署前必须人工检查

1. 保留原始 v1.46 ZIP 与独立哈希备份，不要直接覆盖现场生产程序。
2. 在测试环境核实所有当前 Dense/Sparse 点的 `content_hash` 是否以完整正文 UTF-8 的 SHA-256 编码，`document_id / project_code / spec_code / version / file_name` 是否齐全、与真实 SQL 一致。**旧点缺字段将被拒绝，可能降低召回；不可放松验证掩盖索引漂移。** 必要时通过受控索引同步重建，而非在问答时任意猜版本。
3. 测试 PostgreSQL 17 只读账号的 `rag_current_chunks` 与 `documents` SELECT；保证查询函数和最小授权策略一致；审计 `SPEC_BODY` 与 Coversheet 混合旧数据。
4. 选取真实 508→550 完整技术本体、真实 QA/BUG、历史软件版本与批准版本映射，执行人工标注的质量 Golden（章节、PDF 页、条件、状态、表格、例外、正反证据、引用正确性）。
5. Windows GUI / API / Qdrant / PostgreSQL / BGE-M3 / CrossEncoder 的现场全链测试，报告 p50/p95、错误/拒答率、内存/CPU、并发与断线情况；进行敏感权限、日志及备份恢复检查。
6. 流式企业回答现在在完整内容验证后才释放，意味着**企业领域的首 token 延迟可能接近完整生成耗时**；需现场测试用户体验和 CPU 超时策略。

## 残余风险（不能标记已解决）

- **主张与原文的语义匹配**：引用 `[1]` 存在，只说明结构上可追踪，不保证这条引用真正证明 4B 生成的所有数值/否定/例外。复杂问答需要结构化 Claim→Requirement 槽位与双人 Golden 审核，仍未完成。
- **表格与脚注覆盖**：未实现全量条件/时序/单位/例外/表格的槽位级完整性认证。被预算排除的证据可能导致更频繁拒答；这是有意的安全折衷。
- **RAG 之外的结构型证据**：历史差异、关系和目录元数据的独立状态真实性仍按原 v1.46 逻辑，尚无真实 PostgreSQL 17 运行证据。
- **生产验收门禁**：没有客户 Golden 成绩、实机延迟/SLA、真实数据库及 Windows 签收；本版本只承诺离线源码修改通过回归，不能宣称“保证企业级”。

## 验收命令（在源码包根目录）

```powershell
python -m pytest -q tests/test_v147_enterprise_answer_quality.py tests/test_v147_request_safety.py
python -m pytest -q
python -m compileall -q app
# 在实际配置 Ruff 的 CI/工作站另执行 ruff check 与 ruff format --check
```

对应项目的发布验证 JSON 与 SHA-256 清单可用于检查二进制一致性；它们不能替代业务正确性测试。
