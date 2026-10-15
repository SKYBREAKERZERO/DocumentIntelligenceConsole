# Stage 1 v1.29 — Windows、依赖及 PostgreSQL 只读验收

## 修复范围

本次不是新功能阶段，而是 v1.28 Stage 1 正式验收阻断项的工程修复。原 `requirements.txt` 为 UTF-16，已经转 UTF-8，保留原版本清单并为 `pywin32` 加上 Windows 专用条件。修复旧测试收集阶段访问数据库 / 丢失 PDF 等副作用；导入项目路由、Pipeline 不会主动连接数据库。没有依赖的 GUI 相关测试属于显式未执行，不代表验证通过。

## Windows 11 + PowerShell 5.1 + Python 3.12

从 v1.29 完整 ZIP 干净解压到独立目录，保留已存在的用户数据与 PostgreSQL 内容，不在旧版本目录原地覆盖。必须有 `py -3.12`：

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.29_CN_FULL'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_v129_windows.ps1
$py = '.\.venv\Scripts\python.exe'
& $py -m pytest -q
& $py .\scripts\verify_runtime_package.py
& $py .\scripts\stage1_environment_smoke.py --out 'C:\authorized\Stage1.v129.offline.json'
```

注意：精确锁版本可能因 wheel / pip 仓库实际状况而安装失败；安装失败必须记录原始 `pip` 输出并视为 **BLOCKED**，不要删除失败项来伪造成功。报告默认不连接 PostgreSQL，也不创建 Tk 窗口。

## 显式 Windows GUI 导入检查（不代表窗口验收）

```powershell
& $py .\scripts\stage1_environment_smoke.py --check-gui-import `
  --out 'C:\authorized\Stage1.v129.gui_import.json'
```

必须由工程师手动执行 `run.ps1`，检查项目切换、PDF 导入、数据库配置、报告输出和关闭/再启动，记录 Windows/Python 版本及实际操作结果；脚本只验证模块导入成功，没有创建交互式 GUI。

## PostgreSQL 实际只读连接验证

使用数据库管理员授予 SELECT 的专用角色（不要使用超级用户或正式写入凭据）；从本机安全注入 `DIC_STAGE1_RO_PASSWORD` 环境变量，切勿在命令行写出密码。示例参数中的数据库名是范例，必须按实际项目替换：

```powershell
& $py .\scripts\stage1_environment_smoke.py --check-db `
  --db-host 127.0.0.1 --db-port 5432 --db-name rag_21mm `
  --db-user stage1_review_ro --db-password-env DIC_STAGE1_RO_PASSWORD `
  --out 'C:\authorized\Stage1.v129.sql_ro.json'
```

只有指定 `--check-db` 才连接真实 PostgreSQL。连接设置 `default_transaction_read_only=on` 和 `statement_timeout=5000`，运行 `BEGIN TRANSACTION READ ONLY`、`SELECT current_database(), current_setting('transaction_read_only')`、`ROLLBACK`，不查询原始业务正文，不执行写入。无连接或密码环境变量时报告应是 `BLOCKED`，不会记录凭据、DSN 或原始数据库异常。此检查只证明当次连接和事务行为，并不证明 v1.26 的来源链 SQL 已正确映射。

之后使用 v1.25/v1.26/v1.27/v1.28 提供的审查工具，完成原始 PDF、Pipeline JSON、SQL 来源、人工 Gold 的证据包；报告输出目录应受控且禁止重复覆盖。

## 阻断与签收界限

- `stage1_production_acceptance: NOT_VERIFIED` 在本次所有自动脚本中固定不变；即使导入/只读连接全部 PASS，也不能由本地脚本签发生产批准。
- 当前开发环境没有原始 DCM/550 PDF、真实 PostgreSQL 凭据或 Windows GUI；相关生产验收均未执行。
- 对大文件吞吐、故障恢复、备份与回滚、安全权限和独立人工双语/跨页 Gold 仍需要真实环境测量及签收。
- Stage 2 SQL+Qdrant 检索开发可以准备，但未经证实的 Stage 1 关系不得当作已确认知识写入生产图谱。
