# DocumentIntelligenceConsole Enterprise v1.54：21MM 550 BT 连续对话修复

## 真实故障（2026-10-27）

- `查一下 21MM 的 Bluetooth Audio function spec v2.52.pdf` 曾经因选择了工程 Agent 导致 Ollama 回答后引用校验拒绝。
- `Bluetooth Audio function spec v2.52` 可以从 PostgreSQL 正确列出正文 PDF，`document_id=21MM:UNSPECIFIED:NAME:a349727f3f5156c17204@v2.52`，`version_id=3`。
- 目录可以读出 50 条，但用户紧接着说 `按章节总结一下内容` 时，上轮 PDF 上下文丢失。
- 连续输入 `总结一下` 后，通用模型流程触及两路并发上限，显示 `Local RAG is busy`。

## 本次修复

1. 显式版本 PDF 的「查一下／查询」等前缀问法走 `SPEC_LOOKUP`。
2. 只从最近的用户消息抽取明确出现过的完整版本式样名和 21MM/24MM 作用域，处理 `按章节总结一下内容`、`总结一下` 等追问；**不从模型回复采纳新身份或式样编号**。
3. 新增独立的只读 `SPEC_CHAPTER_CONTENT` 路由：直接查询真实 `document_id` 对应的 PostgreSQL `contents`，按章节返回至多 2 段有 `content_id` / 页码的原文节选。截断片段显式标记，不以局部材料声称完整要求或真正的技术总结。
4. 结构化查询绕过 Ollama Prompt 组装和生成校验（工程推理仍保留企业门禁）；独立限制为两个并发的 SQL 答案槽位，不受两个繁忙 Ollama 会话占用。
5. 550 编号仍绑定在 Cover Sheet v2.50 的 `specification_id=2`；正文 v2.52 的 `specification_id=3`、`spec_code=NULL` 未作任何数据库修复。读取正文不代表已经批准归并。

## Windows 升级与验收

1. 停止 v1.53 后端（释放 TCP 18080），保留 PostgreSQL/Qdrant/Ollama/Open WebUI。
2. 把本完整 ZIP 解压到 `C:\mission\Python`，切到 `DocumentIntelligenceConsole_Enterprise_v1.54_CN_FULL`。首次创建 `.venv` 并安装 `requirements.txt`、`requirements-api.txt`。
3. 使用 `start_backend.ps1` 启动新的服务，重新打开 Open WebUI 会话（确保先前被拒绝的模型回复不会误导新的问题）。
4. 运行：`& .\.venv\Scripts\python.exe .\scripts\verify_v154_openwebui_multiturn.py`。
5. 该脚本逐轮提交真实聊天历史，报告实际路由、原文片段数量、证据告警、响应耗时，不打印正文和口令。五项均 `accepted=true` 且 `summary=PASS` 才完成本次聊天直答验收。

## 测试与限制

- 在当前 Linux 离线环境中，180 项相关回归通过，0 失败；未执行完整全量回归，未连接 Windows 内部数据库。
- 50 条目录并不自动证明可读 50 章正文；需要现场查看 `SPEC_CHAPTER_CONTENT` 是否查到 `contents` 原文。
- 此版本提供**可核验的按章原文摘录，不提供覆盖全章全部条件/例外的自动语义归纳**。后者需要按章节遍历全文、可信引用与覆盖率校验，并在本地模型能力范围内实现。
- 仍禁止 Coversheet 代替正文；不修改 PostgreSQL/Qdrant 身份和索引，不进行任何新外网检索。
