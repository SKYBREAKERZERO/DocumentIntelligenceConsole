# v1.53：550 Bluetooth Audio 双身份读取整改（不修改主数据）

## 已得到的现场事实（仅来自用户运行的只读数据库审计）

- `specification_id=2`：`spec_code=550`、`region_scope=EXCEPT_NA`、`current_version_id=2`；对应 `550_Bluetooth Audio Function Spec_Cover_Sheet_Ver.2.50改(電子承認).xlsx`（SUPPORTING_ONLY）。
- `specification_id=3`：`spec_code=NULL`、`region_scope=NULL`、`current_version_id=3`；对应 `Bluetooth Audio function spec v2.52.pdf`（SPEC_BODY 候选，MAIN v2.52，`version_id=3`）。
- `rag_chunks` 与 `rag_current_chunks` 中，ID=2 各 622 条、ID=3 各 85 条。两张视图可能重复同一批物理行，不能相加作为唯一 chunk 数量。封面侧污染与正文检索丢失的风险真实存在。
- 共 44 个相关引用字段的行数检查成功，地域属性不一致（`mixed_regions=true`）。这些事实不足以授权 ID 合并、规格编号更改或 Qdrant 改写。

## 本版实现

1. 完整文件名 / `550 BT` 的数据库查找仍以正式绑定的 `SPEC_BODY` 为准；没有找到正式绑定时，使用 **文件级、只读、精确标题** 候选查询，并 LEFT JOIN MAIN version 和 specification，回显真实 version_id、specification_id 和 spec_code。
2. 存在 MAIN 正文版本、但编号不匹配时，标记 `SPEC_IDENTITY_CANDIDATE` / `LIMITED`，不得自动提升为 `SPEC_LOOKUP` / `STRONG`。未关联版本文件仍为 `SPEC_UNBOUND_FILE`。
3. 对身份候选 PDF，可以通过实际 document_id 查询章节与小节，回答 `SPEC_OUTLINE_CANDIDATE` 并显式标注 **编号归属未核准**。不从 Coversheet 补齐章节。
4. 对历史差异，无两份经核实的同一式样 MAIN 本体版本就拒绝比较。不得把 v2.50 封面算作 v2.52 正文的前一版本。
5. `/api/v1/retrieval` 增加 `document_id` 和 `file_name`，可复核查询结果来源，不打印技术正文或 API token。
6. 无数据库写入，无自动合并，无 Qdrant payload 更改。**工程发布状态：NO-GO，仍需真实环境联调以及身份/地域审批。**

## Windows 现场验收

在 `C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.53_CN_FULL` 目录准备 venv，正常停止旧 Backend 后启动 v1.53，并执行：

```powershell
.\.venv\Scripts\python.exe .\scripts\verify_v153_550_readonly.py
```

预期 `spec_lookup` 能找到实际物理 PDF，返回 `SPEC_IDENTITY_CANDIDATE` 且 `strength=LIMITED`；`spec_outline` 返回该 PDF 的数据库真实目录（前提是 chapters/sections/contents 存在）， `spec_version_facts` 不生成虚假的双版本差异。**验收脚本不将“路线正确”等价于“目录存在”。**

## 正式归并前必须完成的检查

- 由有权维护式样目录者确认 v2.52 PDF 是否确属 **550**，以及 `EXCEPT_NA` / `UNSPECIFIED` 的地域含义是否一致。
- 按 FK、视图和自定义字段逐条统计迁移需求；另外审查 `documents.metadata`、`contents`、`embeddings`、`rag_chunks`、Qdrant payload 内部字符串形式的 `specification_key` / `specification_code` / `specification_id` 引用。
- 经审批后才可编写带备份、反向回滚、幂等检查和一致性门禁的事务性数据修复；禁止直接 `UPDATE specifications SET spec_code='550' WHERE id=3`。
