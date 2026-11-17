# Open WebUI 任务卡片

此补丁适用于 Open WebUI `v0.12.0`，基准提交为 `15818d88fd7bde1b73de33590f50d15d21e8d7c1`。

在干净的 Open WebUI 源码目录执行以下命令。将示例中的补丁路径换成此仓库中该文件的实际路径。

```sh
git apply --check /path/to/DocumentIntelligenceConsole/docs/open-webui-v0.12.0/task-cards.patch
git apply /path/to/DocumentIntelligenceConsole/docs/open-webui-v0.12.0/task-cards.patch
npm ci
npm run build
```

启动 DocumentIntelligenceConsole 的 Backend，在 Open WebUI 中添加可访问的 OpenAI 兼容连接。Backend 的默认地址为 `http://127.0.0.1:18080/v1`，令牌由本机生成。若 Open WebUI 在容器中运行，使用容器能够访问的宿主机地址。

| 卡片 | 后端入口 |
| --- | --- |
| QA／BUG 查询 | `document-intelligence-rag` |
| 式样查询 | `document-intelligence-spec-query` |
| 生成用例／探索观点 | `document-intelligence-test-design` |
| BUG 趋势分析 | `document-intelligence-bug-trends` |

式样查询默认使用 `dic-spec-query-4b:latest`，可通过 `ollama create dic-spec-query-4b:latest -f config/Modelfile.spec-query` 创建。测试设计默认使用本机 `qwen3:4b-instruct`，趋势统计读取当前可查询的 BUG 台账。

补丁保留 Open WebUI 品牌。随附上游 [LICENSE](LICENSE) 和 [LICENSE_HISTORY](LICENSE_HISTORY)。
