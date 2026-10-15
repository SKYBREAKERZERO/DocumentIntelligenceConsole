# DocumentIntelligenceConsole v1.08 — 运维、恢复和安全验收手册

## 1. 可用性等级与承诺边界

本版实现 **单节点韧性 (single-node resilience)**，而非真正的双机/多机高可用：

- Windows 当前用户**已登录**的情况下，独立 Watchdog 检查并重启停止的 Backend；不会终止未知端口监听者。
- 数据库、Qdrant、Ollama、Docker Desktop、GPU 或主机自身停止时，**不提供冗余实例自动故障切换**；数据恢复能力取决于备份是否真正执行、能否读取、是否完成恢复演练。
- 对外宣称 99.9% 之类 SLA、RTO、RPO 前，必须有冗余设备、独立备份位置、恢复流程和实测数据；本次不做未经验证的可用性保证。
- Windows 登录自动运行不是 Windows Service；无人登录或用户注销后不会继续驻留。若需 7×24 服务，应使用专用服务账号、受控 Windows Service/Task Scheduler、冗余硬件和负载切换；需要独立部署与安全审核。

## 2. 初次启动

保留 v1.07/v1.05 目录、配置、API Key、PostgreSQL 和 `qdrant_storage`，将新包解压到 `...v1.08_CN_FULL`。仅允许一个版本占用 18080。先停旧版 Backend（确认 PID/启动路径，不要对端口上的未知进程强制结束），再在新目录启动 GUI。GUI 将按现有策略异步启动 Backend。

```powershell
cd 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.08_CN_FULL'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

## 3. 看护后台的启用、检查、撤销

在 **Settings → 外部 UI / Open WebUI → 后台进程与 Windows 登录自启动** 中启用 **后台独立看护**，与原 GUI 登录自启开关相互独立。首次启用后需数秒才能看到心跳。该功能在用户登录状态下即使关闭 GUI 也继续运行；重启系统后需该用户再次登录。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage_backend_watchdog.ps1 -Action Enable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage_backend_watchdog.ps1 -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage_backend_watchdog.ps1 -Action Disable
```

注意：Disable 不会中断当前正在回答的 Backend，只会撤销后台看护与自启登记。一个用户配置目录只允许一个 Watchdog 持有锁。旧的 v1.08 看护运行时先关闭它，再切到新安装目录。

看护日志：`%LOCALAPPDATA%\DocumentIntelligenceConsole\logs\backend_watchdog.log`（轮换 2 MB × 3 个备份）；Backend 原始启动日志：`backend_startup.log`。不要把包含运行信息的日志公开上传或绑定到 WebUI 静态目录。

恢复规则：15 秒轮询；确认 `STOPPED` 后自动尝试启动；10 分钟最多 5 次启动。若显示 `PORT_OCCUPIED_UNVERIFIED`，**不会**杀死陌生进程。`RESTART_CIRCUIT_OPEN` 表示频繁崩溃，应排查配置、模型缓存、磁盘和 Python Traceback；等待窗口结束后才继续尝试。

## 4. 模型、并发与可观察性

本地 OpenAI-compatible 模型仍是 `document-intelligence-rag`，仅转发到经过现有 Evidence 控制的 RAG 服务。所有调用必须通过 Bearer Token。

```powershell
$token=(Get-Content "$env:LOCALAPPDATA\DocumentIntelligenceConsole\local_api_token.txt" -Raw -Encoding UTF8).Trim()
$h=@{Authorization=('Bearer ' + $token)}
Invoke-RestMethod -Uri 'http://127.0.0.1:18080/api/v1/runtime/metrics' -Headers $h
Invoke-RestMethod -Uri 'http://127.0.0.1:18080/api/v1/health/dependencies' -Headers $h
```

默认最多 2 个同时生成的聊天；第 3 个立即收到 HTTP 429 和 `Retry-After: 3`，这保护单台电脑免受无界 CPU/GPU 并发耗尽。需要调整时先执行负载测试，再在**Backend 启动环境**中设置 `DIC_MAX_ACTIVE_CHATS=1..8` 并重启 Backend。看护由 Windows 登录环境启动，更新环境变量后应重新登录才可确保生效。

`/api/v1/health/ready` 仅反映配置/Embedding 预热准备情况；并不验证 SQL/Qdrant/Ollama 在线，必须联合 `/api/v1/health/dependencies` 和真实检索进行业务验收。`/v1/models` 用于发现模型 ID，不可当成端到端健康证明。

## 5. 权威 SQL 数据备份（必须先预演）

只在本机运行，目标目录必须是 ACL 限制、加密、**不经 WebUI 暴露**的安全存储；不要把公司式样书/QA/BUG 数据直接同步到未经授权的云盘。确认实际数据库名称是否与下列默认一致；不要备份演示/错误数据库。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup_authoritative_sql.ps1
# Review the 4 selected database names, user, PostgreSQL install path, destination.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup_authoritative_sql.ps1 -Apply
```

脚本对每个数据库运行 `pg_dump -Fc`，使用 `pg_restore --list` 进行**格式可读取**检查，写入 SHA-256 清单。它并未做真实恢复演练，不能证明备份可恢复完整系统。PostgreSQL 每个数据库有独立 MVCC 快照，四个库**没有跨库同一时刻的事务快照**。如果跨数据库一致性重要，需要应用级写入维护窗口和一致性协议。

恢复演练（需独立隔离环境）：准备 PostgreSQL 17 → 校验 dump/manifest SHA-256 → `pg_restore --list` → 在空白测试数据库 `pg_restore` → 运行 schema/表行数/关联约束检查 → 验证 Qdrant 派生索引恢复或从 SQL 重建 → 使用真实但匿名化的 QA/BUG/式样书验证。不能直接覆盖生产库进行恢复演练。

Qdrant 是派生索引，但从 SQL 重新计算 BGE-M3 embeddings 可能耗时很长，**RTO 不可按瞬时重建估算**；生产应有受控的 Qdrant 快照/数据卷备份方案，并记录 Embedding 模型缓存/权重版本。

## 6. 局域网、隐私与安全

1. `scripts/check_enterprise_readiness.ps1` 是只读检查，会指出 `18080,5432,6333,6334,11434` 是否被绑定在非 loopback。Qdrant 历史配置若为 `0.0.0.0:6333-6334`，先确认并备份 `qdrant_storage`，再按原脚本安全重绑。不要直接删除向量卷。
2. 只有 Open WebUI 3000 可以按企业授权对局域网共享。**明文 HTTP 不适合正式业务**；正式使用需内网 HTTPS 和受控入口。
3. 共享 API Key 意味着后端当前尚**不能**实现每位同事的精确 21MM / 24MM / QA-BUG 数据隔离。正式部署需要可信服务端身份、Backend RBAC 和审计链路，不能只依赖 Open WebUI 的用户登录。
4. 本地模型不会自动保证没有联网，必须额外关闭 Open WebUI 外部模型、联网搜索和外部工具，并通过网络出站规则限制意外访问。把 PDF、Jira 文本视为不可信数据，不能允许其诱导 Agent 越权或执行模型生成 SQL。
5. 不要关闭内网公司安全软件、绕过 Wi-Fi 的 AP 隔离或打开数据库和模型端口来修复客户端连通性问题。

## 7. 完整验收建议

- 用 21MM 508/550 的 **真实版本**验证双端关系和证据；确认模糊引用为 `REVIEW_REQUIRED` 或未解析，而不是伪造精确边。
- 两个用户同时发送复杂问题，第三个收到明确拒绝；验证 429 后客户端不会无限重试。
- 开启一条流式回答，在客户端取消连接后查看指标 `active` 应回到 0。
- 在授权测试窗口**只终止经确认的本项目 Backend 进程**，等待看护重启，验证 Open WebUI 模型恢复。不得误结束公司其他进程。
- 模拟 PostgreSQL/Qdrant/Ollama 短暂离线，检查 `/health/dependencies` 和回答失败时是否有明确状态；并验证服务恢复。
- 备份数据库，在**隔离测试实例**做完整恢复演练；确认 SQL 真实性与 Qdrant 检索一致性。
- 记录 GPU/CPU/RAM、P50/P95 响应、错误率、每用户权限，以及内网 HTTPS/防火墙基线。

本版本不宣称这些在用户 Windows/公司 Wi-Fi 上已经执行。 
