# 隔离环境与测试人员工作流增量交付

本轮已实际实现固定条件流程、精确索引核账及隔离 SQL/向量验收。生产库五项业务需求仍不能全部签收：完整召回、企业内容关系回填、适用版本核准与符合性判断仍有缺口。旧验收报告是历史记录，以本记录及 `RELEASE_ISOLATED_WORKFLOW_VALIDATION.json` 为本轮增补。

## 实现内容

- 新增 `PRE_TICKET_CHECK`：先在 QA/BUG 的原始观测字段中做向量发现，再回 SQL 校验正文哈希和票据身份。同一票的多个片段归并；达到 0.72 相似度阈值的候选会停止式样兜底。没有强匹配才查询明确工程的式样。阈值只作候选分支条件，不是重复问题概率，尚需企业标注集校准。
- 观测字段限 SUMMARY、DESCRIPTION、BUG_PHENOMENON、BUG_ACTUAL_RESULT、QA_ACTUAL_BEHAVIOR；修复后效果、对策、单纯前置条件不能单独触发重复票分支。字段类别与票号回查 SQL，不信任向量载荷自报身份。
- 新增 `TICKET_SPEC_TRACE`：从票内明确参照字段解析文件名、历史版本、章节和页码，以 SQL 精确核验对应正文。缺失或歧义会保留为未解析引用；不会偷偷换成最新版本或其他编号。
- 两条流程均不调用模型规划或自由生成。输出是来源摘录及固定状态，`conformance=UNDETERMINED`；相似票、引用关系或排名不能代替适用软件/式样版本映射及完整技术条件核验。
- Open WebUI 沿用原模型标识和接口，无需更换前端。非流式 `rag.workflow` 与流式结束包提供步骤和状态；结构化入口为 `/api/v1/retrieval` 的 `operation=testing_check`。
- 新增每请求合作式检索预算、SQL 单语句/锁等待限制及 Qdrant 查询超时。默认流程预算 25 秒。首次本地 BGE 冷加载不可中途强制停止，可能超过该预算；加载结束后会检查预算并明确返回未完成。生产部署仍需启动预热和单独的并发/断连验收，不能把该预算宣称为硬实时保证。
- 更新项目内 QA/BUG Skill 说明，保留离线快照技能及其证据限制。

## 真实索引核账

使用与当前同步服务相同的 SQL 入选条件，流式读取 ID 和正文哈希，在本地 SQLite 中做集合比较。没有修改生产数据或点。

| 数据域 | 合格 SQL 点 | 实际向量点 | 缺少 | 不再合格的多余点 | 重叠部分正文哈希不一致 |
|---|---:|---:|---:|---:|---:|
| QA/BUG | 949679 | 38592 | 911087 | 0 | 0 |
| 21MM | 89872 | 54095 | 37706 | 1929 | 0 |
| 24MM | 31838 | 31838 | 0 | 0 | 0 |
| COMMON | 0 | 0（集合缺失） | 0 | 0 | 0 |

这取代上轮单纯比较计数的估计。以上检查只认证 ID/正文哈希覆盖，不认证所有模型/索引元数据、检索语义或符合性。SQL 使用 repeatable-read 快照；Qdrant scroll 没有事务快照，检查前后计数一致也不能证明期间绝无等数量替换。COMMON 空库不属于“已有可用资料”。精确核账结果见 `docs/acceptance/index_coverage.json`。

## 隔离环境实际验证

建立独立 PostgreSQL 17 实例，工作区单独目录，127.0.0.1:15432；使用新建 `dic_accept_*` Qdrant 集合。四个工程域的隔离库初始化成功。所有测试文档/票据明确标记为合成样本；没有复制生产企业正文。

| 验证 | 结果 |
|---|---|
| TXT、PDF、DOCX、XLSX、PPTX → JSON → 真实 SQL → 真实 Qdrant | 合成样本通过 |
| QA XLSX → 规范 JSONL → 专用 SQL → Qdrant | 合成样本通过 |
| 增量同步重复执行 | 第二次 upsert=0，通过幂等检查 |
| 式样 550 v1.00→v1.01 | 保存的 READY 变更集可读，正文 500ms→700ms；需求单元对齐含 1 MODIFIED、1 UNCHANGED |
| 正文定位 | 返回明确版本、章节 2.1、第 17 页及 SQL content_id |
| 内容级关联候选 | 550 同句明确引用 531 的 2.2 节；生成来源双方完整的候选 |
| 受控关联审核 | 在隔离测试身份下调用受控函数成功，读取到 1 条受控关系；直接更新被阻止。只验证权限路径，不是企业人工审核 |
| 票据明确引用历史正文 | REFERENCE_RESOLVED，约 93ms |
| 相似票分支 | POSSIBLE_DUPLICATES，约 250ms，未执行式样兜底 |
| 无强匹配转查式样分支 | SPEC_EVIDENCE_REVIEW_REQUIRED，约 297ms |

这些是小规模、已知答案的合成验收，不代表企业全格式复杂版式、生产内容覆盖、人工标注质量或并发负载通过。隔离报告见 `docs/acceptance/`；凭据、PostgreSQL 数据目录、原始企业响应均不进入发行 ZIP。

## 生产只读与 Open WebUI 复测

此前两条复杂问题都在 180 秒超时。本次固定流程首次复测分别约 0.9 秒（票据溯源）和 2.3 秒（起票前检查），热缓存复测约 0.3 秒、0.55 秒；这些只是本机样例耗时，不能当作性能承诺。

真实 `MM21SPMAN-88` 能返回票据与引用，但其精确文件名 `531_Connected_Radio_Function_Ver2.02.docx` 未在本次 21MM documents 查询中命中，spec_code=531 的登记也未命中。因此结果是 `REFERENCE_INCOMPLETE`，不能宣称目标正文已找到。文件可能未导入或命名/登记需要核准，不能据此断言企业中没有这份文件。

真实蓝牙切歌无声查询已在 Open WebUI 临时聊天中显示：先查票、没有达到阈值的强匹配后查式样、展示票据候选和带页码/章节/content_id 的式样摘录，符合性保持“证据不足”。这是流程和来源输出通过，仍不是“没有历史重复票”或“现象符合式样”的业务结论。

## 修复的真实 SQL 问题

1. 空库初始化的 `engineering_relation_catalog` 视图缺少分组列，导致一键建库失败；补齐 run.id/src.id，已在四个隔离库验证初始化。
2. v1.33 受控审核函数的 PL/pgSQL 变量 `version_id` 与列同名，真实调用报 AmbiguousColumn；改为 checked_version_id，重新安装 v1.33/v1.37 后完成真实隔离审核验证。**修改了发行源 SQL 文件，但没有对生产执行该迁移。**
3. 旧流式服务调用约定继续保留；只有新流程使用新增状态回调。修复 Windows 测试读取 UTF-8 源码时依赖系统编码的问题。

## 运行与复现

正式 Backend 默认启动方法不变。另增加 `python -m app.server --config-dir <明确配置目录>`，便于隔离配置；不传参数仍使用原用户配置。不得把验收实例当生产库。

只读工具：

```powershell
python scripts/live_acceptance/audit_index_coverage.py --scopes QABUG 21MM 24MM COMMON
python scripts/live_acceptance/plan_production_upgrade.py
```

独立实例和合成验收（需要已有 PostgreSQL 工具、本机 Qdrant 和已缓存 BGE）：

```powershell
python scripts/live_acceptance/isolated_environment.py setup
# Windows 受作业生命周期管理的运行环境需另开受控终端保持实例：
python scripts/live_acceptance/isolated_environment.py serve
# 在另一个终端继续；如果 setup 已初始化成功，initialize 可重复校验：
python scripts/live_acceptance/isolated_environment.py initialize
python scripts/live_acceptance/run_isolated_business.py all
python scripts/live_acceptance/verify_isolated_review.py
python scripts/live_acceptance/isolated_environment.py stop
```

`setup` 遇到已有状态/目录/端口会拒绝覆盖；`serve` 要求实例当前未运行。已运行时无需再次启动 serve。私有凭据只在 `output/isolated_acceptance/`，该目录及其他输出均不发包。隔离审核脚本只创建测试角色和合成状态，不得修改成生产目标后直接执行。

## 仍待生产落地

- 先核准导入文件、数字编号、主正文和适用版本，建立企业真实标准问答/查重标注集。
- 备份并演练恢复后，审核 `docs/acceptance/production_upgrade_plan.json` 中的增量迁移。派生表、非登录函数所有者、人员审核权限和回填应分开审查，不自动给应用管理员充当人工审核人。
- 按精确核账清单分批补齐缺少的点。QA/BUG 同步会实际写入超过 91 万个向量，耗时和资源需单独评估；本轮没有启动。21MM 的旧点也没有自动删除。
- `QABugQdrantManager` 目前仍会将当前行及现有点列表加载到内存；百万片段生产补齐前，需要进一步改为分页/持久检查点及可取消批处理，不能仅凭本轮小样本幂等验证宣称规模验收通过。
- 生产内容单元和关联仍未回填；软件→适用式样映射仍未核准。因此五项需求的总体状态仍是“部分完成、不可完整生产签收”。

本轮结束后关闭临时 Backend 和独立 SQL 实例；保留隔离数据目录及测试集合供复核，未删除生产点、未更改 Wi-Fi/LAN、生产凭据或 Open WebUI 配置。RAGFlow 仍是默认关闭的可选风格适配层，不具备完整 RAGFlow DSL/API/数据库互换能力。
