# v1.49 · 按工程任务分类的 Agent Skills（研发版）

> 状态：离线受控执行与回答发布门禁集成完成；不是生产环境认证。保持 INTERNAL_ONLY、SPEC_BODY 优先、零数据库写入。

## 目标与架构

- `config/skills/<name>/SKILL.md`：兼容标准 Agent Skills 的工作说明；各目录另含 `manifest.json`，不会因拷贝到 Open WebUI 就自动获得 Python 权限。
- `app/skills/catalog.py`：固定的七技能 allow-list；`SKILL.md`/manifest 不允许注册 Python 可执行对象。
- `app/skills/executor.py`：离线 JSON 受控执行器，沿用既有 Stage 1–4 / BUG 工程代码，不执行动态 SQL、shell、网络请求或模型自造 Python。
- `app/skills/live_evidence.py`：**已经集成**到 `app/ai/rag_service.py` 的企业回答发布门禁；使用可信上下文组装器提供的 evidence ID 与 admissible EvidenceItem 对象。
- `scripts/run_skill.py`：单次本地 JSON 执行入口，2 MB 输入限制；错误退出码 2。并不连接真实 PostgreSQL/Qdrant。
- `examples/skills/*.json`：七个合成示例，仅供工作流/负向测试，不包含真实工程资料。

## 七类技能及其证据上限

| Skill | 阶段 | 工作内容 | 限制 |
|---|---|---|---|
| `spec-requirement-extraction` | 1 | 整句保留的来源单元、Unicode 原文跨度、hash、时序/数值/表格线索 | 非规范语义裁决 |
| `content-relation-audit` | 2 | 508→550 明确 SPEC+section 引用及双端原文快照检查 | 仅结构引用，不证明工程依赖 |
| `spec-version-impact` | 3 | 旧版/新版同式样 Requirement Unit 映射 | 生效时间、关系有效性仍未核验 |
| `engineering-hybrid-retrieval` | 4 | 三路 SQL/Dense/Sparse 离线排名与 RRF | SQL 用户快照非实时身份认证 |
| `engineering-qa-bug-analysis` | 6 | 现象/触发/期望抽取，候选要求词面排序 | 无真实版本授权映射不下符合性结论 |
| `rag-evidence-verification` | 7 | 引用与数值/版本的字面发布门禁 | 不能证明条件适用与语义正确 |
| `rag-quality-gate` | 7 | 带标记 Golden/合成案例的发布门禁评测 | 不等于企业生产验收 |

## Windows PowerShell 操作

从 v1.49 的源码根目录安装好原项目依赖，再执行：

```powershell
python .\scripts\run_skill.py --list
python .\scripts\run_skill.py --suggest "550 历史版本影响 508 的哪些要求"
python .\scripts\run_skill.py spec-requirement-extraction --input .\examples\skills\spec-requirement-extraction.json
python .\scripts\run_skill.py content-relation-audit --input .\examples\skills\content-relation-audit.json
python .\scripts\run_skill.py spec-version-impact --input .\examples\skills\spec-version-impact.json
python .\scripts\run_skill.py engineering-hybrid-retrieval --input .\examples\skills\engineering-hybrid-retrieval.json
python .\scripts\run_skill.py engineering-qa-bug-analysis --input .\examples\skills\engineering-qa-bug-analysis.json
python .\scripts\run_skill.py rag-evidence-verification --input .\examples\skills\rag-evidence-verification.json
python .\scripts\run_skill.py rag-quality-gate --input .\examples\skills\rag-quality-gate.json
python -m pytest -q tests/test_v149_engineering_skills.py
```

离线输入契约：

1. **要求提取**：`document_id, content_id, section_id, text, file_name, document_role=SPEC_BODY`，可有 `page_number, project, spec_code, version_id`。
2. **关联审核**：`source` 与 `target` 两个对象，每端有 `project, document_id, file_name, document_role, <side>_chunk, <side>_text, <side>_start, <side>_end, <side>_hash`；目标另带 `target_code, target_section`，均为原文快照。
3. **版本比较**：`old, new` 两个同项目同式样、不同 `version_id` 的 SPEC_BODY 正文快照；对齐不是有效性裁决；未匹配项只能报告 `UNMATCHED_IN_SUPPLIED_SNAPSHOT`，不等于已证实新增/删除。
4. **RRF**：`sql, dense, sparse` 三组按名次排列的快照对象，含 `scope, document_id, version, content_id, content, content_hash, file_name, document_role`；向量必须与 SQL 同身份同原文才算票数。
5. **BUG**：`bug` 原始字段及可选 `units`（已有 `SpecUnitSnapshot` 字段）；没有版本审批不能裁决符合性。
6. **证据**：`answer` 与 `evidence:[{id,text,label}]`，在线白名单必须由系统内部准备，不得取自用户或模型文本。
7. **质量**：`cases` 数组，每项满足已有 `scripts/evaluate_answer_gate.py` 的 `evaluate_case` 字段契约；真实案例标记也需要独立审计。

## 与 Open WebUI、Ollama、LangGraph 的关系

- **已完成**：`--suggest` 是纯能力建议，不会触发真实执行。RAG 企业回答发布路径调用 `verify_live_enterprise_answer`；非企业普通聊天保持原模式。
- **已有独立入口**：其余六类 Skill 通过项目本地 CLI 手工调用，**未**连接 Open WebUI 的自动 Tool 路由或 LangGraph。不能声称前台已经能够自动按问题类型运行七类 Skills。
- Open WebUI 若支持导入 Agent Skills，可手动参考/导入 `SKILL.md` 定义；不能通过导入说明文件直接获得受控 Python/SQL 能力。上线应增加经过鉴权和限额的 FastAPI Tool 网关，并仅向其暴露审核过的固定动作。
- 生产实例不应从网络动态安装 Skill、读取第三方指令或执行模型产出的命令；要固定版本、审计许可证和所有文件来源。

## 强制门禁与已知局限

- 对原始客户工程库的任何技术结论，必须从经过授权的 PostgreSQL 当前/历史 SPEC_BODY 原文回查，并使用正确式样版本、哈希、页码；离线 JSON 样例本身不能证明数据库身份。
- 结构引用、RRF 分数、版本文本连续、BUG 候选都不能自动得到 CONFIRMED 或 PASS/FAIL。
- 当前证据 Skill 只可验证引用语法、数字/单位/版本字面。`ON 500ms / OFF 600ms` 条件误配等语义错判依然存在；应补基于 Requirement 信号、表格和人工 Golden 的 Claim→Requirement→Evidence 语义评测。
- 未执行客户真实 21MM、24MM、508/550、QA/BUG 的现场端到端工作流，未完成 Windows GUI / Open WebUI 技能自动调用或生产流量性能签收。
- 升级 ZIP 不自动变更 PostgreSQL、Qdrant、人工审核状态或防火墙配置。

**生产验收：NO-GO。建议在明确权限模型后逐个把已验证的技能接入 Stage 5 的结构化工作流。**
