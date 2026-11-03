# v1.44 — Stage 4 混合检索第一阶段：SQL + BGE-M3 Dense + RRF

## 范围及状态

本轮已把 `app/ai/retrieval.py` 原先“按工具顺序拼接 SQL / Dense 命中”的路径改成 RRF 融合，仅融合可识别的**工程式样本体内容块**。已有 BGE-M3 1024 维 Dense 模型、Qdrant 当前索引以及 PostgreSQL `rag_current_chunks` 均沿用现有实现；没有执行向量重建、SQL 迁移或 DB 写操作。

**尚未实现/未验收：** BGE-M3 原生 Sparse 编码和 Qdrant Sparse 向量索引、三路 Dense/Sparse/SQL RRF、CrossEncoder 本地重排、生产 Golden Dataset、实际 PostgreSQL 17/Qdrant 查询、Windows GUI、CPU 内存/延时基线。不得把 v1.44 当成完整 Stage 4。

## 检索流程

1. 现有 Agent 根据工具计划运行 `sql_exact_search`（PostgreSQL 条件搜索）和 `semantic_search`（Qdrant Dense）。只有两条检索工具均在计划内才启用本轮 RRF；单工具使用旧路径。QA/BUG、版本审计、结构引用、审核关系与候选建议**绝不纳入** SQL/Dense 内容块 RRF。
2. 将 `scope + document_id + specification_version + content_id` 作为严格的内容块身份，项目、物理文档或版本不同均不合并。调用已有 `PrimarySpecificationDocumentPolicy` 过滤 Coversheet、审批表等支持文档；缺失身份者不参加 RRF。
3. 候选召回源内部排名从 1 开始，采用 `RRF(d)=Σ 1/(60 + rank_source(d))`。同一源、同一内容块的多个问句命中只计一次；结果稳定排序。
4. 如果 SQL 与 Qdrant 声称相同内容身份，但全文不一致或两个非空的 `content_hash` 不同，则**取消 Dense 的融合投票**，只保留 SQL 原文，标记 `vector_source_mismatch=True`，不伪造一致性。没有 SQL 的 Dense 候选不自动获得 PostgreSQL 来源可信声明。
5. `HYBRID_CHUNK` 元数据提供 `rrf_ranks`、`rrf_score`、`retrieval_sources`、原始 Dense 相似度（如有）、`ranking_only=True`。RRF 数值不是准确率、关系确认、工程符合性结论或证据等级。证据治理继续在 RRF 之后执行。
6. 精确时序核验仍为独立单工程/只读终止路由，不执行 RRF。未经审核的 BUG→SPEC 候选仍只在展示区，不能进入模型上下文。

## 离线 Golden Dataset 评测（必须由真实业务数据标注）

使用 JSONL，每行含 `query_id`、`relevant`、`sql`、`dense` 四个字段；后三者是对象数组，至少含 `scope, document_id, specification_version, content_id, file_name, content`。`relevant` 应由审核者标注正确的**原始正文**，`sql/dense` 为独立运行两种检索时记录的排名顺序。`content` 为真实来源快照，不允许混入 Coversheet 或客户外的数据。**评估命令是本地文件读取，不会主动查询 PostgreSQL/Qdrant，不会自动生成标注。**

```powershell
python .\scripts\evaluate_stage4_rrf.py .\golden_stage4.jsonl --k 10 --output .\stage4_rrf_metrics.json
# 人工设定门限后启用门禁，例如（数值只是命令示例，并非既定企业标准）：
python .\scripts\evaluate_stage4_rrf.py .\golden_stage4.jsonl --k 10 --min-recall 0.80 --min-mrr 0.60
```

输出 `Recall@k`、`Precision@k`、`MRR@k` 的 SQL/Dense/RRF 可比结果及 `offline_only=true`。未提供真实标注、空查询集、非法字段、重复查询等必须 `BLOCKED`，退出码 2。由项目负责人制定阈值并核对样本覆盖范围后，才可用于实际质量验收。

## 现场待验门禁

- 在 PG17/Qdrant 配置下使用真实 508/550 SPEC_BODY、QA/BUG 与非正文对照样本；核对数据库角色权限、索引覆盖率以及 Dense payload 的原文与 SQL `content_hash`。
- 逐项复核无编号模糊检索、精确编号、历史版本误召回、Coversheet 排除、跨项目碰撞、QA/BUG 隔离及已审核关系的证据边界。
- 再独立加入 BGE-M3 Sparse 存储和查询契约与本地重排开关；在真实 Golden Dataset 比较 SQL、Dense、Sparse、二/三路 RRF、Reranker，并记录 P50/P95、内存和 CPU 使用，不影响离线已审核证据。
- 无人工验收签名、客户真实源文件和现场性能数据时，仍标记 `NOT_VERIFIED`。
