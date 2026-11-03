# v1.45 Stage 4 — BGE-M3 Sparse 独立索引与三路 RRF

## 已有实现与边界

- 继续使用已有 PostgreSQL `rag_current_chunks` / SPEC_BODY 精确检索与未命名 1024 维 Dense Qdrant 向量，不更改其 Schema。
- 新增 BGE-M3 `BGEM3FlagModel` **原生词元 ID → 学习权重**的 Sparse 编码器（不使用 BM25、哈希词元或假稀疏向量）。需要另外安装兼容的 `FlagEmbedding`；使用 CPU 和 FP32，延迟与内存可能明显增加。
- 对每个工程 Dense 集合建立一个**独立 Sparse sidecar**：`<原 Dense 集合名>__bge_m3_sparse_v1`；名称 `bge_m3_lexical_v1`，Qdrant `SparseVectorParams`。只写入 `(content_id, 稀疏向量, 精简身份/哈希元数据)`，**不在 Sparse Payload 存原文**。
- 从 PostgreSQL 的 `public.rag_current_chunks` 与 `public.documents` 经统一 `sql_primary_predicate` 过滤，拒绝 Coversheet/Supporting；缺失数据、重复 ID、过量记录和长文段阻断索引任务。只读取 PostgreSQL，不创建表、不更改 GRANT。
- 在线 Qdrant Sparse 命中按 `content_id` 从同一工程 PostgreSQL 获取正文，**必须校验**原始集合/工程、点 ID、文件、文档 ID、SPEC 代码、版本与 `content_hash`。检验失败的点丢弃，不参与融合。
- 查询只在 `DIC_STAGE4_SPARSE_ENABLED=1`、SQL + Semantic 混合路径及 Sidecar 可用时调用三路 RRF；否则自动保留 v1.44 SQL + Dense。`RRF_V2` 是排序分，不是工程证据置信度。
- Agent 的 `TEMPORAL_RELATION_AUDIT`、已审核工程关系、QA/BUG 仍走独立检索，不允许加入正文 RRF。

## 部署流程（PowerShell）

必须先完成本工程的 PostgreSQL 17 和 Qdrant 连接验证，并核对工程 Profile 对应的数据库与 Dense Collection，禁止把项目 A 的 DSN 与项目 B 的集合混用。

```powershell
# 按本机隔离 venv 版本验证后安装（可选依赖；基础启动默认无需此包）
.\.venv\Scripts\python.exe -m pip install FlagEmbedding

# 示例：请替换为与 21MM 对应的真实连接字符串和已使用的 Dense 集合名；
# 在实际环境中请通过受保护的环境变量/凭据管理注入密码。
$env:DIC_STAGE4_PG_DSN = "host=127.0.0.1 port=5432 dbname=<21MM工程DB> user=<只读用户>"
$env:DIC_STAGE4_QDRANT_URL = "http://127.0.0.1:6333"
# 若启用 Qdrant API Key，通过 DIC_STAGE4_QDRANT_KEY 注入

# 只读预览：不加载 FlagEmbedding、不触碰 Qdrant，也不写 PostgreSQL
.\.venv\Scripts\python.exe .\scripts\build_stage4_sparse_index.py `
  --project 21MM --dense-collection <真实Dense集合名> --max-chunks 10000

# 操作员确认身份、数量和旁路集合名后才明确启用写入
.\.venv\Scripts\python.exe .\scripts\build_stage4_sparse_index.py `
  --project 21MM --dense-collection <真实Dense集合名> --max-chunks 10000 --write

# 上线前在另一个 PowerShell 测试会话中设置
$env:DIC_STAGE4_SPARSE_ENABLED = "1"
```

**同步语义：** Sidecar 构建是受限的 UPSERT，不会删除旧 Sparse 点、不删除 Dense 点、不自动重建。PostgreSQL 源只读快照与 Qdrant 写入不是分布式原子事务。如果写入失败，旁路索引可能只有部分文段；上线前需对齐全量数量和跨版本哈希，并复跑。旧点、正文漂移和删除记录会在在线时被 SQL 核验拒绝，但不能代替完整的索引同步对账。不要把试运行或无异常退出当成生产验收。

## Golden Dataset 对比

`golden.jsonl` 每行包含：`query_id`、人工标注的 `relevant`、`sql`、`dense` 和可选的 `sparse` 候选数组。每条候选包含 `scope/document_id/specification_version/content_id/file_name/content`。所有标签必须来自真实式样正文的人工核对；不要用自动抽取候选充当测试真值。

```powershell
.\.venv\Scripts\python.exe .\scripts\evaluate_stage4_rrf.py .\golden.jsonl `
  --k 10 --ranker rrf_three_way --min-recall 0.80 --min-mrr 0.60
```

输出区分 `sql`、`dense`、`sparse`、双路 `rrf` 和三路 `rrf_three_way`。这是一套离线评估接口，**并未使用用户真实 Golden Dataset 验收任何具体阈值**；数值只是命令示例，不能当作项目验收标准。

## 尚未完成的现场门禁

1. 真实 `FlagEmbedding` 与本地 BGE-M3 模型加载及 GPU/CPU 对照；模型权重版本固定、设备内存与初始化耗时测量。
2. PostgreSQL 17 + Qdrant 1.19.0 实机写入/查询及 Sparse Collection Schema 验证，正式同步状态对账/删除策略与故障恢复。
3. SQL、Dense、Sparse、双/三路 RRF 的真实 Golden Dataset 准确率和 p50/p95 延迟基准；本地 CrossEncoder Reranker 尚未实现。
4. Windows GUI 与 508 → 550 原始 PDF 全链路手工核验；未审核语义关系仍不能当作已证明的工程依赖。

**本版本只交付可运行源码与离线测试，不自动连接或修改任何用户 PostgreSQL/Qdrant 实例。**
