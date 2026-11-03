# Stage 2 v1.34：PostgreSQL 只读授权预检与主 GUI 审核入口

本次彻底停用旧版自报 actor 的客户端直写审核 API；只能通过数据库认证审核函数提交。没有迁移数据库或给账户授予权限。继续使用 v1.33 既有人工审核受控函数与双端证据核验，不重新解释历史审批。**本次新增的是可以拒绝不合格权限配置的部署预检**，不是生产验收合格证。

## 执行前条件

- 每个工程项目使用独立 PostgreSQL 数据库，`public.project_info` 恰好包含目标 `project_code` 一行。不能在 QA/BUG 混合库操作。
- 先由 DBA 审核并安装 `database/upgrade_engineering_stage2_v1_33.sql`，完成备份和恢复演练。要求该受控函数由专用 **NOLOGIN、非 SUPERUSER** 角色所有；若以前以登录管理员执行迁移，则预检将 **BLOCKED**，不能自动通过。
- 使用具备 `dic_review.authorized_reviewers` 读取权限的独立数据库审核账号执行预检。它只读检查系统目录、有效 GRANT 和审核员登记表；不能用普通审核员替代 DBA 权限审计。
- DBA 对 `stage2_reviewer_01` 明确创建唯一 LOGIN 并在 `dic_review.authorized_reviewers` 启用登记。不给任何关系审核表 UPDATE/DELETE、审核事件 INSERT/UPDATE/DELETE、`dic_review` 或 `public` 模式 CREATE；须检查角色组继承权限。

## Windows 只读预检

在 **v1.34** 项目根目录运行，先设置仅当前 PowerShell 会话中可用的 DB 密码环境变量（或不设，程序会交互请求密码；请勿把密码写进日志）。角色名替换成实际数据库中的账户，后两个 `--app-role` 是可选但建议的内部应用/Worker 账户。

```powershell
.\.venv\Scripts\python.exe .\scripts\preflight_stage2_db.py `
  --host 127.0.0.1 --port 5432 `
  --database rag_21mm --project 21MM `
  --audit-user stage2_auditor `
  --reviewer stage2_reviewer_01 `
  --app-role app_user `
  --app-role outbox_worker `
  --output .\output\stage2_preflight_21mm.json

if ($LASTEXITCODE -ne 0) { throw 'Stage 2 授权预检未通过，禁止启用审批' }
```

程序使用单事务 `REPEATABLE READ READ ONLY`，数据库身份、受控函数和触发器结构检查均来自同一快照。JSON 写入采用排他创建，不能覆盖旧证据；不包含审核原文、私有密钥或密码，但**包含角色名和数据库名**，按内部安全资料管理。

### 主要 PASS 条件

1. 正在连接的数据库名及工程身份均与指定值严格匹配，且只包含该工程项目。
2. 受控函数确实为 `SECURITY DEFINER`、固定 `search_path=pg_catalog`、由非登录且非超级用户的专用角色所有，并且未授予 PUBLIC 执行权。
3. 两个启用的 BEFORE ROW INSERT/UPDATE/DELETE 触发器必须绑定到精确的预期 guard 函数（仅检查同名触发器是不够的）。
4. 审核账号必须具备 LOGIN、无数据库管理权限、有活动 DBA 登记及显式函数 USAGE/EXECUTE。对关系表及审计表无敏感 DML 权限，对 `public` 与 `dic_review` 无 CREATE 权限。
5. 通过 `--app-role` 显式列出的应用、Worker 账号不能执行审核函数，且无上述敏感写权限。**未列出的其他账号不会自动得到检查**；DBA 必须审计数据库全部有效角色继承、对象所有权和潜在默认权限。

预检中有一项失败即返回 JSON `result=BLOCKED`、进程退出码 `2`。配置、查询、权限或网络错误会以非零退出码失败，不产生“PASS”。无现场数据库可用时，不能推断真实部署已满足这些条件。

## 主 GUI 的 Stage 2 入口

在 GUI 的 PostgreSQL 设置区域选择 **Stage 2 · 内容关系双端证据审核（独立登录）**。新窗口由独立 Python 进程启动，绝不使用或传递主 GUI 数据库密码。审核员须自行登录，查看双端完整原文，手动抄录候选 ID 与两端 SHA-256 前缀，填写理由，二次确认。受控写入由 DB 函数再验证当前版本和完整哈希。尚未打包/验收的旧 EXE 会拒绝打开该入口；请使用 Python 源码版。

## 上线前仍必须进行的人工验收

1. DBA 以实际审核账号验证：可 `SELECT` 预览和调用受控函数；不可直接更改关系 `status`、审计表记录、函数和 trigger；撤销授权后调用必须失败。
2. 以实际应用/Worker 身份确认不能执行受控审核函数、不能修改已审核状态；检查 `PUBLIC`、对象 owner、group、DEFAULT PRIVILEGES 与 `SET ROLE` 路径。
3. 用真实 PostgreSQL 17 注入锁争用、连接丢失、并发版本切换、回滚及重复提交，确认状态与审计事件始终一致。
4. Windows 现场实际运行 GUI，检查独立密码输入、不持久化、失败回滚和角色切换。
5. 核验 508→550 真实式样正文 `Bluetooth Audio function spec v2.52.pdf`（不接受 v2.50 Coversheet），双端原文、章节、页码、Unicode 偏移、SHA-256 及当前版本严格一致。

检查输出绝不能当作外部签名或 WORM 证据；数据库所有者和超级用户仍属于信任根。
