# 核心后台版：从这里开始

**当前交付入口：[本次完整包、五项需求状态与限制](START_HERE_CONDITIONAL_CORE_CN.md)。** 新增分页同步/校验、取消续跑和经审核的有限条件判定；下文链接中的旧测试数量与缺口描述属于各自历史快照。

最新增补：[假式样清理与典型场景合成测试](docs/CLEANUP_AND_SYNTHETIC_SUITE_CN.md)。已按用户指示清理 1001/2007，新增四份明确标识的 990xxx 测试 PDF、JSON 与 QA/BUG 样本；测试资料只进入隔离库。

本轮新增隔离 SQL/向量验收、精确索引核账及起票前固定条件流程。当前完成范围与生产缺口请先阅读
[本轮交付及复现说明](docs/WORKFLOW_DELIVERY_2026-10-28_CN.md)。生产迁移和缺少向量的补齐没有自动执行；合成样本通过不代表五项业务需求全部生产签收。

完整解压 ZIP，使用原项目 Python 环境执行 `run.ps1` 或：

```powershell
python -m app.gui.application
```

默认进入“企业知识后台”。管理员按以下顺序操作：

1. 选择顶部处理范围：21MM、24MM、Common 或 QA/BUG。
2. 打开“配置 / 建库 / Wi-Fi”，保存服务参数、测试连接，使用原有初始化按钮建库建表；已有数据不重置。
3. 在模型配置中选择已在本地缓存的向量模型，确保维度与集合匹配。模型缺失时会报错，不自动联网下载。
4. 添加文件，检查文件列表和 JSON 输出目录，点击“一键入库并校验”。
5. 通过“SQL 数据库”“Qdrant 库”“版本对比”“工程关系”检查结果。
6. 用户使用 Open WebUI，连接独立 Backend 的 `/v1` 接口并选择 `document-intelligence-rag`。

保留原本的普通转换入口，可以只导出 JSON。一键完整入库会同时启用 JSON 和 SQL，再同步向量；每次 JSON 输出放在 `core_runs/<运行ID>` 下。

详细说明、恢复规则和验收边界见 [docs/CORE_BACKEND_CN.md](docs/CORE_BACKEND_CN.md)。旧 RAGFlow 风格页面仍为默认关闭的可选兼容功能，不是主要用户前端。
