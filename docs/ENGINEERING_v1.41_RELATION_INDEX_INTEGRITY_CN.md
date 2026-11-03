# v1.41 Stage 2 关系索引完整性与 SPEC_BODY 版本选择（只读）

## 解决的误判/漏检

1. `specifications.current_version_id` 在历史资料中可能指向 Coversheet；v1.40 新结构查询直接等于该值，造成真正式样正文关系全部漏检。v1.41 使用统一 `sql_preferred_body_version_id()`，在 MAIN 分支排除 `SUPPORTING`、Coversheet 和未知角色，优先有效的当前正文，当前指针是封面时退到最新本体。若本体尚无 Requirement Unit，**不**借用旧版本结果。
2. v1.40 的“相邻单元”来自已被章节条件过滤的返回行；中间不同章节、甚至 `REJECTED` 单元可能被遗漏。v1.41 每条单元从数据库未过滤的同版本/同文档/同 content 全量单元中取得真正 `next_unit_id`，只有其等于右端 ID 且双端偏移无重叠才能判定 `ADJACENT_INDEXED_UNITS`。
3. `STRUCTURAL_REFERENCE` 的来源或目标单元若 `REJECTED`，拒绝输出规则证据；章节目标仍不可任意选某条作为需求级依赖。

## 只读索引审计

在 PowerShell 中设置**受限只读账号**的 `POSTGRES_PASSWORD` 环境变量，分别检查项目和 SPEC：

```powershell
python .\scripts\audit_stage2_relation_index.py --database rag_21mm --project 21MM --spec-code 550 --user dic_reader --output .\stage2-550-index-audit.json
```

CLI 拒绝 QABUG 数据库与超级账号，并先验证 `project_info` 的库名和 project_code 匹配。事务显式设置 `REPEATABLE READ READ ONLY`、5 秒锁等待与 90 秒语句超时；过程不自动写数据库或替用户部署 v7 迁移。

- 校验首选 SPEC_BODY、单一 SPEC 结果与版本身份。
- 通过已有 `require_complete_backfill()` 检查 v7 完整回填凭据、版本归属和条数。
- 顺序读取**全部** `contents`（包括不会生成 Requirement Unit 的章节块），重算与 backfill 相同的长度前缀 UTF-8 SHA-256 指纹。
- 遍历该版本每条 `ei_requirement_units`，校验真实 content 文本、Unicode 偏移、原文文本、规范化 SHA-256、版本/文档与总数。
- 最多 10000 个内容块、50000 个单元；超限、扫描不完整、摘要漂移、缺失凭据或角色不可信均 `BLOCKED`（退出码 2）。`PASS` 仅对当次数据库快照成立；不等于 Requirement Unit 的语义抽取完全正确、更不等于已经验证 508→550 的关系精度。

输出中仅包含 IDs、版本、计数与哈希，不输出要求正文。`--output` 禁止覆盖已有审计文件；可自行按制度加签名及归档。

## Stage 3 历史关系判定边界

`REFERENCE_CHANGE_REVALIDATION` 只证明被引章节存在经核验的、持久化的 READY 差异。旧关系当时是否有效、源版本是否匹配及变化后是否仍然成立都未证明：`prior_relation_validity=UNDETERMINED`、`source_version_alignment=NOT_ESTABLISHED`。不得把“变更”当成“失效”或“冲突”。

## 验收未完成

当前工具运行环境没有 PostgreSQL 服务连接/真实角色、Windows GUI 或客户 508 与 550 实际 PDF；所有新增测试都是模拟 DB 的拒绝性测试。实际验收仍需 DBA 用只读账户在 PostgreSQL 17 上执行该审计与原有 v1.37 权限/语义预检，并建立真实 PDF Golden Dataset、性能 p95 和人工判断。Stage 4 Hybrid Retrieval 尚未在本轮实现。
