# 分批同步与条件判定（2026-10-28 增补）

本页描述本次代码的能力及操作方式。生产数据未批量同步，新增审核结构只部署到独立合成验收库。

## QA/BUG 一键流程的变化

原生文件 → JSON/JSONL → QA/BUG SQL → Qdrant → 质量核对的入口保持原样。

- 同步按 SQL 主键分页，正文/向量工作批次最多 128 条；不再把整个 QA/BUG 正文库或全部向量载荷读入内存。
- 同时检查 SQL 状态和真实向量。SQL 写着 COMPLETED，但点丢失、正文或指纹不符时，会重新处理。
- 每批向量确认完成后，才提交 SQL 完成状态和索引状态。中断后保留已提交批次；再次增量同步会核对实际两端，跳过已完成内容。
- 嵌入计算后再次锁定和检查源记录、正文及指纹，来源变动时显示 PARTIAL，不强行标记完成。
- 同一个数据库/集合的同步使用数据库会话锁互斥。连接消失后锁会释放；新任务取得锁后会将遗留 RUNNING 审计标为 INTERRUPTED。
- 全量重算采用原集合分批覆盖，不先删除整个集合。取消并非后台自动续跑；需要再次点击增量同步。
- 质量核对也改为分页：SQL 每页 256 条、向量每页 512 条，检查缺失、孤点、正文/指纹、载荷索引及前后计数，并响应取消。
- 预览为兼容原界面仍保留片段 ID 列表，内存随 ID 数增长；不再保存全部正文/载荷。本次没有重写式样域旧同步器，也不能声称全部导入器、模型和所有查询均为常量内存。

实际验证：14 个隔离 QA/BUG 片段；删除一个测试点后补回 1 个；取消时提交 2 个，再运行补齐 12 个，第三次写入 0 个。正文被篡改但声明哈希保持不变时，分页校验仍拒绝通过。离线生成 2 万条同步数据和 3 万条校验数据，分页过程的 Python 跟踪内存峰值均低于 6 MB；这不包含模型权重，也不是百万条真实生产吞吐测试。

SQL 与 Qdrant 不是一个原子事务。并发导入期间可能产生短暂不一致；前后数量稳定也不能证明所有并发变更都被捕获。检索继续从 SQL 核验原文/身份/哈希，异常时拒绝该证据。较大修复应选择业务低峰并在完成后再次增量核对。

## 有限条件判定的含义

新 `CONFORMANCE_CHECK` 是固定业务流程，不让模型编造 SQL、代码、判定规则或审批。AI 的自然语言理解不会代替审核记录。

只有以下信息同时可验证，才比较实测值：

1. 明确的 21MM 或 24MM、式样编号、软件版本字段、车型、地域。
2. 唯一、已受控审核的“软件版本/车型/地域 → 式样版本/正文文档”映射。
3. 绑定映射指纹、content_id、正文哈希、章节的已审核检查。
4. 该已登记 MAIN/APPROVED 式样正文仍是本体、内容哈希与审核时一致。
5. 用户或票内明确提供该项测量值以及配置中每一个适用条件。

当前有限规则支持英文原文字面锚点：`within 700 ms` 最大值、`between 800 ms and 1500 ms` 闭区间、`shall remain active` / `shall be active` 状态比较。时间单位为 ms 或 s，按所配置测量键解释，不自动换算。拒绝负时间、非有限数、阈值与原文不一致、任意执行代码。其他语言/复杂组合语义需要人工核查，不能自动认证。

人工审核还必须检查整条原文的前置条件、例外、适用范围与测量定义。代码不会自动证明配置已覆盖了原文的全部条件；本体中未配置的其他要求也不在结论范围内。

结果只可能为：

| 结果 | 解释 |
| --- | --- |
| MATCHED_CONFIGURED_CHECKS | 在本次已核查的配置条件内符合；不是整本式样认证 |
| POTENTIAL_NONCONFORMANCE | 至少一项适用检查与实测值不一致，疑似不符合，需要人员复核 |
| UNDETERMINED | 映射/审核/原文不可验证、缺少条件或测量值、规则不适用等，证据不足 |

配置中的非适用项返回 NOT_APPLICABLE，不把“例外生效”误判成不符合。所有适用项通过且没有 UNKNOWN 才返回配置范围内符合。用户输入与票内原始上下文/实测字段冲突时保持证据不足。用户的 `approved=true` 等文字不能授予审核权限。

## Open WebUI 的使用

原连接地址、模型 ID 和聊天界面继续使用。填写明确上下文和已经定义的测量键，例如隔离样例：

```text
21MM 是否符合式样？式样编号=990101；软件版本=SYN-SW-1；车型=SYN-MODEL；地域=JP；stop_latency_ms=900；ignition=OFF；emergency_call=false
```

这是测试资料专用示例，生产库没有这些审核配置。实际使用应换成管理员已配置的身份和测量键。900 ms 相对该测试版本 700 ms 的已审核上限会返回“在已核查条件内疑似不符合”，同时给出源文件、版本、章节、物理页、content_id、SHA256 和原文。缺少紧急呼叫状态会返回证据不足。

查询起票前是否有历史票仍优先查 QA/BUG；找到强相似候选时停止式样兜底，由人员判断是不是同一问题。未召回不等于不存在。没有强候选再查询式样；如果用户还要求符合性，继续尝试上述受控检查，不能用当前版本候选顶替适用版本。

固定 SQL 事实、票据溯源、起票前流程和有限判定的聊天接口现在可在未配置 Ollama 模型时响应。开放式生成仍需要本地模型。Open WebUI 模型列表/健康状态可能仍显示模型或嵌入尚未就绪，应按实际依赖状态判断可用范围。

新增认证只读接口 `POST /api/v1/testing/assess`：

```json
{
  "scope": "21MM",
  "specification_code": "990101",
  "context": {"software_version":"SYN-SW-1","model":"SYN-MODEL","destination":"JP"},
  "observations": {"stop_latency_ms":900,"ignition":"OFF","emergency_call":false}
}
```

继续使用原 Backend Bearer 令牌；返回 `assessment` 和 `evidence`。该接口不创建规则、不审核、不写库。普通聊天及 SSE 返回的 `rag.workflow` 也保留判定状态。

## 管理员配置与审核

新增 `scripts/manage_conformance.py` 是本地管理工具，默认只预览。此版尚无规则编辑图形界面；不能把它称为已完成企业审批平台。

部署前由数据库管理员检查专用 QABUG v1.38 前置结构，再审阅可选 `database/upgrade_conformance_checks_v1.sql`。用专门 NOLOGIN、非超级用户的受信 owner 安装新 schema/函数/触发器，并使映射表属于同一 authority；不要让普通应用或审核登录成为 owner 成员。以普通应用登录安装会导致运行时 readiness 失败，这是预期保护。

管理员按本企业流程登记独立非管理员审核登录到 `dic_conformance.authorized_reviewers`，授予 schema USAGE、映射/checks/audit 必要 SELECT、`fingerprint(text,bigint)` 与 `review(text,bigint,text,text,text)` EXECUTE。只读运行账号也需要必要 SELECT 与 fingerprint EXECUTE。提交账号按需授予映射/checks INSERT 及对应序列 USAGE；直接修改 APPROVED/REVOKED、删除对象或改写审计均受触发器约束。脚本不建角色、不授予权限、不登记审核人。旧表自称 APPROVED 而没有这套真实审计的记录不会被新判定器信任。

映射模板见 `examples/conditional_checks/mapping.template.json`，检查模板见 `check.template.json`。先从数据库浏览确认真实 version_id/content_id，用完整值替换 null 占位符。映射的版本标签和文档 ID、检查的正文哈希和章节均由工具读取 SQL 绑定，不能由输入伪造。

```powershell
# 默认只读预览；显式 --apply 才插入 PENDING 提案
python scripts/manage_conformance.py propose --kind MAPPING --file my_mapping.json
python scripts/manage_conformance.py propose --kind MAPPING --file my_mapping.json --apply

# ID 是刚插入的真实 ID；审核时复制 inspect 输出的完整指纹
python scripts/manage_conformance.py inspect --kind MAPPING --id 123
python scripts/manage_conformance.py review --kind MAPPING --id 123 --review-login reviewer_name --fingerprint "复制真实64位指纹" --status APPROVED --reason "人工核对了车型地域、软件版本及式样原文适用性"

# 映射审核后再提交 CHECK，检查也须独立 inspect / review
python scripts/manage_conformance.py propose --kind CHECK --file my_check.json
```

reviewer 的密码默认隐藏输入，也可通过 `DIC_CONFORMANCE_REVIEW_PASSWORD` 环境变量传递；不要把密码写进命令行、模板或共享 ZIP。可用 `--config-dir` 选择已保存的内部配置目录。

支持 PENDING → APPROVED、APPROVED → REVOKED、REVOKED → PENDING；直接状态跳跃、过期指纹、未登记/被停用审核登录及管理员登录审核会失败。撤销不要求原文仍有效。检查原文或绑定内容变化后，应提交新的待审核配置，不复用旧审核。

运行时部署后仍需实际企业人员完成审核、业务金标准评估与生产发布；本包的合成审核账号只证明权限机制可运行，不代表企业人员已经签字。

## 复现和设计参考

离线回归：`python scripts/validate_core_backend.py`。本次受影响集的最终数量以 `RELEASE_CONDITIONAL_CORE_VALIDATION.json` 为准。

独立服务环境需安装 PostgreSQL 工具、有本地缓存 BGE-M3 和可用 Qdrant。测试脚本固定独立端口 15432，并核验数据目录；不要把隔离状态改成生产地址。按原 `docs/WORKFLOW_DELIVERY_2026-10-28_CN.md` 建立隔离环境、装入样例后，执行 `verify_isolated_review.py`、`verify_conformance.py` 与 `verify_streaming_sync.py`。脚本会写自建合成数据及专属集合，不能用于直接生产验收。报告在 output/isolated_acceptance；发行包只收录脱敏摘要。

参考：[Qdrant upsert 官方接口](https://api.qdrant.tech/api-reference/points/upsert-points)的稳定 ID 覆盖和等待写入完成契约，以及 [PostgreSQL 显式锁文档](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)的会话互斥方式。本次将它们落实为可重放批次和单集合任务互斥。核对日期 2026-10-28，实际兼容性依据当前本机服务测试；未升级本机依赖。
