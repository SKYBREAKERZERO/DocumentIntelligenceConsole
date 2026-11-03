# v1.51 一体包使用说明（Windows 11 / PowerShell）

这个 ZIP **已经包含**完整 DocumentIntelligenceConsole Enterprise v1.51 源码、七类 Skills 定义、Backend 与 Windows GUI、依赖列表、v1.51 SQL 事实问答修复说明及 `scripts/verify_v151_spec_facts.ps1` 现场测试脚本。不需要另外下载修复文件。

## 1. 新目录解压（不要覆盖正在运行的 v1.50）

将 `DocumentIntelligenceConsole_Enterprise_v1.51_ALL_IN_ONE.zip` 下载到 Windows 的 Downloads 目录，在 PowerShell 执行：

```powershell
New-Item -ItemType Directory -Path 'C:\mission\Python' -Force | Out-Null
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\DocumentIntelligenceConsole_Enterprise_v1.51_ALL_IN_ONE.zip" -DestinationPath 'C:\mission\Python' -Force
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.51_CN_FULL'
Test-Path '.\app\ai\spec_facts.py'
Test-Path '.\scripts\verify_v151_spec_facts.ps1'
```

两个 `Test-Path` 应当返回 `True`。如果下载路径不同，请将 ZIP 文件路径换成实际位置。

## 2. 虚拟环境和依赖

使用本机 Python 3.12，项目自带固定版本的 requirements；若已在新目录安装可用 `.venv`，不必重复安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r .\requirements-api.txt
```

首次依赖安装可能需要网络，但 Backend 的业务检索仍执行 INTERNAL_ONLY。安装可能包含较大的 PyTorch 依赖，须确保磁盘空间。

## 3. 安全切换 Backend

- 在**原来的 v1.50 Backend PowerShell 窗口**按 `Ctrl+C`，或由 GUI 正常停止它。
- 不要停止 PostgreSQL、Qdrant、Ollama、Open WebUI 或 LangGraph。
- 不要直接结束未知 PID，也不要在 18080 尚被占用时再次启动。

检查 18080 是否空闲：

```powershell
Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue
```

如果没有返回监听进程，在**新目录**启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_backend.ps1
```

保持窗口打开。需要 Windows Tool 时，在另一个 PowerShell 中进入相同新目录并执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

## 4. 真实数据库事实问答验收

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_v151_spec_facts.ps1
```

脚本对“BT/550 文件存在性、正文目录、21MM 式样目录、相邻可信本体版本变化”执行四个只读端到端查询。仅凭脚本和离线单元测试通过，**不能**宣称 21MM 550 v2.52 已完成客户数据验收。

## 5. 文档位置

- 修复说明：`docs/DocumentIntelligenceConsole_v1.51_SQL_Factual_Answer_Remediation_CN.md`
- 详细工程说明：`docs/ENGINEERING_v1.51_SPEC_FACTUAL_QA_CN.md`
- 七类 Skills：`config/skills/`
- 源码和测试：`app/`、`tests/`

本一体包只重新组织交付资料，**未更改 v1.51 的 Python 业务源码或测试**，不执行数据库迁移、索引重建或人工审核写操作。
