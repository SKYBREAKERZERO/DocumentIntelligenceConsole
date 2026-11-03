# v1.32 · Stage 2 内容级关系图与双端原文审核工作台

## 1. 目标、基线与非目标

本次基于 **v1.31 CN FULL ZIP**，增加只读静态关系拓扑与源证据审核工作台。两端是**内容单元**（段落 / 要求）而不是文档标签、式样书目录或 Qdrant 相似文本。**没有**自动推断、自动通过/拒绝、覆盖数据库、写入 Qdrant，也没有对 GUI 进行直接修改。GitHub `main` 的可验证基线仍为 v1.30 commit `2a41f0c`；不要覆盖另一个代码谱系的本地 v2.4.22。

只在 PostgreSQL 当前工程项目内读取关系；由 `load_review_case` 逐条复验：同项目、当前版、版本主文档 `document_id`、双端 SPEC_BODY 优先规则、对应 `contents.content` 子串、Unicode 精确偏移与 SHA-256、Stage 1 完整回填 receipt 与审核状态的一致性。导出时不合格或 STALE 的候选必须被隔离，其原文**不能进入图谱**。

## 2. Windows PowerShell 实际操作

项目根目录下，Python 3.12 虚拟环境和 PostgreSQL 读权限已经由运维配置。使用**专用只读 SQL 角色**（以下 `graph_viewer` 是示例，实际账号由 DBA 分配），不要用通用应用账号和超级用户。数据库标识必须对应一个工程规格库，不可使用 `rag_qabug`。

```powershell
# 不要将密码写进命令行；默认从 POSTGRES_PASSWORD 获取，未设置则安全输入
# 只导出待审核候选。输出目录必须尚不存在；其父目录须已存在。
.\.venv\Scripts\python.exe .\scripts\export_content_relation_graph.py `
  --host 127.0.0.1 --database rag_21mm --project 21MM --user graph_viewer `
  --status REVIEW_REQUIRED --limit 50 `
  --output .\output\stage2_review_21mm_20261021

# 若需要诊断已确认/拒绝关系，显式指定需要的审核状态：
.\.venv\Scripts\python.exe .\scripts\export_content_relation_graph.py `
  --database rag_21mm --project 21MM --user graph_viewer `
  --status CONFIRMED --status REJECTED --status STALE --limit 100 `
  --output .\output\stage2_relation_audit_21mm_20261021

# 纯离线文件一致性校验，无数据库连接：
.\.venv\Scripts\python.exe .\scripts\export_content_relation_graph.py `
  --verify .\output\stage2_review_21mm_20261021

# 在 Windows 本机、经授权的受控环境下打开 HTML：
Start-Process .\output\stage2_review_21mm_20261021\index.html
```

目录包含：`index.html`（离线审查页面、内容单元 SVG 边、可折叠双端原文、核验/隔离明细）；`graph.json`（完整结构化关系与来源）；`manifest.json`（校验值和导出时间）。SVG 最多概览 40 条边，HTML 详情保留**所有已核验导出条目**（总样本最多 200 条）。每条边展示方向、relation_type、score、关联理由，原文、文件名、document_id、unit_key、content_id、章节、页码、偏移和 SHA-256。

**安全提醒：** 浏览器打开的是只读历史快照，**并不是**当前最新的审核状态；快照写入文件后，数据源可能更新或权限撤回。`CONFIRMED` 标记仅表示导出时的审核状态，不应代替实时 RAG 的审核/版本/SPEC_BODY 门禁。任何确认/拒绝/撤回均需另行使用 v1.31 的受控人工审核 CLI（及数据库授权），不能在本 HTML 上完成审批。

## 3. 安全/完整性

- 报告包含未经删节的工程规格正文，即使是 `REVIEW_REQUIRED` 也可能包含秘密内容。**只在获授权、受管控的终端离线保存**。请 DBA/IT 确保 Windows NTFS ACL 对目录实施最小权限控制；不要上传公网、在线 HTML 阅读器或外部生成式 AI。
- SQL 查询采用 `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY`，数据库 project_info 必须与 `--project` 匹配，否则拒绝生成；跨项目关系隔离。命令行默认不显示密码，文件写入拒绝覆盖旧目录。
- HTML 实体转义原文，静态 SVG 与浏览器 CSP 禁止外部资源和脚本；不会执行原文中的 HTML、JavaScript 或恶意标签。`--verify` 验证 manifest 校验值、JSON/HTML 严格一致性、每端原文哈希及偏移长度。
- SHA-256 **仅是本地自洽性校验**，不含受信任的发布者数字签名、第三方时间戳、外部不可变存储证明；拥有完整目录修改权限的攻击者可以重算三份文件。不能据此宣称不可抵赖审计。源内容与最新 PostgreSQL 状态仍需在线复验。
- 条目数受 200 上限约束。列表是**有界抽样**，可能有更多未展示候选，不能将 `0` 或 `200` 结果当作全库完整扫描。`STALE` 候选不会通过证据复验，其原文不会渲染。

## 4. 企业级验收门禁与未完成项

**已完成的本机检验**：无数据库离线回归；审查非法状态、错误项目、带脚本的原文转义、源跨度 SHA-256、隔离明细、文件被篡改、SQL 只读快照次序、项目数据库不匹配和不覆盖输出目录。

**未完成的现场验收**：真实 PostgreSQL 17 数据量/锁竞争/读角色 GRANT；真实 508→550 关联与物理 PDF 双端核验；主文档与 Coversheet 实际行数据检查；Windows 11 文件权限/浏览器/GUI；数据更新并发、性能 P95 与最小权限/审计角色隔离。无现场结果前不得标注“生产验收通过”。

**下一步建议**：用真实 PostgreSQL 只读角色在 v1.32 运行图谱导出和证据源对账，再设计独立受控审核写入接口（身份绑定、最小权限、审计不可变与事务竞争测试），最后才接入多用户 GUI 审核界面。
