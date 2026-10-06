from __future__ import annotations

import functools
import re
from typing import Any

# Runtime Simplified-Chinese UI dictionary. Internal protocol/database values are
# intentionally not translated unless they are pure presentation labels.
_ZH: dict[str, str] = {
    # Product / shell
    "Document Intelligence Console Enterprise": "文档智能控制台 企业版",
    "NAVIGATION": "导航",
    "AI WORKSPACE": "AI 工作台",
    "INGESTION": "数据导入",
    "SPECIFICATION": "规格管理",
    "DATA": "数据管理",
    "RAG & VECTOR": "RAG 与向量",
    "ADMINISTRATION": "系统管理",
    "Active Context": "当前上下文",
    "Quick Actions": "快捷操作",
    "Refresh": "刷新",
    "Processing Scope": "处理范围",
    "Processing Scope (required)": "处理范围（必选）",
    "Settings": "设置",
    "Ready": "就绪",
    "Not configured": "未配置",
    "Not selected": "未选择",
    "Not tested": "未测试",
    "Not connected": "未连接",
    "Loading…": "加载中…",
    "Status": "状态",
    "Connection": "连接",
    "Credential": "凭据",
    "Database Schema": "数据库结构",
    "User config file": "用户配置文件",
    "New Computer Setup": "新电脑初始化",

    # Navigation pages
    "Enterprise AI Agent": "企业 AI 智能体",
    "AI & RAG Configuration": "AI 与 RAG 配置",
    "AI Service Status": "AI 服务状态",
    "AI Query Audit": "AI 查询审计",
    "Document Import": "文档导入",
    "JSON Import": "JSON 导入",
    "JSON → PostgreSQL": "JSON → PostgreSQL",
    "Specification Catalog": "规格目录",
    "Version History": "版本历史",
    "Version Compare": "版本比较",
    "Change Center": "变更中心",
    "Engineering Relations": "工程关系",
    "Database Explorer": "数据库浏览器",
    "SQL Editor": "SQL 编辑器",
    "Visual Query": "可视化查询",
    "ER Diagram": "ER 关系图",
    "Document Explorer": "文档浏览器",
    "Embedding Monitor": "Embedding 监控",
    "SQL → Qdrant Sync": "SQL → Qdrant 同步",
    "Qdrant Browser": "Qdrant 浏览器",
    "Qdrant": "Qdrant",

    # Dashboard
    "Documents": "文档",
    "Specifications": "规格",
    "Versions": "版本",
    "Current Versions": "当前版本",
    "Recorded Changes": "已记录变更",
    "Unresolved Relations": "未解析关系",
    "Embeddings D / P / F": "Embedding 完成 / 待处理 / 失败",
    "Import Documents": "导入文档",
    "Browse Database": "浏览数据库",
    "Run SQL": "运行 SQL",

    # Import / processing
    "1. Input Files": "1. 输入文件",
    "＋  Add Files": "＋ 添加文件",
    "＋  Add Folder": "＋ 添加文件夹",
    "Select Document Folder": "选择文档文件夹",
    "Folder Import": "文件夹导入",
    "Remove": "移除",
    "Clear": "清空",
    "2. Output": "2. 输出",
    "Output Directory": "输出目录",
    "Browse": "浏览",
    "3. Processing Options": "3. 处理选项",
    "Output Targets": "输出目标",
    "Generate JSON": "生成 JSON",
    "Save to PostgreSQL": "保存到 PostgreSQL",
    "4. Actions": "4. 操作",
    "▶  Convert": "▶  开始处理",
    "Cancel": "取消",
    "Latest Specification Result": "最近规格处理结果",
    "Open Specification": "打开规格",
    "View Changes": "查看变更",
    "5. Processing Status": "5. 处理状态",
    "Execution Log": "执行日志",
    "1. JSON Files": "1. JSON 文件",
    "＋  Add JSON": "＋ 添加 JSON",
    "＋  Add JSON Folder": "＋ 添加 JSON 文件夹",
    "Select JSON Folder": "选择 JSON 文件夹",
    "JSON Folder Import": "JSON 文件夹导入",
    "2. Import Target": "2. 导入目标",
    "3. Import Behavior": "3. 导入行为",
    "What happens during import": "导入时的处理方式",
    "⇩  Import to PostgreSQL": "⇩  导入 PostgreSQL",
    "5. Import Status": "5. 导入状态",
    "Import Log": "导入日志",

    # Settings / DB init
    "PostgreSQL Settings": "PostgreSQL 设置",
    "1. Connection": "1. 连接",
    "Password": "密码",
    "2. Actions": "2. 操作",
    "✓  Test Connection": "✓  测试连接",
    "Save Settings": "保存设置",
    "Restore Defaults": "恢复默认值",
    "Clear Saved Password": "清除已保存密码",
    "▦  Initialize / Upgrade Enterprise Database": "▦  初始化 / 升级企业数据库",
    "Initialize / Upgrade Enterprise Database": "初始化 / 升级企业数据库",
    "3. Qdrant / Console": "3. Qdrant / 控制台",
    "Console Page Size": "控制台每页行数",
    "Remember Qdrant API key in OS credential store": "在操作系统凭据存储中记住 Qdrant API Key",
    "Open Qdrant Browser": "打开 Qdrant 浏览器",
    "PostgreSQL Connection": "PostgreSQL 连接",
    "PostgreSQL RAG Schema": "PostgreSQL RAG 结构",
    "PostgreSQL Enterprise Database": "PostgreSQL 企业数据库",
    "PostgreSQL Password": "PostgreSQL 密码",

    # Specification / version
    "Specification": "规格",
    "Search & Filters": "搜索与筛选",
    "Search": "搜索",
    "Type": "类型",
    "Region": "区域",
    "Reset": "重置",
    "Specification Masters": "规格主数据",
    "Selected Specification": "已选规格",
    "Active Specification": "当前规格",
    "Main Branch History": "主分支历史",
    "Compare": "比较",
    "Relations": "关系",
    "Changes": "变更",
    "Legend": "图例",
    "Highlight": "高亮",
    "Show": "显示",
    "Compare Versions": "比较版本",
    "From": "从",
    "To": "到",
    "Version Change Timeline": "版本变更时间线",
    "Change Details": "变更详情",
    "Before": "变更前",
    "After": "变更后",
    "Current versions only": "仅当前版本",
    "Reconcile Unresolved": "协调未解析关系",
    "View": "视图",
    "Relation": "关系",
    "Meaning": "含义",
    "Relations & Evidence": "关系与证据",
    "Relationship Graph": "关系图",
    "Manual Relation Editor": "手工关系编辑器",
    "Authoritative Relation Facts": "权威关系事实",
    "Relation Evidence Inspector": "关系证据检查器",
    "Chunk / Evidence": "Chunk / 证据",
    "Location": "位置",
    "Document": "文档",
    "Mapping": "映射",
    "Evidence Text": "证据文本",
    "Canonical Chunk": "权威 Chunk",
    "Create / Edit Manual Specification Relation": "创建 / 编辑手工规格关系",
    "Manual Relation Audit Registry": "手工关系审计登记",
    "Source specification": "源规格",
    "Target mode": "目标模式",
    "Existing target": "已有目标",
    "External Spec No.": "外部规格编号",
    "Title hint": "标题提示",
    "Locator type": "定位类型",
    "Target locator": "目标定位",
    "Evidence type": "证据类型",
    "Note": "备注",
    "Evidence / engineering decision": "证据 / 工程判断",
    "Add Relation": "新增关系",
    "Update Selected": "更新所选",
    "Delete Selected": "删除所选",
    "Root": "根节点",
    "Depth": "深度",
    "Direction": "方向",
    "Include unresolved": "包含未解析",
    "Audit candidates": "审计候选",
    "Zoom": "缩放",
    "Fit": "适应窗口",
    "Reset Layout": "重置布局",
    "Old Version": "旧版本",
    "New Version": "新版本",
    "RAG Query Policy": "RAG 查询策略",

    # AI workspace
    "Enterprise AI Workspace": "AI工作台",
    "Model": "模型",
    "Knowledge Scope": "知识范围",
    "＋  New conversation": "＋  新建对话",
    "RECENT CHATS": "最近对话",
    "Rename": "重命名",
    "Delete": "删除",
    "Send": "发送",
    "Stop": "停止",
    "Evidence Inspector": "证据检查器",
    "Source": "来源",
    "Scope": "范围",
    "Score": "评分",
    "Source detail": "来源详情",
    "Agent Trace / Audit": "智能体轨迹 / 审计",
    "Save configuration": "保存配置",
    "Ollama Runtime": "Ollama 运行时",
    "RAG Retrieval": "RAG 检索",
    "Test / Load Ollama models": "测试 / 加载 Ollama 模型",
    "Write metadata-only AI query audit": "仅记录元数据的 AI 查询审计",
    "Current-only Qdrant Collections": "仅当前版本 Qdrant Collection",
    "OpenAI-compatible Gateway (optional Open WebUI client)": "OpenAI 兼容网关（可选 Open WebUI 客户端）",
    "Enable gateway on application startup": "应用启动时启用网关",
    "Engineering System Prompt": "工程系统提示词",
    "Request": "请求",
    "Execution detail": "执行详情",
    "Run full health check": "执行完整健康检查",
    "OpenAI-compatible API / Open WebUI": "OpenAI 兼容 API / Open WebUI",
    "Start API": "启动 API",
    "Stop API": "停止 API",
    "Diagnostics": "诊断信息",
    "Rename conversation": "重命名对话",
    "Conversation name": "对话名称",
    "Save": "保存",
    "Delete conversation": "删除对话",
    "AI configuration": "AI 配置",
    "AI model": "AI 模型",
    "AI API": "AI API",
    "New conversation": "新建对话",
    "AI workspace ready": "AI 工作台已就绪",
    "No evidence loaded": "尚未加载证据",
    "Settings loaded": "设置已加载",
    "Stopped": "已停止",
    "Not checked": "未检查",

    # Data console
    "Enable table writes": "允许表写入",
    "＋ Add": "＋ 新增",
    "✎ Edit": "✎ 编辑",
    "− Delete": "− 删除",
    "⟳ Refresh": "⟳ 刷新",
    "Filter": "筛选",
    "Sort": "排序",
    "Apply": "应用",
    "Export CSV": "导出 CSV",
    " Data ": " 数据 ",
    " Structure ": " 结构 ",
    " Indexes ": " 索引 ",
    " Constraints ": " 约束 ",
    " DDL ": " DDL ",
    "Refresh DDL": "刷新 DDL",
    "Save DDL": "保存 DDL",
    "Previous": "上一页",
    "Next": "下一页",
    "▶ Run": "▶ 运行",
    "Run Selection": "运行所选",
    "Format": "格式化",
    "Open": "打开",
    "Enable write SQL": "允许写 SQL",
    "Chart": "图表",
    "Export Result": "导出结果",
    "Schema": "Schema",
    "Base table": "主表",
    "Join type": "连接类型",
    "Join table": "连接表",
    "Base join column": "主表连接列",
    "Join column": "连接列",
    "SELECT columns": "SELECT 列",
    "WHERE expression": "WHERE 条件",
    "Direction": "方向",
    "Generate SQL": "生成 SQL",
    "Open in SQL Editor": "在 SQL 编辑器中打开",
    "Generated SQL": "生成的 SQL",
    "Result": "结果",
    "Load / Refresh ERD": "加载 / 刷新 ERD",
    "Refresh Documents": "刷新文档",
    "Refresh Collections": "刷新 Collection",
    "Points / Payloads": "Points / Payload",
    "Refresh Summary": "刷新汇总",
    "Load Rows": "加载记录",
    "Qdrant Governance & Data Quality": "Qdrant 治理与数据质量",
    "Validate Data Quality": "验证数据质量",
    "Repair Payload Indexes": "修复 Payload 索引",
    "Sync History": "同步历史",
    "Operational Log": "运行日志",
    "Qdrant Data Quality": "Qdrant 数据质量",
    "Qdrant Payload Indexes": "Qdrant Payload 索引",
    "Full Rebuild Current Qdrant Index": "完整重建当前 Qdrant 索引",
    "Data Quality: not validated": "数据质量：尚未验证",
    "Preview the current-only source before syncing.": "同步前请先预览仅当前版本的数据源。",
    "Idle": "空闲",

    # Dialogs / common messages
    "Success": "成功",
    "Warning": "警告",
    "Error": "错误",
    "Completed": "已完成",
    "Completed with Errors": "完成，但存在错误",
    "Processing Cancelled": "处理已取消",
    "Batch processing was cancelled.": "批处理已取消。",
    "No Input Files": "没有输入文件",
    "Please select at least one document.": "请至少选择一个文档。",
    "No JSON Files": "没有 JSON 文件",
    "Please select at least one JSON file.": "请至少选择一个 JSON 文件。",
    "No Output Selected": "未选择输出",
    "Enable at least one processing output.": "请至少启用一种处理输出。",
    "Please select an output directory.": "请选择输出目录。",
    "Host cannot be empty.": "主机地址不能为空。",
    "Database cannot be empty.": "数据库名不能为空。",
    "User cannot be empty.": "用户名不能为空。",
    "Port must be between 1 and 65535.": "端口必须在 1 到 65535 之间。",
    "Connect timeout must be greater than 0.": "连接超时必须大于 0。",
    "Port must be an integer.": "端口必须是整数。",
    "Connect timeout must be an integer.": "连接超时必须是整数。",
    "Qdrant timeout must be a positive number.": "Qdrant 超时必须为正数。",
    "Console page size must be an integer between 10 and 1000.": "控制台每页行数必须是 10 到 1000 之间的整数。",
    "Delete this AI conversation from local history?": "确定从本地历史中删除该 AI 对话吗？",
    "No Ollama model is selected. Start Ollama, load a model, then refresh the model list.": "尚未选择 Ollama 模型。请启动 Ollama、加载模型，然后刷新模型列表。",
    "AI/RAG configuration saved.": "AI/RAG 配置已保存。",
    "Only base tables can be modified.": "只有基础表可以修改。",
    "Select exactly one row.": "请只选择一条记录。",
    "Safe editing requires a primary key.": "安全编辑要求表具有主键。",
    "Select one or more rows.": "请选择一条或多条记录。",
    "Safe deletion requires a primary key.": "安全删除要求表具有主键。",
    "No table data is loaded.": "尚未加载表数据。",
    "No SQL result is available.": "当前没有可用的 SQL 结果。",
    "Select a schema and base table.": "请选择 Schema 和主表。",
    "Choose both join columns.": "请选择两侧连接列。",
    "Run the visual query first.": "请先运行可视化查询。",
    "Select a specification first.": "请先选择一个规格。",
    "Select a specification and two versions first.": "请先选择一个规格和两个版本。",
    "From must be older than To.": "“从”版本必须早于“到”版本。",
    "Refresh the version history and select valid versions.": "请刷新版本历史并选择有效版本。",
    "Select a manual relation to update.": "请选择要更新的手工关系。",
    "Select a manual relation to delete.": "请选择要删除的手工关系。",
    "Delete Manual Relation": "删除手工关系",
    "Manual Relation": "手工关系",
    "Relation Evidence": "关系证据",
    "Exit Application": "退出应用",
    "Document processing is still running.\n\nExit anyway?": "文档处理仍在运行。\n\n仍要退出吗？",
    "Run health check to validate the complete AI stack.": "执行健康检查以验证完整 AI 技术栈。",
    "Ollama Base URL": "Ollama 服务地址",
    "Default Chat Model": "默认聊天模型",
    "Temperature": "温度参数",
    "Context Window (num_ctx)": "上下文窗口（num_ctx）",
    "Candidate Top K": "候选 Top K",
    "Final governed evidence": "最终治理后证据数",
    "Minimum semantic score": "最低语义评分",
    "Maximum context characters": "最大上下文字符数",
    "Planner history turns": "规划器历史轮数",
    "Embedding Model": "Embedding 模型",
    "Vector Dimension": "向量维度",
    "Listen Host": "监听主机",
    "Port": "端口",
    "API Key": "API Key",
    "UTC Time": "UTC 时间",
    "Evidence": "证据",
    "Total ms": "总耗时 ms",
    "Open WebUI Base URL: http://<host>:<port>/v1": "Open WebUI 基础地址：http://<host>:<port>/v1",
    "Endpoints: GET /health · GET /v1/models · POST /v1/chat/completions": "接口：GET /health · GET /v1/models · POST /v1/chat/completions",
    "Source specification": "源规格",
    "Qdrant URL": "Qdrant 地址",
    "Timeout (sec)": "超时（秒）",
    "Enabled only after a successful connection test. Non-destructive: upgrades public RAG schema + app_config control plane; existing business data, settings and vectors are preserved.": "仅在连接测试成功后启用。此操作为非破坏式升级：升级 public RAG 结构与 app_config 控制面，并保留现有业务数据、用户设置和向量。",
    "Password available for this session": "当前会话已提供密码",
    "Password is not configured": "尚未配置密码",
    "RAG schema not verified": "RAG 结构尚未验证",
    "Qdrant: not tested": "Qdrant：尚未测试",
    "Select a project to determine the target database.": "请选择项目以确定目标数据库。",
    "No file selected.": "尚未选择文件。",
    "No specification selected": "尚未选择规格",
    "Select two versions to compare.": "请选择两个版本进行比较。",
    "Select a specification to review its change timeline.": "请选择一个规格以查看其变更时间线。",
    "No change set selected": "尚未选择变更集",
    "Select a relation to inspect source evidence.": "请选择一条关系以查看来源证据。",
    "Load relations to render the graph.": "加载关系后渲染关系图。",
    "Select a table": "请选择表",
    "No query executed.": "尚未执行查询。",
    "Select a chunk.": "请选择 Chunk。",
    "PostgreSQL: not tested": "PostgreSQL：尚未测试",
    "SQL Current 0": "SQL 当前 0",
    "Qdrant 0": "Qdrant 0",
    "New 0": "新增 0",
    "Changed 0": "变更 0",
    "Unchanged 0": "未变更 0",
    "Stale 0": "过期 0",
    "Ctrl+Enter / F5 = Run · Ctrl+S = Save": "Ctrl+Enter / F5 = 运行 · Ctrl+S = 保存",
    "Refresh Summary": "刷新汇总",
    "Page 1": "第 1 页",
    "Confirm INSERT": "确认 INSERT",
    "Confirm UPDATE": "确认 UPDATE",
    "Confirm DELETE": "确认 DELETE",
    "Confirm SQL Write": "确认 SQL 写入",
    "SQL Write Blocked": "SQL 写入已阻止",
    "Format SQL": "格式化 SQL",
    "Full Rebuild Current Qdrant Index": "完整重建当前 Qdrant 索引",
    "Console UI Error": "控制台界面错误",
    "Edit selected row": "编辑所选记录",
    "Delete selected row(s)": "删除所选记录",
    "Copy selected row": "复制所选记录",
    "1. Specification Catalog": "1. 规格目录",
    "2. Version History": "2. 版本历史",
    "3. Compare Versions": "3. 版本比较",
    "AI AUTO · Agentic RAG": "AI AUTO · Agentic RAG",
    "Batch": "批次",
    "Collection": "Collection",
    "Current-only indexing policy": "仅当前版本索引策略",
    "Dimension": "维度",
    "Document conversion · Structured JSON · PostgreSQL storage": "文档转换 · 结构化 JSON · PostgreSQL 存储",
    "ERD": "ER 图",
    "Embedding model": "Embedding 模型",
    "Full Rebuild": "完整重建",
    "Index configuration": "索引配置",
    "Low-saturation colors for comfortable long reading.": "低饱和度配色，适合长时间阅读。",
    "Open Change Center": "打开变更中心",
    "Preview Sync": "预览同步",
    "Refresh Context": "刷新上下文",
    "Remember on this computer": "在此电脑上记住",
    "Remember password on this computer (Windows user environment)": "在此电脑上记住密码（Windows 用户环境）",
    "Run": "运行",
    "SQL → Qdrant Current Index": "SQL → Qdrant 当前索引",
    "Scope Default": "默认范围",
    "Sync Now": "立即同步",
    "↻  Refresh": "↻  刷新",
    "Cancel stops after the current document finishes.": "取消将在当前文档处理完成后停止。",
    "Business versions are sorted semantically, not by import sequence. Late imports are spliced into the history chain automatically.": "业务版本按语义版本顺序排序，而不是按导入顺序；后补导入的历史版本会自动插入正确的版本链位置。",
    "Default retrieval uses rag_current_chunks (Current only). Historical versions are retained for explicit lookup, comparison and audit.": "默认检索使用 rag_current_chunks（仅当前版本）。历史版本保留用于显式查询、比较和审计。",
    "Default retrieval uses rag_current_chunks (Current only). History is retained for explicit version lookup and comparison.": "默认检索使用 rag_current_chunks（仅当前版本）。历史数据保留用于显式版本查询和比较。",
    "Historical chunks are intentionally excluded from this collection. Version-history QA can use a separate history index later.": "此 Collection 有意排除历史 Chunk；版本历史问答后续可使用独立历史索引。",
    "Import does not parse the original Office/PDF file again.": "导入时不会再次解析原始 Office/PDF 文件。",
    "Incremental sync embeds only New + Changed and removes known Stale points.": "增量同步只对新增和变更数据生成向量，并删除已知的过期 Point。",
    "Only JSON generated by Document Intelligence Console Enterprise": "仅支持由文档智能控制台企业版生成的 JSON",
    "PostgreSQL is the source of truth. Only public.rag_current_chunks is indexed for normal QA; historical versions remain in SQL.": "PostgreSQL 是唯一权威数据源。普通问答仅索引 public.rag_current_chunks；历史版本继续保留在 SQL 中。",
    "PostgreSQL output is enabled.\nEnter the PostgreSQL password.": "已启用 PostgreSQL 输出。\n请输入 PostgreSQL 密码。",
    "PostgreSQL password is requested only when database output is enabled. In JSON-only mode, files are automatically separated into output/21MM, output/24MM, or output/COMMON.": "仅在启用数据库输出时请求 PostgreSQL 密码。仅生成 JSON 时，文件会自动分流到 output/21MM、output/24MM 或 output/COMMON。",
    "Specification → Version → Document → Chapter → Section → Chunk": "规格 → 版本 → 文档 → 章节 → 小节 → Chunk",
    "Supported: PDF / DOCX / PPTX / PPT / XLSX / TXT / PNG / JPG": "支持：PDF / DOCX / PPTX / PPT / XLSX / TXT / PNG / JPG",
    "Target is controlled only by PostgreSQL Settings. JSON project_code is validated before any file is imported.": "导入目标仅由 PostgreSQL 设置控制；导入任何文件前都会验证 JSON 的 project_code。",
    "The distributed application can still be a single EXE. This page creates only a per-user runtime configuration under LocalAppData.": "发布后的应用仍可保持单一 EXE；此页面只会在 LocalAppData 下创建当前用户的运行时配置。",
    "The password is never written to config.yaml or application logs.": "密码绝不会写入 config.yaml 或应用日志。",
    "version_seq is audit/import order. Version display/order uses version_sort_key. Late historical imports are spliced into the semantic chain without downgrading Current.": "version_seq 仅表示审计/导入顺序；版本显示与排序使用 version_sort_key。后补导入的历史版本会插入正确语义链，不会降低 Current。",
}

# Longer presentation-only sentences.
_ZH.update({
    "The Processing Scope above is shared by ingestion, version management, database tools and RAG administration.": "上方的处理范围由文档导入、版本管理、数据库工具和 RAG 管理共同使用。",
    "Current is the default RAG target. Historical versions remain immutable and queryable explicitly.": "当前版本是默认 RAG 目标；历史版本保持不可变，并可按需显式查询。",
    "Manual relations are stored separately from extractor output and remain auditable.": "手工关系与自动抽取结果分开保存，并始终可审计。",
    "Edit/Delete require a primary key. Views are read-only.": "编辑/删除要求表具有主键；视图为只读。",
    "Double-click a table to open its Data tab.": "双击表可打开其“数据”页签。",
    "AI AUTO is the only user-facing retrieval mode. Ollama plans safe read-only retrieval automatically; PostgreSQL/Qdrant execution stays inside the application. Chat and embedding models remain separated; existing 1024-dimensional Qdrant indexes are protected.": "AI AUTO 是唯一面向用户的检索模式。Ollama 自动规划安全的只读检索，PostgreSQL/Qdrant 的实际执行始终由本应用控制。聊天模型与 Embedding 模型严格分离，并保护现有 1024 维 Qdrant 索引。",
    "Metadata-only operational audit. Prompts and answers are not duplicated here; the audit records planner source, tools, scopes, evidence identifiers, timings and execution status.": "仅记录元数据的运行审计。这里不会重复保存提示词和回答；审计记录规划器来源、调用工具、范围、证据标识、耗时和执行状态。",
    "Enterprise Agentic RAG is ready. Select an Ollama model and ask an engineering question. AI AUTO will plan and execute safe retrieval automatically.\n": "企业级 Agentic RAG 已就绪。请选择 Ollama 模型并提出工程问题，AI AUTO 将自动规划并执行安全检索。\n",
    "This will delete and recreate the selected Qdrant collection, then re-embed every current SQL chunk.\n\nHistorical SQL data is not deleted.\n\nContinue?": "此操作将删除并重新创建所选 Qdrant Collection，并重新向量化所有当前 SQL Chunk。\n\n历史 SQL 数据不会被删除。\n\n是否继续？",
})


def zh(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text
    exact = _ZH.get(text)
    if exact is not None:
        return exact
    # Translate a few safe presentation patterns without touching IDs/data.
    m = re.fullmatch(r"Page\s+(\d+)", text)
    if m:
        return f"第 {m.group(1)} 页"
    m = re.fullmatch(r"(\d+) files selected", text)
    if m:
        return f"已选择 {m.group(1)} 个文件"
    m = re.fullmatch(r"(\d+) JSON files selected", text)
    if m:
        return f"已选择 {m.group(1)} 个 JSON 文件"
    m = re.fullmatch(r"(\d+) specifications", text)
    if m:
        return f"{m.group(1)} 个规格"
    return text


def _translate_args_kwargs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if "text" in kwargs:
        kwargs = dict(kwargs)
        kwargs["text"] = zh(kwargs["text"])
    return args, kwargs


def install_zh_cn_ui(ttk_module: Any, tk_module: Any, messagebox_module: Any) -> None:
    """Install a conservative Simplified-Chinese presentation layer once.

    Only presentation text is translated. Database/API values and arbitrary
    runtime data are left untouched unless they exactly match a known UI label.
    """
    if getattr(ttk_module, "_dic_zh_cn_installed", False):
        return
    ttk_module._dic_zh_cn_installed = True

    # ttkbootstrap widget constructors
    for name in ("Label", "Button", "LabelFrame", "Labelframe", "Checkbutton", "Radiobutton", "Menubutton"):
        cls = getattr(ttk_module, name, None)
        if cls is None or getattr(cls, "_dic_zh_patched", False):
            continue
        original = cls.__init__

        @functools.wraps(original)
        def init(self, *args, __original=original, **kwargs):
            args, kwargs = _translate_args_kwargs(args, kwargs)
            return __original(self, *args, **kwargs)

        cls.__init__ = init
        cls._dic_zh_patched = True

    # Native Tk widgets occasionally used for dialogs.
    for name in ("Label", "Button", "Checkbutton", "Radiobutton", "LabelFrame"):
        cls = getattr(tk_module, name, None)
        if cls is None or getattr(cls, "_dic_zh_patched", False):
            continue
        original = cls.__init__

        @functools.wraps(original)
        def init(self, *args, __original=original, **kwargs):
            args, kwargs = _translate_args_kwargs(args, kwargs)
            return __original(self, *args, **kwargs)

        cls.__init__ = init
        cls._dic_zh_patched = True

    # Tree headings and tree items.
    tree_cls = getattr(ttk_module, "Treeview", None)
    if tree_cls is not None and not getattr(tree_cls, "_dic_zh_patched", False):
        orig_heading = tree_cls.heading
        orig_insert = tree_cls.insert

        @functools.wraps(orig_heading)
        def heading(self, column, option=None, **kw):
            if "text" in kw:
                kw["text"] = zh(kw["text"])
            return orig_heading(self, column, option, **kw)

        @functools.wraps(orig_insert)
        def insert(self, parent, index, iid=None, **kw):
            if "text" in kw:
                kw["text"] = zh(kw["text"])
            if iid is None:
                return orig_insert(self, parent, index, **kw)
            return orig_insert(self, parent, index, iid=iid, **kw)

        tree_cls.heading = heading
        tree_cls.insert = insert
        tree_cls._dic_zh_patched = True

    # Notebook tab labels.
    notebook_cls = getattr(ttk_module, "Notebook", None)
    if notebook_cls is not None and not getattr(notebook_cls, "_dic_zh_patched", False):
        orig_add = notebook_cls.add

        @functools.wraps(orig_add)
        def add(self, child, **kw):
            if "text" in kw:
                kw["text"] = zh(kw["text"])
            return orig_add(self, child, **kw)

        notebook_cls.add = add
        notebook_cls._dic_zh_patched = True

    # Message boxes.
    for name in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel", "askretrycancel"):
        original = getattr(messagebox_module, name, None)
        if original is None or getattr(original, "_dic_zh_patched", False):
            continue

        @functools.wraps(original)
        def wrapper(title, message=None, *args, __original=original, **kwargs):
            return __original(zh(title), zh(message), *args, **kwargs)

        wrapper._dic_zh_patched = True
        setattr(messagebox_module, name, wrapper)

    # Native context menus.
    menu_cls = getattr(tk_module, "Menu", None)
    if menu_cls is not None and not getattr(menu_cls, "_dic_zh_patched", False):
        for method_name in ("add_command", "add_cascade", "add_checkbutton", "add_radiobutton"):
            original = getattr(menu_cls, method_name, None)
            if original is None:
                continue

            def menu_method(self, *args, __original=original, **kwargs):
                if "label" in kwargs:
                    kwargs["label"] = zh(kwargs["label"])
                return __original(self, *args, **kwargs)

            setattr(menu_cls, method_name, menu_method)
        menu_cls._dic_zh_patched = True

    # Window titles.
    for cls_name in ("Tk", "Toplevel"):
        cls = getattr(tk_module, cls_name, None)
        if cls is None or getattr(cls, "_dic_title_patched", False):
            continue
        original = cls.title

        @functools.wraps(original)
        def title(self, string=None, __original=original):
            if string is None:
                return __original(self)
            return __original(self, zh(string))

        cls.title = title
        cls._dic_title_patched = True
