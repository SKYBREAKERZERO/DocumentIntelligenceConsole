# v1.40 工程内容结构关系与历史变更联合检索（只读）

## 目的和边界

v1.40 以 v1.39 为基线。**没有新增数据库表、迁移、GRANT 或自动关系审核**，直接读取现有 v7 `ei_requirement_units`、当前 `specification_versions`、`contents`、本体 `documents` 以及现有 `READY` 历史差异。

`STRUCTURAL_LAYOUT` 只描述三种可重复核验的布局事实：

1. `SAME_SECTION`：两个独立、原文跨度/哈希均通过校验的单元处于同一明确编号章节。
2. `PARENT_CHILD_SECTION`：同一文档、同一版本、同一 SPEC 的章节标识构成**恰好一级**的父子关系，例如 `4`→`4.1`，不把 `4`→`4.1.2` 当成直接父子。
3. `ADJACENT_INDEXED_UNITS`：在**同一 content chunk** 中按偏移排序连续、两个跨度不重叠，且扫描集中的其他 Requirement Unit 没有插在二者之间。此处“相邻”指已抽取/已索引单元，不声称 PDF 页面物理连续，更不声称工程因果关系。

三者全部有双端原文、Unicode 偏移、SHA-256、文档、版本、页面、章节及 unit ID。只允许正文角色（`PRIMARY_CONTENT`／`SPEC_BODY`），`REJECTED` 单元不可作为结构证据。扫描每本式样书最多 120 单元；发现第 121 个时拒绝用不完整片段计算“相邻”。只读动态查询**不是持久化全量结构图**，未检索到不等于没有关系。

`STRUCTURAL_REFERENCE` 保持 v1.39 的明确章节引用逻辑和双端校验。系统不得将上述结构关系或引用自动写成 `ei_content_relations.CONFIRMED`。

## 无编号问句

无精确 SPEC 编号时：先沿用已有 SQL 本体式样名称识别。若查询同时启用了语义/SQL 检索，则只从 `evidence_role=PRIMARY_CONTENT` 且返回真实 SPEC code 的候选中最多取 8 个作**搜索范围**。候选本身不得当作关系结论，所有关系仍须 SQL 版本、双端正文与 hash 再校验。无匹配候选或范围溢出时保守返回空，不进行无限制全库扫描。中文问法与英文式样标题完全不匹配时仍可能检索失败，尚不等同 Stage 4 混合召回。

## 变更联合检索

当 Agent 同时选择 `relation_search` 和 `version_search`：

- 首先沿用现有双端验证的当前 `STRUCTURAL_REFERENCE` 和持久化 `CHAPTER_VERSION_DIFF`。
- 对用户**没有给定章节号**但发现真实被引章节的情况，至多再按两个目标章节补查历史变化；仍然只读既存 `READY` 且双方文档为本体的变更记录，独立检查 old/new SHA-256。
- `REFERENCE_CHANGE_REVALIDATION` 要求工程库 scope、目标 SPEC 编号、章节（允许已引用章节下属子章节）、**目标当前式样版本和 document_id** 都一致，且变更有可核验 ID、引用来源原文 hash。
- 输出为 **REVALIDATION_REQUIRED（需要人工重新核验）**，不是关系已经失效、存在冲突、功能受影响或“不符合要求”。与 v1.38 BUG 历史版本映射无自动联动。
- 历史对比尚缺 READY change set、未覆盖全部正文内容或无已识别目标时不能据此说“没有变化”。

## 回归与部署

- 入口：`app/ai/retrieval.py` 的关系/版本只读路径；校验规则在 `app/engineering_intelligence/deterministic_read_models.py`，证据强度隔离在 `app/ai/agent.py`。
- 本轮无 schema 迁移。旧的 v1.33/v1.37 受控审核 SQL **不会**被替代；人工批准仍须真实 DBA 部署和授权。
- Windows 源码测试：`python -m pytest -q`；特性回归：`python -m pytest -q tests/test_v140_layout_and_relation_change_join.py tests/test_v139_structural_reference_and_chapter_diff.py`。
- 真实 PostgreSQL 17、Windows GUI、客户 508→550 原始 PDF 和大库性能测试尚未执行；本版本没有验证生产数据库是否已有 v7 / READY 数据，没有变更 PostgreSQL 或 Qdrant。

## 进一步工作

Stage 2 的持久化/批量结构索引、无编号多语言实体消歧、大章节完整性凭证；Stage 3 版本映射与审核旧关系的跨版本再评价；Stage 4 Dense/Sparse/RRF/Reranker；Stage 5 LangGraph；Stage 7 真实库 Golden Dataset / p95 延迟均未在 v1.40 宣称完成。
