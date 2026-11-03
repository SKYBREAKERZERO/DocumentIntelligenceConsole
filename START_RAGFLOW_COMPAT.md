# 可选 RAGFlow 风格工作台

默认关闭，不改数据库。完整说明和兼容边界见 [docs/RAGFLOW_COMPAT_CN.md](docs/RAGFLOW_COMPAT_CN.md)。

完整解压后，在原项目 Python 环境中启动独立后端：

```powershell
$env:DIC_RAGFLOW_COMPAT = '1'
python -m app.server
```

打开 `http://127.0.0.1:<现有 API 端口>/compat/ragflow`，输入原本地 API 令牌。

包含知识库映射、检索测试、固定工作流 JSON 导入/验证/导出/运行、内存聊天。不是完整 RAGFlow，不支持 RAGFlow Canvas DSL，也不提供上传、解析和数据迁移。
