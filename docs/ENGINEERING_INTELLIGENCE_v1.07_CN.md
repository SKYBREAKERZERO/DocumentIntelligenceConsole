# Engineering Intelligence v1.07 — 要求级知识完整性加固与实施状态

## 实施原则

- **本地优先**：只连接你的 PostgreSQL（默认 127.0.0.1:5432）；没有云端模型或网络 API 调用。保留原 Open WebUI、FastAPI、Qdrant、Ollama 配置。
- **原文权威**：PostgreSQL `specifications`、`specification_versions`、`documents`、`contents` 仍为 Source of Truth；`ei_*` 全部是附加的派生表，不更改原库的业务表结构。
- **两端证据**：新关系必须同时有 source、target 原文、段落边界、document_id、content_id、Section、版本，且默认 `REVIEW_REQUIRED`。不使用向量相似度直接确认工程关系。
- **不可静默跨域**：明示 21MM / 24MM 的请求必须遵守范围；只有用户明确询问跨域的 QA/BUG 影响，Agent 才会同时检索 QABUG。
- **保持旧功能**：原有版本 Diff、文档级关系图、QA/BUG SQL、Qdrant 检索不被替换；未建立 v7 schema 时，新 API 返回 `SCHEMA_NOT_INSTALLED`，原 Agent 可继续使用。
- **增量与预算**：要求单位由现有 SQL 内容离线生成；每个单元保存 Hash；数据库写入使用 ON CONFLICT DO NOTHING；批量读取使用 content_id 游标页；版本匹配最多 20,000 次候选比较，避免无限两两比较。
- **完整性保障**：某个 Content 超过提取上限时立即失败并回滚，绝不悄悄截断；每个成功处理的版本写入 `ei_version_backfill_receipts`，包含源行数、要求数、最大 Content ID 和 SHA-256 源指纹（空白行也计入）；映射/关系读取前必须验证完整回填。
- **引用精度**：引用 Spec 550 Section 4.1 不等于引用其中任意具体段落；目标 Section 存在多个要求单元时写入 `ei_unresolved_relation_anchors`，不会随机生成段落级关系。

## 当前已实现的增量功能

1. `ei_requirement_units`：从原始内容抽取行/段落级原文单元，记录准确字符偏移、章节/页码、原文 Hash、条件、时序、例外线索；默认 `AUTO_EXTRACTED`，不是已批准需求。
2. `ei_content_relations`：只对原文明确的目标式样号 + Section 建立候选边；若该 Section 只有唯一目标单元才建立双端段落候选；多目标/无目标/未回填通过 `ei_unresolved_relation_anchors` 保留待处理信息，不伪造精确关系。
3. `ei_version_mappings`：确定性对齐相同段落、移动段落；相似文本修改保存 `REVIEW_REQUIRED`，可查看旧/新证据。只在两版具备完整回填收据时进行差异判断。
4. `ei_relation_review_audit`：显式人工批准/否决关系并记审计记录；更新不能由 LLM 触发。
5. 原 `relation_search` / `version_search` 在存在 v7 数据时增补双端证据；没有新数据时保留原检索行为。
6. Agent Workflow 白名单、步骤/范围预算、有限重试；模糊术语可保守扩展（不是擅自把不同实体当同义词），明确的跨域 QA/BUG 问题可触发组合检索。
7. 带 Bearer Token 的本地只读 API：`/api/v1/intelligence/requirements`、`/api/v1/intelligence/relations`、`/api/v1/intelligence/version-mappings`。

## 升级步骤（在 Windows PowerShell 中）

**强烈建议先备份整套配置和数据库。** 不要覆盖正在运行的 v1.05 安装目录。下面命令假定 v1.07 已解压到 `C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.07_CN_FULL`。

```powershell
cd 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.07_CN_FULL'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt

# Step 1: preflight (no data changes)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\init_intelligence_schema.ps1 -Project 21MM -Database rag_21mm
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\init_intelligence_schema.ps1 -Project 24MM -Database rag_24mm

# Step 2: backup + additive migration. Requires disk space for full pg_dump.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\init_intelligence_schema.ps1 -Project 21MM -Database rag_21mm -Apply
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\init_intelligence_schema.ps1 -Project 24MM -Database rag_24mm -Apply

# Step 3: dry-run a small sample
.\.venv\Scripts\python.exe .\scripts\build_engineering_intelligence.py --database rag_21mm --project 21MM --limit-specifications 3

# Step 4: backfill all MAIN versions. Read-only SQL source; writes only ei_* tables.
.\.venv\Scripts\python.exe .\scripts\build_engineering_intelligence.py --database rag_21mm --project 21MM --all-versions --version-mapping --relations --apply
.\.venv\Scripts\python.exe .\scripts\build_engineering_intelligence.py --database rag_24mm --project 24MM --all-versions --version-mapping --relations --apply
```

实际库名应以你的 Settings 为准，确认 `project_info.project_code` 一致。迁移脚本不会删除 Qdrant Volume。以上命令可能较慢，建议在无活跃文档导入的维护时间运行。处理超大型式样书时，先用 `--limit-specifications 3` 分批验收。 如果单个源 Chunk 的有效行数超过默认 200，系统会拒绝截断并报告 Content ID；在核实来源后可对离线回填增加 `--max-units-per-content 1000`（允许 1–10000），不要直接用断行丢数据。

**版本映射**：`--version-mapping` 必须配合 `--all-versions`，避免只存新版本造成不完整对比。离线回填不是聊天请求路径的一部分。

**查询 API**：

```text
GET http://127.0.0.1:18080/api/v1/intelligence/requirements?scope=21MM&spec_code=508
GET http://127.0.0.1:18080/api/v1/intelligence/relations?scope=21MM&spec_code=508
GET http://127.0.0.1:18080/api/v1/intelligence/version-mappings?scope=21MM&spec_code=508
GET http://127.0.0.1:18080/api/v1/intelligence/unresolved-anchors?scope=21MM&spec_code=508
Authorization: Bearer <your existing local API key>
```

`/v1/models` 与 `/v1/chat/completions` 保持原有 Open WebUI 接入方式，不需要新建 API Key。切换新版本后请先停掉旧 Backend，确保 18080 由新进程监听。

## 只读完整性检查（建议每次回填后运行）

```powershell
.\.venv\Scripts\python.exe .\scripts\verify_engineering_intelligence.py --database rag_21mm --project 21MM
.\.venv\Scripts\python.exe .\scripts\verify_engineering_intelligence.py --database rag_24mm --project 24MM
```

检查结果的 `main_versions_without_receipts` 包括未选择回填的历史版本，不能直接认定为故障；`stale_chunk_count_receipts > 0` 应暂停版本对比并检查源快照。`unresolved_reasons` 是需要后续做证据审核/更精确定位的项目，不等于工程矛盾。

**迁移兼容**：如果此前安装过不含收据表的 v1.06 schema，重新执行本版本 `init_intelligence_schema.ps1 -Apply`（会先备份、CREATE TABLE IF NOT EXISTS）；旧版本没有收据，需重新运行显式回填命令生成，不会自动把历史不完整数据标为已验证。

## 人工审核关系（只限本机开发者）

```powershell
.\.venv\Scripts\python.exe .\scripts\review_engineering_relation.py --database rag_21mm --project 21MM --candidate-id 123 --status CONFIRMED --actor 'reviewer' --reason 'Validated both source sections against approved specification'
# 预演核对后再加 --apply
```

关系的人工确认是显式操作。先核对两端原文是否正确，不要批量自动确认。

## 目前尚未完成的后续阶段（不能称为已上线）

- 强语义级 Requirement 抽取：PDF 表格单元、嵌套条件、否定/例外、同一长段内多条要求，以及规范级身份追踪，需要评估真实式样。
- 跨式样书逻辑依赖/冲突的自动关系判定：目前只生成精确 `Spec No + Section` 引用的候选；相似而未显式引用的关系仍待本地 reranker + 人工标注评估。
- 高质量 Sparse/BM25 + Dense + RRF + 本地 Reranker 混合检索。
- 真正可并行、可中断/恢复的 Agent DAG 引擎；v1.07 具备安全工作流图表示，现有工具执行仍按原顺序。
- QA/BUG 对应具体要求、测试用例以及逻辑冲突求解器的完整跨库链接。
- 生产级 Windows/局域网 HTTPS、RBAC、离线外传封锁及真实 P95 压力验收。

下一版应基于真实式样样本和人工核验集继续实现。不能用无原文依据的模型输出冒充已确认工程关系。

## 最小验收标准

- 当前项目 SQL schema v6 和 Qdrant Collection 不变，现有 RAG 可以继续问答。
- 对 508 明确引用 550 的 Section：目标只有唯一单元时返回两端准确原文和版本，默认 REVIEW_REQUIRED；目标存在多个单元时只返回待解析 Section 引用，严禁凭相似度凑出段落级边。
- 修订版变化可找到旧/新段落，MODIFIED 未核验必须明确提示。
- 21MM 查询不混入 24MM；仅明确跨域提问时才组合 QA/BUG。
- 同样数据再次回填的新增行数不应增加；原始内容被修改时要拒绝静默覆盖旧收据；离线任务不可影响在线首 token 延迟。
- 仅做了静态与隔离回归测试；当前没有在你的 Windows PostgreSQL 实例上执行过迁移和回填。实际部署须先备份，并通过维护窗口的抽样验收。
