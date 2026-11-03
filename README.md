# DocumentIntelligenceConsole

企业式样书、QA/BUG与内容关系的本地检索分析工具。当前源码整理日期：2026-11-03，基于v1.54及后续修复。

## 核心流程

`企业文档 → 结构化JSON → PostgreSQL → Embedding → Qdrant → Backend → Open WebUI`

- **Tool**：连接配置、工程建库/升级、文档入库、SQL/Qdrant查看、增量同步、版本对比与关系管理。
- **Backend**：理解自然语言及对话对象，查询式样和票据，检索正文并由本地模型分析回答。
- **Open WebUI**：用户聊天入口，连接本地OpenAI兼容接口，模型名称为`document-intelligence-rag`。
- **位置追溯**：支持章节、内容ID、已知页码及部分表格位置，不虚构缺失的页数。
- **Skills**：提供式样提取、版本、关系、检索和QA/BUG分析的受控工具与配置。

## Windows启动

建议Python 3.12，准备PostgreSQL、Qdrant和Ollama。向量模型及聊天模型需要提前下载；运行以本地服务和本地模型为基础。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
```

在Tool中配置连接，选择工程并执行建库/升级，再导入文档。确认数据库登记情况后执行SQL到Qdrant的同步。日常更新使用增量同步；完整重建会重新处理索引。

单独启动Backend：

```powershell
.\start_backend.ps1
```

Backend默认地址为`http://127.0.0.1:18080`。Open WebUI连接地址为`http://127.0.0.1:18080/v1`，鉴权使用Tool生成的本地API令牌。Docker中的Open WebUI需使用其可访问的宿主机地址，不能把容器内127.0.0.1视为宿主机。

数据库密码、本地令牌和个人配置保留在运行电脑。配置模板为`config/config.yaml`，可通过`CONFIG_FILE`指定配置文件。

## 当前回答行为

- 清单、版本和明确引用来自数据库事实；流程解释、分析和总结由Ollama模型生成。
- 追问继承最近用户明确选定的式样、工程等对象。长聊天不再因超过40条消息直接拒绝；模型上下文仍有token预算。
- 流程问题由模型选择小节，并补充Flow正文。证据包含标题、正文和实际位置。
- 正文超过上下文时分批读取，保留来源编号，不按固定字符数截去正文尾部。
- 版本对比使用与Tool共用的差异记录。明确引用、目标内容候选和人工确认关系需要区分。

## 目录

| 路径 | 内容 |
| --- | --- |
| `app/` | Tool、Backend、解析、存储、检索、关系及Skills |
| `database/` | 建库及升级SQL；执行前核对目标并备份 |
| `config/` | 默认配置、分类及关系规则 |
| `scripts/` | 诊断、校验、评估和运维脚本 |
| `tests/` | 单元与回归测试 |
| `examples/` | 模板和标注为SYNTHETIC的合成样例 |
| `docs/` | 功能和部署说明；历史记录不是当前生产验收 |

## 验证

```powershell
.\.venv\Scripts\python.exe scripts\verify_runtime_package.py
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests/test_long_chat_requests.py tests/test_source_batch_reading.py tests/test_outline_presentation.py tests/test_version_followups.py tests/test_issue_model_answer.py -q
```

这些是针对性回归，不等于全仓库、全部格式或所有业务需求验收。真实服务验收需要独立配置环境及授权语料。见[本轮整理与验证](docs/RELEASE_20261103.md)。

## 边界

- 质量受模型能力、正文解析和检索覆盖影响；有引用不代表语义必然正确。
- 缺词、合并表格、图片和缺页码位置需核对原文件。
- 语义候选不能直接作为正式工程依赖或BUG判定依据。
- 逐章分析在CPU和小上下文下可能很慢，目前可能等章节汇总后一次返回。
- RAGFlow为可选风格/格式适配，不是原生RAGFlow的完全替代。见[适配说明](START_RAGFLOW_COMPAT.md)。
- 不附带企业正文、QA/BUG库、数据库备份、向量数据、模型权重或本机配置。

补充入口：[核心后台](START_CORE_BACKEND.md) · [打包说明](BUILD_EXE.md) · [历史变更](CHANGELOG.md)
