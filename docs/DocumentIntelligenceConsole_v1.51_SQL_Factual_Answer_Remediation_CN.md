# v1.51 — 企业 RAG 式样书事实检索修复与验收

## 原始生产缺陷

用户查询“Bluetooth Audio function spec v2.52有吗”“550 BT 按目录总结”“有哪些 21MM 式样书”“查已登记相邻本体版本变化”时被路由至 CPU 本地 4B Agent，随后因为引用无效被阻断，或模型错误声称内部知识不存在/需外部求证。

## 本次改动

- `SPEC_LOOKUP`：根据显式式样编号、标题及真实文件名直接查 PostgreSQL；用户提供 v2.52 时严格匹配本体版本；BT、蓝牙、Bluetooth 只用于候选发现，不预设与 550 同一编号。
- `SPEC_CATALOG`：项目维度直接读规格注册表与本体版本关联；封面不能被当作“当前技术本体”；显示登记而缺乏可信本体的条目。
- `SPEC_OUTLINE`：直接读取可信本体所对应 document_id 的 `chapters/sections`，无记录时谨慎降级到正文索引的 section_id；没有技术条款全文时仅能展示已登记的章节事实，不编造章节摘要。
- `SPEC_VERSION_FACTS`：优先查询 MAIN 分支已登记的相邻两个 SPEC_BODY 版本；严格要求 READY 且 is_adjacent 的持久化差异集，展示数据库保存的新旧文本和版本 ID；没有两版或没有 READY diff 时明确说明，关系影响保持 UNDETERMINED。
- `documents` 已导入但 `specification_versions` 尚无本体版本关联时：单独标记“文件已导入但身份/版本关系未核验”。
- `SPEC_*` 事实回答通过确定性 SQL 结果渲染，不调用 Ollama/本地 CrossEncoder 或联网服务，也不尝试绕过其他工程推理路径的证据发布门禁。
- 修复 `app/server/runtime.py` 已现场复现的 `connect_timeout` 参数重复传递（四域 PostgreSQL 健康检查误报）。

## 安全与限制

生产部署为 INTERNAL_ONLY；本次代码不创建或修改数据表、不写 PostgreSQL/Qdrant、不批准任何人工关系、不在不完整版本上编造工程影响。`BT=Bluetooth` 是搜索词扩展，不证明具体式样编号。PDF 在 Windows 文件夹存在不证明它已导入并绑定到可信版本。目录结构的只读列举不是技术内容的语义总结。保存的差异文本未重新执行原始 PDF 全文哈希核验；仍需真正生产审核。

## Windows 现场验收步骤

1. 备份现有 v1.50 的用户配置文件与工作目录。将完整 v1.51 ZIP 解压到**新目录**，不要覆盖在旧 v1.50 文件夹上。
2. 确认 `.venv` 的 Python/依赖，先通过 GUI 或原 PowerShell `Ctrl+C` 停止旧 `18080` 后端，验证端口已释放，启动新 `start_backend.ps1`。避免再出现 WinError 10048。
3. 在新目录运行：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_v151_spec_facts.ps1`。
4. 核对 21MM `rag_21mm` 的真实 SPEC_BODY 条目、v2.52 文件名、版本 ID、章节记录及变更集。观察四个请求的 `rag.route` 和耗时，不把空数据判断成输入错误。
5. 正文 PDF 的页码、技术要求语义完整性、508→550 已确认工程依赖、真实 PostgreSQL/Qdrant 大规模召回、客户 Golden 准确率均未在交付环境完成验收，生产仍为 NO-GO。

## 失败处理

- Route 不匹配：检查实际监听 18080 的 Backend 进程是否为 v1.51 路径。
- `SQL` 查询成功但无数据：查看真实版本链接、project_code、document_role 及 chapters/sections 导入状态。**禁止**用 Coversheet 或联网搜索替代。
- 版本差异无 READY 记录：不能将“未找到差异”表述为“两版没有变化”。

## 回归

- pytest 1232 passed、5 skipped、0 failed（离线）；最终 ZIP SHA-256 清单已复核。
- 生产 PostgreSQL、原始客户 PDF、Windows GUI 和 Golden Dataset 尚未在当前环境真实执行。
