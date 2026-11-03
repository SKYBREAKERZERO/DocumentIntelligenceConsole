# Stage 2 v1.35 — 全工程库 LOGIN 角色权限审计与有界事务

## 范围

v1.35 基于 v1.34 增加部署前的 PostgreSQL **只读安全审计**，不修改数据库或原有受控审核 SQL，不升级数据库 schema，不授予角色权限。任何失败都必须 `BLOCKED`，绝对不可根据一个绿色预检结果自动修改关系状态。Stage 2 的双端原文、工程/版本/页码/章节/offset/SHA-256、人工审核和 `DB_SESSION_USER` 受控审计规则均保持不变。SPEC_BODY/PRIMARY_CONTENT 正文优先，Coversheet 不得成为技术证据。

## 关键变化

1. 不依赖操作员事先枚举全部 `--app-role`：扫描当前数据库里 **有效拥有 CONNECT 权限的全部非超级用户 LOGIN**。若超过 500 个，拒绝通过，避免 `LIMIT` 产生的假安全报告。
2. 对每个登录检查当前受控函数的有效 EXECUTE 是否与 active DBA reviewer 登记匹配；检查通过角色链得到的函数所有者 MEMBER 权限（含可能的 `SET ROLE` 路径）、关系表 / 审计表 UPDATE（包括列级授权）、DELETE、INSERT 审计记录、TRIGGER、TRUNCATE、模式 CREATE 等。
3. 检查两条 guard 函数保持 `SECURITY INVOKER` 和 `search_path=pg_catalog`，受控函数的专用 NOLOGIN owner 不带 CREATEDB、CREATEROLE、REPLICATION、BYPASSRLS。仍需 DBA 独立检查函数源代码及对象 owner / 默认权限。
4. 原本点名的 reviewer 还必须出现在有效 CONNECT 登录扫描中。预检 `schema` 为 `dic-stage2-readonly-preflight-v2`；失败退出码 2，缺陷或 DB 连接错误为非零异常；从未生成生产验收合格证。
5. GUI 审核事务内 `lock_timeout=5s` / `statement_timeout=60s`；授权预检事务内 `lock_timeout=5s` / `statement_timeout=90s`。锁冲突会失败并回滚；**禁止在不核对数据库最终状态时盲目重试有歧义的批准提交**。

## Windows 执行（需已存在的专用只读 DBA 审计登录）

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.35_CN_FULL'
.\.venv\Scripts\python.exe .\scripts\preflight_stage2_db.py `
  --host 127.0.0.1 --port 5432 `
  --database rag_21mm --project 21MM `
  --audit-user stage2_auditor --reviewer stage2_reviewer_01 `
  --app-role app_user --app-role outbox_worker `
  --output .\output\stage2_preflight_21mm_v135.json
if ($LASTEXITCODE -ne 0) { throw 'Stage 2 v1.35: 预检未通过，禁止提交审核' }
```

密码来自会话环境变量 `POSTGRES_PASSWORD`，若未设置会交互询问，禁止写入配置文件、命令历史或提交到代码仓库。JSON 只保存角色名称、检查名及原因，不输出业务正文或密码，但仍属于内部安全资料；输出只允许新文件，不能覆盖已有证据。

## 部署前仍未解决的边界

- 全库扫描范围是 **可 CONNECT 本工程数据库的非超级 LOGIN**；数据库超级用户、对象及 schema 所有者、可修改受控函数/trigger 的 DBA 是信任根，不能靠该扫描证明未被侵害。被临时授予权限、快照后权限变化、跨数据库或网络级绕过，也无法从历史报告证明安全。
- 角色关系通过 `pg_has_role(..., 'MEMBER')` **保守**检查所有者关系；可能导致比实际 SET ROLE 更严格的拒绝，但不会因此自动授权。
- 用真实 PostgreSQL 17 分别登录 reviewer、应用、Worker 和未授权登录，实测函数调用、撤销权限、列级 GRANT、角色链、锁超时、断线、重启和事务回滚。不能以合成 cursor 单测替代真实 `pg_catalog` 验证。
- 原始 `Bluetooth Audio function spec v2.52.pdf` 正文与 `v2.50 Cover Sheet` 应由人工审查真实 508→550 来源证据；无真实文档或人工 Gold 不得声称 Stage 1/Stage 2 正式验收。
- RAG 全链 SQL+Qdrant 结果、独立本地 WebUI/Windows Python 3.12、审核 UI，均仍需真实部署测试。本源码 ZIP 并不代表 EXE 已打包或 GitHub 已推送。
