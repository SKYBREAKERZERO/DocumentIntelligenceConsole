# 历史版本说明（不作为当前验收结论）

> 本次核心后台增量版：请先阅读 [START_CORE_BACKEND.md](START_CORE_BACKEND.md)。默认以管理配置、完整入库、SQL/Qdrant 查看、历史版本、工程关系和 Skills 为主；用户聊天通过 Open WebUI。完整验证及限制见 [核心后台说明](docs/CORE_BACKEND_CN.md)。下方保留原版本历史说明。

## 550 Bluetooth PDF chat continuation (v1.54)

The v1.54 chat API distinguishes physical PDF lookup, table of contents and
bounded original-text excerpts from the 21MM PostgreSQL `contents` table.
Short follow-ups inherit an explicitly named versioned PDF from recent **user**
turns. The new `SPEC_CHAPTER_CONTENT` route provides source excerpts, not
an exhaustive interpretive summary. It does **not** approve a 550 identity merge.

# DocumentIntelligenceConsole Enterprise v1.53 CN FULL (historical notes)


**v1.53 安全读取改进**：根据真实数据库审计结果，550 Cover Sheet (`specification_id=2`、`version_id=2`) 与 Bluetooth Audio v2.52 正文 (`specification_id=3`、`version_id=3`、`spec_code=NULL`) 被分开建档；本版不自动合并，而是新增正文身份候选检索和按真实 PDF `document_id` 获取章节，回显 `LIMITED` 证据，继续禁止把 Cover Sheet 当本体。参见 `docs/ENGINEERING_v1.53_SPEC_IDENTITY_SAFE_READ_CN.md`。

**v1.52 现场故障修复**：21MM Bluetooth Audio 的文件已在 `documents` 中发现，而 v1.51 的精确 `SPEC_LOOKUP` 曾返回 PostgreSQL `IndeterminateDatatype`。本版对版本参数显式定型，并提供 `scripts/diagnose_spec_registration_v152.py` 只读关联审计及 UTF-8 BOM 兼容的 `scripts/verify_v152_spec_facts.ps1`。**文件已导入不代表 550 MAIN SPEC_BODY 版本关系已验证**；参见 `docs/ENGINEERING_v1.52_BLUETOOTH_SQL_FIX_CN.md`。
## v1.52 — 式样事实查询 SQL 直答与 CPU 快速路由

- 文件存在性/真实文件名/规格列表/已登记目录/相邻本体版本差异优先由只读 PostgreSQL 直接回答，跳过本地 Ollama。
- SPEC_BODY 优先、CoverSheet 隔离、无差异集时明确证据不足；修复 PostgreSQL 健康检查误报。
- 现场验收：`docs/ENGINEERING_v1.52_SPEC_FACTUAL_QA_CN.md`。

## v1.50 — 历史版本 + QA/BUG + 式样工程关系的分类 Skill 联合质量优化

- **跨域只读调度**：对 BUG/QA 与式样、历史版本和工程关系的联合问题，自动补齐固定、受控的 SQL / Dense / 版本 / 关系工具；不运行模型自写 SQL 或改变工程库权限。
- **多类证据保留**：在 Top-K 里尽可能保留 BUG 现象、SPEC_BODY、历史变更、工程关系各类证据；缺失的一类必须标为证据不充分，不能编造版本/依赖。
- **BUG 现象补查**：第一轮检索不到正文时，用 PostgreSQL 的实际现象/期望建立最多两次受控 SQL 补查；不把 effect_result 当作缺陷现象，不宣称合规结论。
- **回答质量**：增加历史/QA/BUG 交叉问题的结论限制、结构引用与真实依赖的隔离、简单 ON/OFF 数值条件错用的拒绝检查、长 BUG 字段的整字段安全预算。
- 新增 17 项针对性离线测试及 5 例合成工作流覆盖回放。**并未取得真实客户 Golden 准确率、生产 PostgreSQL / Qdrant 或 Windows 现场验收，维持 NO-GO。**
- 工程说明：[v1.50 跨域 Skills 与质量门禁](docs/ENGINEERING_v1.50_CROSS_DOMAIN_SKILL_QUALITY_CN.md)。

## v1.49 — 七类受控工程 Skills 与答案发布门禁集成

- 新增 **7 个分类 Skills**：Stage 1 要求、Stage 2 内容关系、Stage 3 历史版本、Stage 4 混合检索、Stage 6 BUG 分析、Stage 7 证据验证与质量评测。
- 每个 Skill 含 `config/skills/<name>/SKILL.md` 与 `manifest.json`，附不联网、不写库的 Python 离线受控执行器、CLI 和示例。禁止动态加载模型生成的 Python/SQL。
- `rag-evidence-verification` 已接入企业 RAG 的原回答发布门禁；其他 6 类目前为手工离线 CLI，**尚未**自动接入 Open WebUI 或 LangGraph。
- 所有 JSON 离线输入仍属未认证快照；不能证明工程关系、符合性或生产准确率。现场验收状态继续 **NO-GO**。参见 [v1.49 Skills 开发与运行指南](docs/ENGINEERING_v1.49_SKILLS_CN.md)。

## v1.48 — 企业回答质量加固与证据发布门禁

- **拒绝 Qdrant Dense Payload 直接供应技术原文**；所有 Dense/QA 命中先通过可信 PostgreSQL 文档、版本及 SHA-256 校验。Sparse 与 SQL 精确内容也验证数据库内原文摘要。缺失索引身份和旧哈希会导致安全拒答；请使用受控索引重建。
- **企业模型回答先验证后发布**：引用编号必须在实际送入的证据块中存在，未引用的事实段落及无效引用会被阻断；无证据时退回固定“证据不足”。普通聊天流式模式不变。引用结构验证**不能保证语义支持或工程结论正确**。
- **页码和预算**：正文证据标签增加 PDF 页码（不存在显示 `PAGE_UNKNOWN`）、内容 ID 与摘要；长技术条款不再中途裁剪，遗漏时明确提示不能声称已穷尽工程条件。
- **API 安全**：新增请求尺寸和模型名单校验、异常信息屏蔽。参见 [质量修复与现场验收](docs/ENGINEERING_v1.48_ANSWER_QUALITY_REMEDIATION_CN.md)。
- **生产状态仍为 NO-GO**：真实 PostgreSQL/Qdrant、508/550 Golden、Windows UI、CPU 性能和主张语义支持度未验收。

## v1.46 — Stage 4 本地 CrossEncoder、CPU 超时保护与 Golden 延迟评测

- 新增**默认关闭**的本地 CPU CrossEncoder：仅对 SPEC_BODY 且有 PostgreSQL 正文来源核验的 RRF 块重排；结构关系、版本审计、QA/BUG 及未经审核候选不参与。
- 使用单槽位、独立可终止 spawn 子进程执行推理；限制候选数量、输入字符、Batch、Torch CPU 线程和最长推理时间；失败一律保留原始 RRF 排序。子进程每次冷启动，可能增加 CPU 延迟。
- 新增真实运行（若本地已安装模型）的 Golden 离线基准脚本，计算 Recall@K、MRR@K 与 p50/p95 墙钟时间；**现场真实数据和模型推理尚未验收**。参见 [v1.46 工程说明](docs/ENGINEERING_v1.46_LOCAL_RERANKER_CPU_BENCHMARK_CN.md)。


## v1.45 — Stage 4 BGE-M3 原生 Sparse Sidecar 与三路 RRF

- 使用可选 `FlagEmbedding` 的 BGE-M3 原生词元权重，建立 **独立于旧未命名 Dense Collection** 的 Qdrant Sparse 集合；默认仅预览，人工加 `--write` 才向 Sparse 集合写入。原 PostgreSQL、Dense 向量、审核关系均不修改。
- Agent 混合检索支持 SQL + Dense + Sparse 的三路 RRF，Sparse 原文必须按项目/文档/版本/哈希从 PostgreSQL 回取；未安装可选包、未部署 Sparse 集合或未设置 `DIC_STAGE4_SPARSE_ENABLED=1` 时保留 v1.44 双路路径。
- Golden Dataset 增加 Sparse 与三路 RRF 对照评测，以及 `--ranker rrf_three_way` 质量门禁。**未执行真实 PG17/Qdrant/客户 PDF、模型加载、现场 Golden 与延迟验收；本地 Reranker 仍待开发。** 操作说明：[`docs/ENGINEERING_v1.45_BGE_M3_SPARSE_SIDECAR_CN.md`](docs/ENGINEERING_v1.45_BGE_M3_SPARSE_SIDECAR_CN.md)。


## v1.44 — Stage 4 第一阶段：SQL + Dense RRF 融合和离线 Golden 基准

- 将工程式样正文的 PostgreSQL `sql_exact_search` + Qdrant BGE-M3 Dense `semantic_search` 候选改为**有界 RRF 真实排序融合**。同一物理内容块需同时匹配工程、文档、式样版本和内容 ID，沿用 `SPEC_BODY` 本体优先；原文/哈希有冲突时撤销 Dense 的融合票，只保留 SQL 正文。
- `HYBRID_CHUNK` 显示具体来源、各路名次及仅用于排名的 RRF 数值；**RRF 不等于已审工程依赖或模型置信度**。历史时序审核单路径、关系审核门禁和未确认 BUG→SPEC 展示隔离保持不变。
- 新增 `scripts/evaluate_stage4_rrf.py`：只读 JSONL Golden 数据评估 SQL、Dense、RRF 的 Recall@K、Precision@K 与 MRR@K，支持人工设定通过阈值；不自动标注客户语料或修改数据库。详见 [`docs/ENGINEERING_v1.44_STAGE4_RRF_BASELINE_CN.md`](docs/ENGINEERING_v1.44_STAGE4_RRF_BASELINE_CN.md)。
- **范围限制**：本轮还不是 BGE-M3 Sparse + Qdrant Sparse Index + 本地 Reranker 的完整 Stage 4。仍无现场 PG17/Qdrant、Windows GUI、真实客户 PDF 与性能/准确率正式验收；不新增迁移、不自动写库、离线测试不等于生产通过。

## v1.43 — Stage 3 Agent 历史关系只读核验 + PostgreSQL 17 现场预检

- **Agent 在线集成**：支持严格显式的历史关系查询，例如 `21MM relation_id=31 new_target_version_id=303` 或 `21MM 关系ID 31 新目标版本ID 303`。使用原始问题中分别标注的两个正整数 ID；不猜测版本、不采用 LLM 改写中的 ID。跨项目或无唯一项目时拒绝执行，绝不广播查询。
- **只读单路径**：精确核验仅调用 v1.42 `assess_temporal_relation`，对目标工程数据库先核实 `project_info` 身份，在 `REPEATABLE READ READ ONLY` 快照中检查历史关系与新版要求映射；限制锁等待与执行时间，不混入语义召回、不自动补查、不写库。
- **保守工程结论**：结果标为 `TEMPORAL_RELATION_AUDIT` 或证据不足时的 `TEMPORAL_RELATION_UNVERIFIED`。旧文本未变也只能说明文本连续；工程关系 `relation_validity=UNDETERMINED`、需要人工复核。源式样软件版本生效时间和完整源指纹二次验收仍未证明。
- **数据库预检**：新增 `scripts/verify_stage3_postgres_runtime.py`，只读检查 PG17 项目身份、v7/历史差异相关表、敏感表角色写入与列级更新、表所有者成员关系及账号管理权限。预检通过仅代表本次静态快照符合检查，不等于真实业务或生产验收。
- 不增加数据库 schema 迁移，不自动执行 PostgreSQL/Qdrant 写入、不自动确认关系。v1.42 CLI 保留。Windows GUI、真实 PostgreSQL 17、客户 508→550 PDF、Stage 4 混合检索基准仍待现场验收。执行说明见 [`docs/ENGINEERING_v1.43_AGENT_TEMPORAL_RUNTIME_CN.md`](docs/ENGINEERING_v1.43_AGENT_TEMPORAL_RUNTIME_CN.md)。

## v1.42 — Stage 3 已确认关系的跨版本证据核验（只读）

- 新增 `app/engineering_intelligence/relation_temporal_audit.py` 和 `scripts/audit_stage3_relation_timeline.py`：对指定旧 `CONFIRMED` 内容关系与新目标版本执行**受限只读 PostgreSQL 快照**检查，包括双端真实 SPEC_BODY、原文偏移和 SHA-256、版本顺序、完整回填凭据、`READY` 旧→新比较以及唯一 v7 要求映射。
- 明确返回目标要求文本**不变、移动、修改、删除或证据不足**。无论哪类结果，均保持 `relation_validity=UNDETERMINED`；不自动确认旧关系仍有效、失效或产生变更影响。源式样版本的实际生效时间关联仍未确认。
- 无新数据库迁移、无自动修改关系状态、无生产认证。工具需要操作人员提供关系 ID 与目标版本 ID；当前是**CLI 运维审计**，未接入 GUI/Agent 自动检索。参见 [`docs/ENGINEERING_v1.42_TEMPORAL_RELATION_AUDIT_CN.md`](docs/ENGINEERING_v1.42_TEMPORAL_RELATION_AUDIT_CN.md)。
- 真实 PostgreSQL 17、Windows GUI、客户 508→550 原文、Stage 4 混合检索性能测试仍待实机验收。

## v1.41 — Stage 2 SPEC_BODY 选版与关系索引完整性审计（只读）

- `STRUCTURAL_REFERENCE`、`STRUCTURAL_LAYOUT` 不再直接依赖被历史 Coversheet 污染的 `current_version_id`：先通过统一正文角色策略选择 MAIN 的首选 `SPEC_BODY` 版本；如果首选正文尚未建立 Requirement Unit，不偷用旧版索引。来源/目标都仍验证原文、偏移和 SHA-256，拒绝使用 `REJECTED` 来源或目标。
- `ADJACENT_INDEXED_UNITS` 现在由 PostgreSQL 子查询在**未做章节筛选的所有已索引单元**中计算下一 ID，避免被过滤的其他章节或拒绝单元造成虚假相邻。证据仍只表示索引结构，不证明工程依赖。
- 增加 `scripts/audit_stage2_relation_index.py`：在受限数据库账号的 `REPEATABLE READ READ ONLY` 快照内核查工程数据库身份、首选本体、v7 完整回填凭据、**含零要求块的所有 content 源指纹**、每条 Requirement Unit 的跨度/哈希及总数。缺凭据、漂移、冲突和资源截断返回 `BLOCKED`；通过仅表示该版本索引快照通过完整性检查，不证明语义关系召回完整或生产认证。
- 章节历史变更所触发的 `REFERENCE_CHANGE_REVALIDATION` 显式标注旧关系有效性及源版本时序一致性**未判定**。无自动批准、无历史因果结论、无 schema 迁移。部署与执行见 [`docs/ENGINEERING_v1.41_RELATION_INDEX_INTEGRITY_CN.md`](docs/ENGINEERING_v1.41_RELATION_INDEX_INTEGRITY_CN.md)。
- 当前没有连接真实 PostgreSQL 17、Windows 实机或客户 508→550 PDF，且尚无真实大库性能验收；通过的是离线合成回归。

## v1.40 — Stage 2 正文结构关系 + Stage 3 引用章节历史复核（只读）

- `relation_search` 新增 `STRUCTURAL_LAYOUT`：查同章节、直接父子章节及同一内容块内相邻已抽取单元。每个结构事实都检查正文文档角色、同一版本、双端原文与 SHA-256；**不代表工程功能依赖**，不自动审核或写库。
- 没有明确 SPEC 编号的问句，可利用已检索到的 `PRIMARY_CONTENT` 本体式样编码作为有限的搜索假设，再做 SQL 双端核验；否则沿用正文规格名称识别。无候选则不宽泛扫库。
- 已校验的明确章节引用，可与 `READY` 历史章节差异按目标 SPEC、版本、文档、章节联查，返回 `REFERENCE_CHANGE_REVALIDATION` 人工复核提示，**不判定旧关系失效**。
- 纯只读，零数据库迁移；对每 SPEC 设有范围与条数上限，较大式样需要更精确章节。**尚无真实 PostgreSQL 17、Windows GUI、508→550 PDF 及性能验收。** 详见 [v1.40 工程说明](docs/ENGINEERING_v1.40_LAYOUT_DISCOVERY_AND_CHANGE_REVALIDATION_CN.md)。

## v1.39 — Stage 2 确定性引用证据 + Stage 3 章节级历史差异（只读）

- `relation_search` 新增 `STRUCTURAL_REFERENCE`：不依赖 v1.33 审核函数，只读取已存在的 v7 Requirement Unit 与当前正文 `SPEC_BODY`；对**双端原文、Unicode 偏移、SHA-256、版本、明确 Spec+section 同子句引用锚点**逐项核验。多条目标要求仅按**章节级**呈现，绝不推断某条要求与另一条要求存在依赖。
- `version_search` 新增 `CHAPTER_VERSION_DIFF`：从现有 `READY` 的 `specification_change_sets` / `specification_changes` 读取明确指定章节、关键词及版本对的旧/新正文，验证已有快照 SHA-256，附章节/页码/版本；**无记录不意味着无变更**。
- 两条路径仅查询，不创建、批准、更新任何关系，不重建 Qdrant；已有人工 `CONFIRMED` 关系门禁保持不变。无新增迁移。`v1.38` BUG→式样候选保持展示隔离。
- 参考 [`docs/ENGINEERING_v1.39_STRUCTURAL_REFERENCES_AND_CHAPTER_DIFF_CN.md`](docs/ENGINEERING_v1.39_STRUCTURAL_REFERENCES_AND_CHAPTER_DIFF_CN.md)。真实 PostgreSQL/Windows/508→550 PDF 尚未验收，`Stage 4` 的 Sparse/RRF/Reranker 和 `Stage 5` LangGraph 尚未开发完成。

## v1.38 — BUG 现象到历史 SPEC_BODY 工程要求的候选索引（离线构建、在线只读）

- 新增仅适用于 **QABUG 独立数据库** 的手动迁移 `database/upgrade_bug_spec_candidates_v1_38.sql`：经过审批的精确软件→式样版本映射、候选快照/分数/状态表；不自动 GRANT、不迁移真实数据或审批任何关系。
- `scripts/build_bug_spec_candidates.py` 默认 dry-run；无需实时 4B 推理，从 `phenomenon/actual_result/repro_steps/expected_result` 建立观测结构，对历史要求进行保守的词面排序并保存双端哈希。`effect_result` 不会误当实际现象。
- 显式跨 BUG/SPEC 提问时以 BUG 编号只读查候选，经 QABUG/SPEC 双库的版本、SPEC_BODY、本体原文/Unicode 偏移、SHA-256 二次核验后在 GUI 和非流式本地 API 中**独立展示**。未审核候选**不进入 Ollama 模型上下文、不提升证据强度**；符合性裁决一律标 `证据不足`。
- **尚无实时跨域症状自动规划、4B JSON 结构化增强、Sparse/RRF/Reranker、人工合规审定；未连接真实 PostgreSQL 或 Windows GUI。** 详见 [`docs/ENGINEERING_v1.38_BUG_SPEC_OFFLINE_CANDIDATES_CN.md`](docs/ENGINEERING_v1.38_BUG_SPEC_OFFLINE_CANDIDATES_CN.md)。

## v1.37 — Stage 2 PostgreSQL 服务端引用语义门禁与只读提交对账

- 新增需 DBA 显式执行的 PostgreSQL 17 增量迁移 `database/upgrade_engineering_stage2_v1_37.sql`：对 `REFERENCES_SECTION` 转为 `CONFIRMED` 的数据库写入执行受限同子句 SPEC+section 锚点核验与目标 requirement 唯一性检查；不更改已有关系状态或权限。
- 应用审核路径增加 v1.37 SQL 两个函数源码哈希门禁，旧 SQL 未升级时拒绝写入；只读权限预检已升级到 v3。
- 审核预览输出最后审计 ID，新增 `scripts/reconcile_stage2_review.py` 对提交响应丢失进行只读核验；任何结果均不允许自动重试。
- **未连接真实 PostgreSQL，未执行数据库迁移、并发压力/负向权限实测、Windows GUI 或客户 PDF Gold 验收。** 详细操作见 [`docs/ENGINEERING_STAGE2_v1.37_DB_SEMANTIC_GATE_AND_RECONCILIATION_CN.md`](docs/ENGINEERING_STAGE2_v1.37_DB_SEMANTIC_GATE_AND_RECONCILIATION_CN.md)。

## v1.36 — Stage 2 关系引用锚点精确核验与 RAG 解释边界

- 508→550 类跨规格 `REFERENCES_SECTION` 关系改为**同句/同子句、限定距离、单一明确章节锚点**；跨句引用和多个不同章节候选不能凭关键词/相似度误接。目标章节包含多条要求时，候选生成器不得随意选择其中一条。统一生成器与 PostgreSQL 回填的锚点解析逻辑。
- 人工审核的只读预览再次核验完整双端来源后，额外复验原文内引用锚点与实际目标式样书编号/章节一致；GUI 明示原始引用片段、Unicode 偏移和“引用不代表已证明的因果/功能依赖”。
- 本地 RAG 对已确认但锚点不一致的章节引用关系**拒绝出证据**，对保留的关系增加引用定位与解释上限；静态图谱记录原文锚点，离线验证额外重算锚点，仍不得当成独立签名/实时授权。
- 修复 GUI 在网络中断等不确定提交结果下误称“已回滚、未提交批准”的提示：必须重新连接只读核对最后审核审计后再决定下一步，禁止盲目重试。
- 保留 v1.33 `SECURITY DEFINER` + 审计的原有数据库函数/API/迁移与 v1.35 角色预检，不自动审批或修改数据库。**本轮不是 DB 控制函数内部的锚点约束升级**；DB 直接调用者仍需 DBA 业务规则审查。真实 PostgreSQL 17、Windows 实机和真实原始 PDF 的 Stage 2 人工 Gold 尚未验收。
- 参见 [`docs/ENGINEERING_STAGE2_v1.36_REFERENCE_ANCHOR_CN.md`](docs/ENGINEERING_STAGE2_v1.36_REFERENCE_ANCHOR_CN.md)。

## v1.35 — Stage 2 全库 LOGIN 授权扫描与有界数据库等待

- 增强 `scripts/preflight_stage2_db.py`：在相同 `REPEATABLE READ READ ONLY` 快照下，扫描目标工程数据库中全部具有 CONNECT 权限的非超级用户 LOGIN（最多 500 个；超过即 BLOCKED），而不只核对显式传入的 reviewer / `--app-role`。对每个登录角色核对审核函数有效 EXECUTE + DBA 有效登记、受控函数所有者的间接 MEMBER/SET ROLE 风险、关系表及审计表的表级/列级敏感操作、TRIGGER/TRUNCATE 和两个 schema 的 CREATE。
- 增补两条 guard 函数必须保持 `SECURITY INVOKER` 和固定 `search_path`、受控函数所有者无 CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS、被指定的 reviewer 必须出现在全库有效 LOGIN 扫描中。任何漂移均 `BLOCKED`，不修改 GRANT 或库表。
- Stage 2 GUI 人工审核连接增加事务局部 5 秒锁等待和 60 秒查询超时；DB 预检为 5 秒锁等待和 90 秒查询超时。失败不重试不自动通过，批准仍必须在原有 PostgreSQL 受控事务中写入审计。
- 预检 JSON schema 提升为 `dic-stage2-readonly-preflight-v2`，保留 `result`、`checks` 和 `production_acceptance=NOT_VERIFIED` 字段。新增 26 个合成拒绝性测试，覆盖未列举账号的继承提权、列级 UPDATE、TRIGGER/TRUNCATE、未登记 EXECUTE 和角色扫描截断。
- 未连接生产 PostgreSQL 17、Windows GUI 或真实 508→550 PDF，也未作任何 schema 迁移/索引重建或企业验收认证。参见 [`docs/ENGINEERING_STAGE2_v1.35_GLOBAL_ROLE_AUDIT_CN.md`](docs/ENGINEERING_STAGE2_v1.35_GLOBAL_ROLE_AUDIT_CN.md)。

## v1.34 — Stage 2 主 GUI 入口与只读数据库授权预检

- 在主 GUI PostgreSQL 配置页面增加 Stage 2 独立审核窗口入口；新窗口采用专用 reviewer LOGIN 和人工二次确认，不复用主程序密码或后台令牌。
- 彻底停用 v1.31 旧式自报 actor 的客户端直写审核函数（保留符号但调用即拒绝），只允许 v1.33 起的 PostgreSQL 受控审核路径。
- 新增 `scripts/preflight_stage2_db.py`：在 DBA 明确指定的工程库中用 `REPEATABLE READ READ ONLY` 检查项目身份、数据库角色、函数 SECURITY DEFINER/固定 search_path、非登录函数所有者、触发器绑定及全操作保护、PUBLIC EXECUTE、审核员登记、继承后有效 DML/CREATE 权限及应用账号的受控函数访问。**任一项失败直接返回退出码 2**，不将离线测试冒充生产验收。
- 操作指南见 [`docs/ENGINEERING_STAGE2_v1.34_PERMISSION_PREFLIGHT_CN.md`](docs/ENGINEERING_STAGE2_v1.34_PERMISSION_PREFLIGHT_CN.md)。本轮未执行现场 PostgreSQL 迁移、Windows GUI、真实 508→550 PDF 验收，未自动更改 GRANT。
- v1.34 是 v1.33 源码直接增量；不得覆盖本地不同谱系的 v2.4.22；GitHub main 不受影响。

## v1.33 — Stage 2 数据库受控审核和本地 GUI

- 新增独立 **Stage 2 双端证据审核 GUI**：只读查看 PostgreSQL 候选队列、双端完整原文与精确来源，人工输入候选 ID 与两端 SHA-256 前缀、填写审核理由、选择合法状态并二次确认。
- 新增 **PostgreSQL 17 受控审核迁移 SQL** `database/upgrade_engineering_stage2_v1_33.sql`，用于 DBA 显式安装；`SECURITY DEFINER` 函数以真实 `session_user` 绑定审核身份、检查 allowlist、版本/项目/两端原文/哈希/Stage 1 receipt/审计链，以数据库行锁提交状态与审计事件。没有自动安装、自动授权或数据库写入。
- `scripts/review_engineering_relation.py` 的 `--apply` 不再接受自报审核人；需要专用 reviewer 登录、预览后获得的两端 **完整** SHA-256、预期状态和审核理由。通过数据库函数完成写入，不要求审核员拥有表级 UPDATE 权限。
- 新增 `identity_provenance`：历史 `actor` 自报事件标记为 `UNVERIFIED_LEGACY`，只有新数据库认证事件 `DB_SESSION_USER`、可信审核状态及启用的触发器才允许关系进入 RAG；旧确认边需重新审核，且不会自动改写旧审计。
- 对显式未知 `document_role` **拒绝作为本体证据**；空角色仍仅在可信版本文档归属和实际来源复验后使用 legacy fallback。Coversheet 不能成为技术关系的双端证据。
- 正式生产使用前请阅读 [`docs/ENGINEERING_STAGE2_v1.33_CONTROLLED_REVIEW_CN.md`](docs/ENGINEERING_STAGE2_v1.33_CONTROLLED_REVIEW_CN.md)，先进行真实 PostgreSQL 17/Windows/权限/508→550 PDF 现场验收；离线测试不等于现场通过。
- 本源码包基于 **v1.32** 递增，不能直接覆盖另一代码谱系的 v2.4.22；不修改已有 GitHub `main`。

## v1.32 — Stage 2 内容关系图（离线只读审核工作台）

- 新增 `app/engineering_intelligence/content_relation_graph_report.py` 和 `scripts/export_content_relation_graph.py`：从 PostgreSQL **REPEATABLE READ READ ONLY** 事务，按项目与审核状态抽取内容级双端关系并再次验证完整原文、来源片段偏移、SHA-256、当前版本、本体文件与 Stage 1 完整回填凭据。
- 交付自包含的 `index.html`（**内容单元拓扑 SVG + 可展开的双端全文证据**）、`graph.json` 与 `manifest.json`；拓扑线可进入具体候选。无 JavaScript、外部 CDN 或网络请求。未通过校验的候选只在隔离表记录 ID、状态与原因，**不导出其未经核验的原文**。
- `--verify` 支持离线验证文件哈希、内容单元 SHA-256、跨度长度和 HTML/JSON 一致性；**这些哈希不是数字签名，不能验证审核人身份或防范整个报告集被替换**。报告是历史快照，不等于当前 RAG 的实时权限许可。
- 默认只导出 `REVIEW_REQUIRED`；可明确多次传入 `--status` 包括 `CONFIRMED`、`REJECTED`、`STALE`。范围总上限 200 候选；输出必须是新目录，禁止覆盖旧审核报告。**未添加自动审批、数据库修改、PG 迁移或 Qdrant 重建**。
- Windows 操作示例、安全边界和真正生产验收要求见 [`docs/ENGINEERING_STAGE2_v1.32_CONTENT_GRAPH_EXPORT_CN.md`](docs/ENGINEERING_STAGE2_v1.32_CONTENT_GRAPH_EXPORT_CN.md)。本轮测试为离线模拟；未连接真实 PostgreSQL 或使用生产 PDF，不宣称已通过现场验收。

## v1.31 — Stage 2 内容级关系审核与安全检索

本版本以 GitHub `main` 的 **v1.30 CN FULL** 为基线。`v2.4.22` 不是本 ZIP 的代码基线，禁止将本版直接覆盖该目录。

- 新增 **Content Relation Graph 双端证据审核**：读取原始 `contents` 和 `ei_requirement_units`，逐端验证原文、Unicode 偏移、SHA-256、`document_id`、规格版本和项目归属。只认可当前主版本的规格正文；`Coversheet`、审批表、翻译/排除索引文档不得获得技术关系审批。
- 新审核接口 `app/engineering_intelligence/content_relation_review.py`：候选状态使用显式预期值、事务内行锁和受限状态机；确认/拒绝/撤回均要求理由，每次写入关系状态和审核审计事件。**不会自动批准候选关系**。审核人名称目前是操作者声明，尚非可信 SSO 身份。
- `scripts/review_engineering_relation.py` 支持 `--list` 只读队列和 `--candidate-id` 全文原证据预览。仅在显式 `--apply` 且提供 `--expected-status`、`--actor`、`--reason` 时写入；执行时校验项目数据库身份。
- AI 内容关系检索仅纳入 **CONFIRMED 且最新审计记录匹配** 的当前主版本双端正文关系，同时复验两端源段偏移和 SHA-256；`REVIEW_REQUIRED`/`REJECTED`/`STALE` 不再混入关系证据。取消 21MM/24MM 硬编码，适配已配置的非 QA/BUG 工程项目。
- 不新增数据库 schema，不自动运行迁移、回填、写入 PostgreSQL 或改动 Qdrant。旧 v7/Stage 1 v2 schema 仍须已就绪。保留 OpenAI-compatible API v1.20 协议。

**Windows 操作、风险及验收边界**：[`docs/ENGINEERING_STAGE2_v1.31_CONTENT_RELATION_REVIEW_CN.md`](docs/ENGINEERING_STAGE2_v1.31_CONTENT_RELATION_REVIEW_CN.md)。本版进行了离线模拟数据库的自动化回归测试；**没有连接真实业务 PostgreSQL、没有运行 Windows GUI 实机或执行真实 508→550 人工复核**，不能据此宣布企业生产验收通过。

## v1.30 — 内部知识约束型智能体（Stage 2 前置）

- **先理解，再查内部库**：新增日中英和常见错字联想；不用输入完整 BT/规格/QA-BUG 文档名。自由模型工具不会执行任意 SQL 或访问外网。
- **没有证据就不生成企业事实**：区分 SQL/Qdrant 故障和正常零命中，应用层无证据拒答，不调用 Ollama 补充未证实的业务事实。
- **只读在线诊断**：`scripts/inspect_internal_knowledge_v130.py` 及授权 `GET /api/v1/health/knowledge` 查询本机四个项目知识源状态；不会迁移、导入或删除数据。
- **Open WebUI 重要边界**：必须选择 `document-intelligence-rag`，禁用其他普通 Ollama 模型/外部 Web Search。见 [`docs/ENGINEERING_INTERNAL_AGENT_v1.30_CN.md`](docs/ENGINEERING_INTERNAL_AGENT_v1.30_CN.md)。
- **兼容性**：保留既有 OpenAI API v1.20 契约；新版本是完整 Python 源码，不是重新打包的 EXE。没有执行真实 Windows / PostgreSQL / Qdrant / 业务 PDF 验收。

## v1.29 — Stage 1 依赖 / 测试可复现性 / 实机只读预检

- `requirements.txt` 从 UTF-16 修复为 UTF-8，保留原精确版本（`pywin32` 加 Windows 平台条件），便于 pip 读取；**包版本及 Windows 3.12 安装成功与否仍需在目标机验证**。
- `DatabaseConnection` 在真实连接时才依赖 `psycopg`；`IngestionPipeline` 按格式延迟导入，支持无数据库的离线测试收集。
- 清理旧 `test_database.py` 和 `test_pipeline.py` 的 import-time 连接 / 读取缺失 PDF 行为，改为隔离测试；GUI 集成测试缺依赖时明确 `SKIP`，不是通过。
- 新增 `scripts/stage1_environment_smoke.py`：默认离线，只在 `--check-gui-import` / `--check-db` 明确指定后执行对应检查。数据库 SQL 始终使用只读事务并回滚，错误脱敏，不记录密码。
- Windows Python 3.12 安装脚本 `scripts/install_v129_windows.ps1`、独立验收操作手册 `docs/ENGINEERING_STAGE1_v1.29_WINDOWS_SQL_CN.md`。

**v1.29 不代表 Stage 1 已由企业生产环境签收。** 离线预检和模拟 SQL 测试不能代替真实 Windows GUI 交互、PostgreSQL 数据绑定、人工 Gold 或性能验收。

## v1.28 — Stage 1 发布预检与端到端验收交接

v1.28 新增 `stage1_release_acceptance.py`、`scripts/stage1_release_readiness.py`（及 PowerShell 包装脚本）：检查当前源码包、依赖、内部导入、真实 PDF 文件 SHA-256，并对 v1.25 PDF 对账、v1.26 SQL 来源链和 v1.27 Gold 评分报告做**跨文件身份一致性复核**。任何不完整来源队列和文档版本漂移都不视为完整证据。新增防误报回归测试，并将旧 `tests/test_loader.py` 改为自生成临时双页 PDF，不再在收集阶段读取缺失的 `input/test.pdf`。

**本版只生成离线验收交接报告，输出 `stage1_production_acceptance: NOT_VERIFIED`；不连接真实 PostgreSQL、不运行 Windows GUI、不确认人审身份或日英/状态机语义。** 本机测试通过不等于企业现场验收通过。请参见 [`docs/ENGINEERING_STAGE1_v1.28_RELEASE_ACCEPTANCE_CN.md`](docs/ENGINEERING_STAGE1_v1.28_RELEASE_ACCEPTANCE_CN.md)。

## v1.27 — 人工 Gold 结构化验收（新增）

新增 `stage1_structural_gold.py`、`scripts/evaluate_stage1_structural_gold.py`：在 v1.26 四层来源链与 v1.25 原生 PDF 对账**均完整**的前提下，生成纯待标注清单；由两位独立声明的人工审核角色补齐来源、章节归属和跨页表格真假标签，再计算 Precision / Recall / F1。新增相邻页几何表格配对全集以测量候选召回率（不包含 PDF 漏检表格），没有正例返回未定义而不是 100%。与已有 `stage1-gold-v3` Requirement/Scope holdout 评分组合使用；没有真实人工标注时验收门槛不会自动通过。

详细见 [`docs/ENGINEERING_STAGE1_v1.27_STRUCTURAL_GOLD_CN.md`](docs/ENGINEERING_STAGE1_v1.27_STRUCTURAL_GOLD_CN.md)。**真实 DCM / 550 PDF、独立人工 Gold、Windows GUI 和 PostgreSQL 实机验收依然未完成；本版不自动认证章节归属、跨页表头、日英等价或 DCM 状态迁移。**


**Stage 1 — 原始 PDF → Pipeline JSON → PostgreSQL Content → Requirement Unit 四层来源交叉核验（只读）**

v1.26 新增 `stage1_native_sql_chain.py` 和 `inspect_stage1_native_sql_chain.py`，在**同一条 PostgreSQL `REPEATABLE READ, READ ONLY` 事务**内复核项目/文档/版本归属、原始 PDF SHA-256、来源回填凭据、SQL 快照和需求偏移，再与**当次重新抽取**的原始 PDF 物理页、Pipeline JSON 章节/正文对账结果合并。重复 PDF 原文、页码漂移、章节不在导出树、SQL Content 不唯一、Requirement Unit 偏移不一致或证据预算截断，都不能形成完整候选来源链。

输出不含文档正文、源文件名、真实文档 ID 或凭据；仅包含哈希、页码、定位框、SQL 数字 ID、Requirement Unit key、审核状态和计数。**四层来源匹配仅指字面来源候选，不证明 DCM 状态迁移、条件/动作作用域、表格表头或日英翻译等价。** 不做数据库迁移或 SQL/Qdrant 写入，不自动批准人审结果。详细用法参见 [`docs/ENGINEERING_STAGE1_v1.26_NATIVE_SQL_CHAIN_CN.md`](docs/ENGINEERING_STAGE1_v1.26_NATIVE_SQL_CHAIN_CN.md)。

### Windows PowerShell（已有只读 PostgreSQL 用户）

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.38_CN_FULL'
# 未设置 DIC_STAGE1_RO_PASSWORD 时，CLI 将安全询问口令。
.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report-out 'C:\authorized\DCM.review.json'
.\.venv\Scripts\python.exe .\scripts\inspect_stage1_native_sql_chain.py `
  --pdf 'C:\authorized\DCM-original.pdf' `
  --json 'C:\authorized\DCM.pipeline.json' `
  --review 'C:\authorized\DCM.review.json' `
  --document-index 1 --project 21MM --database rag_21mm `
  --document-id '从数据库查询的精确文档 ID' --version-id 123 `
  --user '已配置的只读用户' --password-env DIC_STAGE1_RO_PASSWORD `
  --report-out 'C:\authorized\DCM.source_chain.json'
```

如果未设置 `DIC_STAGE1_RO_PASSWORD`，CLI 会使用不可回显的交互式口令输入；也可从企业凭据管理器安全注入环境变量。不要把真实密码写进命令行历史记录。`version-id 123` 只是示例，需要换为真实的 PostgreSQL `specification_versions.id`。

**验收边界：** v1.26 新增 16 项生成 PDF / 模拟 PostgreSQL 事务的回归测试。没有接入实际企业数据库与真实 DCM 原始 PDF，Windows GUI、SQL 端到端实机验收与人工 Gold 仍未完成。本版为完整源码包，不是重编译 EXE。

---

## 既有部署与运维说明

### 部署架构与边界

- 管理工具：Windows GUI；启动时异步确保本地 Backend 运行。Settings 可选启用**独立后台看护**，在 Windows 用户登录后监测并恢复后台；关闭 GUI 不影响已运行的看护进程。
- 用户入口：Open WebUI Docker，本机 `127.0.0.1:3000`；以 **Chat Completions** API 连接 `http://host.docker.internal:18080/v1`，模型 ID `document-intelligence-rag`。
- 私有数据：PostgreSQL（21MM / 24MM / COMMON / QA-BUG）作为权威数据，Qdrant 作为派生向量索引；Ollama 和 BGE-M3 全部使用本地服务。不能公开 18080、5432、6333/6334 或 11434。
- **单机韧性不等于真正多节点高可用。** 登录用户退出、Windows 主机故障、断电、磁盘损坏、Docker Desktop/Ollama 停止时，本版本无法确保持续服务或自动切换。
- `ei_*` 要求级知识图谱是手动、显式迁移和回填的增强层，不会在安装、启动 GUI 或聊天时自动修改你的业务库。

## v1.08 改动（保持向后兼容）

1. **聊天并发准入**：默认同时最多 2 个活跃请求，配置 `DIC_MAX_ACTIVE_CHATS=1..8`；超限立即 HTTP 429 + `Retry-After: 3`，避免 Ollama/GPU 资源耗尽。流式完成、失败或断连均会释放准入租约。
2. **独立 Windows 看护**：`app.enterprise.backend_watchdog`；15 秒检查本机认证模型接口；只重启确认为 `STOPPED` 的后台，不杀死/覆盖其他占用端口的进程；10 分钟最多尝试启动 5 次，避免崩溃循环。单实例文件锁；轮换日志和心跳，不记录私有问答或密钥。
3. **GUI 设置**：在“外部 UI / Open WebUI”下添加独立看护开关及状态按钮；Windows HKCU 登录自启动，可反向关闭、不会自动开放局域网，不需管理员权限。看护进程独立于 GUI，在当前 Windows 登录会话内运行。
4. **运行诊断**：新增授权接口 `GET /api/v1/runtime/metrics`（无问题正文/凭据）、`GET /api/v1/health/ready`（区分 BGE 预热与存活，**不等于完整 SQL/Qdrant/Ollama 实时检查**）；`GET /api/v1/health/dependencies` 保留依赖实际探测。
5. **备份与可恢复性**：`scripts/backup_authoritative_sql.ps1` 对权威 PostgreSQL 数据库执行显式 `pg_dump -Fc`，用 `pg_restore --list` 验证并产生 SHA-256 清单。需要单独规划 Qdrant 重建/备份和恢复演练；多数据库备份不是跨库原子快照。
6. **审计工具**：`scripts/check_enterprise_readiness.ps1`（只读）发现本机私有端口意外全网监听，输出活跃请求指标。注意受防火墙/NAT/公司 Wi-Fi 策略影响，端口审计不等于完整渗透测试。

## Windows 安装 / 升级

保留原 v1.05/v1.07 目录和现有 `%LOCALAPPDATA%\DocumentIntelligenceConsole` 用户配置，不要覆盖仍在运行的源码目录；只允许一个版本绑定 18080。

```powershell
cd 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.38_CN_FULL'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

在工具 **Settings → 外部 UI / Open WebUI → 后台进程管理** 中，可以启用“后台独立看护”。启用后重新登录 Windows 仍能在当前用户会话中自动启动看护进程；原有 GUI 登录自启动开关与之独立。诊断与操作脚本见 [`docs/OPERATIONS_RESILIENCE_v1.08_CN.md`](docs/OPERATIONS_RESILIENCE_v1.08_CN.md)。

要确保独立看护在没有 GUI 的情况下继续工作，也可以执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage_backend_watchdog.ps1 -Action Enable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage_backend_watchdog.ps1 -Action Status
```

原来的 `start_backend.ps1` 仍用于前台诊断，不能在同一个端口重复启动第二个 Backend。

## 企业安全要求

本地模型与本地数据库并**不自动等于完全无数据外传**。Open WebUI 外部提供商 / Web Search / 自定义工具必须关闭；必要时通过企业防火墙控制外连。公司 Wi-Fi 端口共享必须由 IT 授权，并配置内网 HTTPS。现有 Open WebUI 与 Backend 通过共享 API Key 对接，**尚未实现服务端按用户/项目隔离**；如业务数据存在用户级权限，必须补充可信身份传递 + Backend RBAC 后才能正式多人部署。Qdrant 如果仍监听 `0.0.0.0:6333/6334`，请先安全收紧且保留 `qdrant_storage` 数据卷。不可把提示词注入或生成式判断当成来源证据。

## 工程智能数据库迁移

请继续严格按 [`docs/ENGINEERING_INTELLIGENCE_v1.07_CN.md`](docs/ENGINEERING_INTELLIGENCE_v1.07_CN.md) 进行备份、审查、显式 `ei_*` 迁移、分批离线回填、检查和人工审核；v1.08 **不更改数据库版本**，不改变 BGE-M3 向量维度或 Qdrant Collection ID，不自动跑迁移。要求级双端证据、未解析引用和历史版本一致性保护保持原样。

## 验收边界

- Python 静态/选定自动化测试在隔离环境执行。Windows UAC、PowerShell 5.1、真实 PostgreSQL/Qdrant、模型性能、内网 HTTPS、数据恢复演练、EXE 打包和多机故障切换需**在部署环境单独验收**。
- `DIC_MAX_ACTIVE_CHATS` 是单个 Backend 进程的准入上限，不能推导出有几人可并发使用；需要用你电脑的 GPU/CPU 和真实业务数据测试。
- 版本历史/关系的深层语义抽取、Sparse+Dense 混合检索、可恢复并行 DAG 和要求级 QA/BUG 到测试全链路仍属于后续工程，未作为本版本已完成能力发布。
