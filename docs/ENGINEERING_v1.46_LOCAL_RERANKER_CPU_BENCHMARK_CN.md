# v1.46 — Stage 4 本地 CrossEncoder、CPU 边界与可重复质量/延迟评测

## 适用范围

本轮为可选、**默认关闭**的 Stage 4 二阶段排序；保留 v1.45 SQL + Dense (+ Sparse) RRF 顺序作为失效回退。Reranker 不决定工程关系有效性、不判断 BUG 是否符合要求、不能补全缺失证据；未经审核的语义关联及 QA/BUG、章节关系、历史差异都不送入本模型。

线上只允许 `HYBRID_CHUNK`、SPEC_BODY、本体 RRF 且有 `POSTGRESQL` 正文召回来源或已验证 `POSTGRES_HASH_AND_IDENTITY_VERIFIED` Sparse 来源的候选进入重排。**旧版 Qdrant 的 Dense-only Payload 不具备独立 PostgreSQL 原文核验，因此不得进入 CrossEncoder。** 本轮全部候选中有任意未经验证项时，整个重排跳过，不通过删掉未知候选改变排序结果。

模型接收受限的 `(question, original_body_text)`；不传入凭据、关系审核决议或 BUG 内容；候选排名只记录 `rerank_rank / rerank_original_rank / reranking_only`，保留原 `rrf_score`，不制造“工程置信度”。

## CPU 安全设计与代价

- Python `sentence_transformers.CrossEncoder` 在 **spawn 子进程**中按 `device='cpu'`、`local_files_only=True`、`trust_remote_code=False` 初始化；启动前设置 Hugging Face 离线环境变量。操作员应先离线下载、审查模型文件，放入可信本地目录。
- 每次请求最多重排 12 项（可设置 2–30）；查询裁剪为 512 字符，正文为 1800 字符；默认 batch 2，Torch CPU intra-op 线程 2、inter-op 线程 1。
- 每次推理由可终止的子进程执行，默认 30 秒硬截止（1–120 秒）；超时、模型缺失、异常或非法非有限分数一律保留原 RRF 顺序。进程只允许一个推理槽位；并发第二次请求快速回退。不会误将已经超时的 Torch 推理留在主 GUI/Agent 进程后台运行。
- 冷启动：每次请求都加载一次本地模型，CPU 上可能耗时很高；这是为了获得进程隔离与可杀死的硬超时，**不宣称低延迟**。生产要降低成本需要后续开发经审计的持久隔离推理服务、缓存策略及监控。
- 默认**没有**跨平台物理内存硬上限：Python/Torch 线程数限制不等于内存配额。高级使用者可在 Linux `RerankConfig(max_address_space_mb=...)` 使用 `RLIMIT_AS`，需在对应模型上验收；Windows 内存硬限额需要 OS Job Object 或外部服务，当前未实现。GPU、Windows frozen EXE 也未验收。

## 启用（PowerShell）

当前 `sentence-transformers` 为现有依赖，模型必须事先在可信网络/机器下载并经内部审批后复制为本地目录；运行时不需要互联网。

```powershell
$env:DIC_STAGE4_RERANK_ENABLED = '1'
$env:DIC_STAGE4_RERANK_MODEL_DIR = 'D:\models\approved-bge-reranker-v2-m3'
$env:DIC_STAGE4_RERANK_TOP_N = '12'
$env:DIC_STAGE4_RERANK_BATCH_SIZE = '2'
$env:DIC_STAGE4_RERANK_CPU_THREADS = '2'
$env:DIC_STAGE4_RERANK_TIMEOUT_S = '30'

# 同一个进程启动应用 / 测试 Agent；不需要执行 schema 迁移。
# 不需要新建 Qdrant Collection；Sparse 是否启用仍由 v1.45 独立控制。
```

没有设置 `DIC_STAGE4_RERANK_ENABLED=1` 时完全保留既有检索路径；从环境变量修改后需要重启进程以统一运行配置。

## Golden Dataset + 实测 CPU 延迟

参考 `scripts/evaluate_stage4_rrf.py` 的数据结构；新增本地基准**每行必须含 `query`、`query_id`、`relevant`、`sql`、`dense`、`sparse`**。`relevant` 必须是人工从真实 SPEC_BODY 文段标注的目标文段，而非模型自动分配的标签。每个检索候选均须具有 `scope/document_id/specification_version/content_id/file_name/content`。候选列表必须来自**同一时点**真实 SQL、Dense、Sparse 检索结果。正式测试前必须核对哈希及版本真实归属；本离线脚本不能完成数据库真实性证明。

```powershell
.\.venv\Scripts\python.exe .\scripts\benchmark_stage4_reranker.py .\golden.jsonl `
  --model-dir 'D:\models\approved-bge-reranker-v2-m3' `
  --k 10 --min-recall-delta 0 --max-p95-ms 60000 `
  --output .\stage4-v146-benchmark.json
```

脚本对每条问题真实运行 RRF 与本地 CrossEncoder，记录同一离线重放中的 Recall@K、Precision@K、MRR@K、增益以及 RRF 与整个二阶段调用的 p50/p95 墙钟时间。二阶段计时包括进程启动与模型加载，但**不包括**实时 PostgreSQL、Qdrant 检索、嵌入、Ollama 及界面渲染。任何查询超时、模型未执行、缺失 Sparse、候选少于 2 项或标签缺失时输出 `BLOCKED`，不能伪装为验收通过。

上面的质量/延迟阈值仅是命令格式示例，并非已完成的真实基准或约定的企业验收线。还应分别采集实际整链 p50/p95、峰值内存、长时间并发及故障回退数据，并验证 QA/BUG 与工程关系的输出没有被重排影响。

## 本轮实际已验收 / 未验收

- **已完成**：原始源码及模拟检索数据上的拒绝性单元测试、全量离线 pytest、归档哈希与 Python 编译（见交付说明）。
- **未完成**：真实本地 CrossEncoder 权重加载、PG17/Qdrant/FlagEmbedding 原库集成、真实客户 Golden、真实 508→550 PDF、Windows GPU/CPU 实机、Windows EXE、内存/整链响应延迟。Ruff 如不可用应标记未执行。
- 本次没有连接或修改用户 PostgreSQL、Qdrant、历史版本映射或人工审核状态。
