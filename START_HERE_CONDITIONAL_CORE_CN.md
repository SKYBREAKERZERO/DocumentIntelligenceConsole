# v1.54 条件核查核心后台：交付入口

本包是基于 `DocumentIntelligenceConsole_Enterprise_v1.54_ALL_IN_ONE.zip` 的完整源码增量包。原目录、原接口、数据库域、七项 Skills、独立 Backend、Open WebUI 对接和默认 internal-only 设计均保留。解压后先阅读 `START_CORE_BACKEND.md`，本轮技术细节见 `docs/CONDITIONAL_CHECKS_AND_SYNC_CN.md`。

## 本轮已实际完成

- QA/BUG SQL → Qdrant 同步改为主键分页、每批持久化、中断后重跑、丢点自动补回、同集合互斥、来源并发变化拒绝、全量重算不先删集合。
- QA/BUG SQL/Qdrant 质量核对改为分页，检查缺点、孤点、正文、哈希、索引结构和扫描前后稳定性，并接入一键流程的取消状态。
- 新增受控的“软件版本/车型/地域 → 式样版本”映射和有限条件检查；结果严格区分配置范围内符合、疑似不符合和证据不足。用户文字、相似度和模型回答不能自授审批权。
- 条件核查接入固定测试人员工作流、认证 API、OpenAI-compatible 普通聊天和 SSE；回答显示式样编号、版本、文件、章节、物理页、content_id、SHA256 与原文。
- 新增默认只读的管理员提案/审核工具、模板、可选数据库迁移、部署边界说明，并更新 `engineering-qa-bug-analysis` Skill。

## 用户提出的五项需求状态

| 需求 | 当前代码能力 | 当前真实验收边界 |
| --- | --- | --- |
| 1. 关键词/现象定位具体式样位置 | 已实现混合检索、SQL 原文核验、文件/版本/章节/物理页/content_id 引用 | 合成场景通过；生产 21MM 向量仍有历史缺口，不能声称完整召回 |
| 2. 多版本增删改 | 已实现保存版本差异、前后原文和定位；合成样例覆盖新增/删除/修改 | 企业真实版本身份、主文档和历史登记仍需管理员核准/补录 |
| 3. 式样内容关联 | 已实现字面引用候选、双端证据和受控审核，不把候选自动当成已批准依赖 | 合成跨式样引用通过；生产关系尚未完整回填和人工审批 |
| 4. QA/BUG 定位引用式样 | 已实现票号精确读取、票内明确文件/版本/章节引用解析和历史正文定位 | 合成 QA/BUG 通过；生产票缺失/歧义引用仍会明确返回未解析 |
| 5. 起票前查重，再查式样并判断 | 已实现候选查重分支、无强候选后式样兜底、受控有限条件判定 | 查重召回仍需企业标注集校准；仅已审核规则可判定，整本式样符合性不自动认证 |

因此，本包已经实现这些需求的核心代码路径和合成端到端验证，但没有完成企业真实数据的生产签收。生产大规模补向量、真实版本/关系/检查审核、真实 Open WebUI 浏览器会话、多用户压力、复杂版式金标准和 EXE 安装包验证仍需单独实施。

## 已验证证据

- 扩展受影响回归：463 passed，41 个测试文件，0 failed/error/skipped。
- 真实隔离 PostgreSQL（127.0.0.1:15432）、本机 Qdrant、本地缓存 BGE-M3：14 个 QA/BUG 合成片段的丢点修复、中断保留 2、续跑补 12、再次写入 0、分页质量检查通过。
- 条件核查：边界值通过/失败、缺少例外条件、例外生效、错软件、错车型、原文哈希漂移、映射指纹漂移、撤销审核、普通/流式聊天与认证接口均通过。
- 原有 11 项典型业务场景全部重跑通过；四份合成 PDF 的目标条款、章节和物理页此前已经逐页验证。
- 包内不包含隔离数据库、测试密码、本地备份、生产原文输出、运行日志或缓存。

机器可读证据在 `docs/acceptance/`，总报告是 `RELEASE_CONDITIONAL_CORE_VALIDATION.json`，逐文件清单是 `RELEASE_CONDITIONAL_CORE_MANIFEST.sha256`。历史 `RELEASE_*` 文件保留用于说明以前快照，不代表当前最终状态。

## 运行入口

桌面后台：

```powershell
./run.ps1
```

独立 Backend：

```powershell
./start_backend.ps1
```

管理员先在桌面后台保存 PostgreSQL/Qdrant/Ollama/Open WebUI 配置并初始化所需数据库，再执行文件转换、SQL 入库、向量同步和质量核对。Open WebUI 使用原 `document-intelligence-rag` 模型标识连接 Backend。

生产迁移计划只有只读模式：`python scripts/live_acceptance/plan_production_upgrade.py`。本包不会自动把测试迁移、合成样例或缺失向量写入生产。

## RAGFlow 兼容边界

保留原可选 RAGFlow 风格知识库、检索、聊天和固定工作流适配，默认关闭。它借鉴 RAGFlow 的数据集/检索/聊天/流程组织方式，不是 RAGFlow 引擎、DeepDoc、Canvas DSL、租户 ACL、官方 SDK 或原生数据格式的全量兼容层，也不能直接导入官方 Canvas JSON。详见 `docs/RAGFLOW_COMPAT_CN.md`。
