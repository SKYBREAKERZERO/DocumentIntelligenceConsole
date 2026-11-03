# Stage 2 v1.36 — 关系引用定位精度与安全解释

## 升级范围

本次仅将 `REFERENCES_SECTION` 从“一个段落提到另一个式样书章节”强化为有明确原文跨度的、能够定位目标编号和目标章节的**引用锚点**。它仍然是**提案/引用**，并不自动证明要求满足、接口依赖、条件冲突、版本影响或因果关系。

所有 Stage 2 的基本约束继续生效：源/目标双方的 `SPEC_BODY` 或等价本体优先，明确排除 Cover Sheet；版本/工程归属一致、两端原始 `contents`、Unicode 偏移、正规化 SHA-256、Stage 1 完整回填及 DB 认证人工审核。`CONFIRMED` 仍须具有 PostgreSQL `DB_SESSION_USER` 最后审核记录和受控函数/触发器环境。

## 实际变化

1. **共享解析器**：`app/engineering_intelligence/relations.py` 的 `extract_reference_anchors` 生成 `ReferenceAnchor`，带原文（没有重写）、起止码点偏移、目标式样书编号和目标章节。对多个规格引用按“下一个规格引用”分隔，限制在同句/同子句且不超过 120 个 Unicode 字符的范围内；同一片段存在多个明确章节标记则视为歧义，无要求级边。
2. **一对一才建立要求边**：`link_explicit_references` 对目标章节中的多条 Requirement Unit 不再按语义相似度随意挑选。已有 SQL `backfill_content_relations` 仍把此情况保留在 `ei_unresolved_relation_anchors` 的 `AMBIGUOUS_TARGET_SECTION`，不会自动审核。
3. **应用级复核**：`load_review_case` 在完整来源复验之后，再核查来源引用确实指向当前目标编号/章节。审核 GUI 展示原文引用和跨度。Python 受控审核客户端在正式调用 DB 函数前复验；失败即拒绝。
4. **RAG 证据门禁**：`_content_relation_search` 检查确认状态、身份、双端哈希后，还复核引用锚点；不匹配的 `REFERENCES_SECTION` 不得进入模型上下文。有效边提示“仅为章节引用，不自动推断工程依赖/变更影响”。其他受人工审核的关系类型没有被自动升级为某种工程因果事实。
5. **只读离线图**：导出新增 `reference_anchor`，渲染时转义；验证器重新从原文推导锚点，与 JSON 属性逐字段对比，不相信仅重新计算的清单哈希。
6. **不确定提交提示**：当 PostgreSQL COMMIT 或网络返回丢失，GUI 不能说“已回滚”。人工必须重新连接，按候选 ID 只读查看状态、最后一条审计事件和来源哈希，确认结果后方可决定继续处理；严禁直接重复审批。

## 示例（合成，不是企业真实 508/550 事实）

| 来源正文片段 | 目标 | 处理 |
|---|---|---|
| `Refer to Spec 550 section 4.1` | 550 §4.1 **仅一个** Requirement Unit | `REVIEW_REQUIRED` 候选，非已证明依赖 |
| `Refer to Spec 550 for details. See section 4.1 in another spec.` | 550 §4.1 | 拒绝误关联；章节在另一个句子 |
| `Refer to Spec 550 section 4.1 and section 5.2` | 550 §4.1 | 拒绝此歧义锚点 |
| `Refer to Spec 550 section 4.1` | 550 §4.1 **多条** Requirement Unit | 只保留待解析的章节级歧义，不随意选择目标要求 |
| `参照 Spec 550 section 4.1` | 550 §5.2 | 不匹配，不可作为该边的引用证据 |

## 验证命令

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.36_CN_FULL'
.\.venv\Scripts\python.exe -m pytest -q .\tests\test_v136_stage2_reference_fidelity.py
.\.venv\Scripts\python.exe -m pytest -q
```

`tests` 使用合成 Requirement Unit 与 Mock PostgreSQL 游标；CI 绿灯≠生产验收。上线前还需对真实 508/550 PDF 本体 + PostgreSQL 17/Qdrant 及 Windows 用户审查流程作人工 Gold 和权限/锁/断线测试。

## 明确未实现的关键边界

- v1.33 的 **DB SECURITY DEFINER** 函数本次没有变更，数据库端尚未新增独立的 SPEC+section 引用语义解析条件；有 EXECUTE 权限的真实人工审核员若直接调用函数，可绕过 Python 解析器。要把引用真实性升级为数据库不可绕过的状态变更条件，后续必须设计与生产 SQL 兼容的受控函数版本升级/回滚及 DBA 部署门禁。**不能把 v1.36 宣称为 PostgreSQL 全链审核防误判闭环。**
- 旧版关系记录不会自动删除、变更审核状态或回填；读取时不符合新锚点要求的 `REFERENCES_SECTION` 会被隔离，需要人工复核或纠正来源关系。
- 句法式引用抽取不会识别所有日英中写法及表格标题/跨页间接引用。检测不到时应保留人工待解析状态，而不是推断关系不存在。
- 离线图谱的 manifest 不是数字签名；即使哈希可重算，也不能证明数据库审核人的真实身份、当前版本或当前有效授权。
