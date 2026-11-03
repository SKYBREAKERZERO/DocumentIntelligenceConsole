# v1.39 — Stage 2 / Stage 3 确定性证据查询（只读）

## 两条证据类型、两种证明范围

1. `STRUCTURAL_REFERENCE`：只证明 **508 本体的某条原文明确引用 550 本体的某章节**。必须存在 v7 `ei_requirement_units`、源/目标 `contents` 与已选当前 `specification_versions`；限定同一工程库与 `branch_name='MAIN'`，严格筛选本体；计算两端完整 Unicode 字符偏移、原文及 NFKC/空白正规化 SHA-256。仅支持明确的 `Spec 550 section 4.1` / 日中英可识别引用格式。目标章节有 2–8 个要求时逐条列出证据，仍是**章节对章节引用**，不得描述为“508 第 x 条依赖 550 第 y 条”；超过 8 条、缺失、源文本漂移或候选扫描截断时拒绝展示，而非部分证明。无需安装 v1.33 审核函数才能出现该类结构性证据；人工审核状态和关系表不变。
2. `CHAPTER_VERSION_DIFF`：只展示现有、状态 `READY` 的 `specification_change_sets` + `specification_changes` 结构化正文比较。版本两端必须属于同一本式样的 `MAIN` 本体。仅当用户显式给出 `section/§/章节/第N章` 或 `关键词:` 时采用该细粒度路径；版本对如 `v2.50`→`v2.52` 精确匹配。逐项验证已持久化文本的原始 SHA-256（不是 v7 Unit 的正规化哈希），记录变更 ID、章节、页码与版本来源。它验证的是**缓存的章节文本快照**，并非与磁盘 PDF 的再次逐页比对；若需要证明缓存仍对应现场 PDF，须运行版本归档与 PDF Gold 核验。

## 示例（同一检索入口）

- `508 引用 550 的哪些章节？`：Agent 选择 `relation_search`，除原人工审核关系外，独立尝试 `STRUCTURAL_REFERENCE`；没有审核记录也可以有字面引用证据，但**不会出现已证明依赖/影响的表述**。
- `550 v2.50 到 v2.52 的 section 4.1 有何变化？`：Agent 选择 `version_search`，使用已存的 READY 差异，并附 old/new 段落及页码，不由小模型发明变化。
- `550 关键词:mute 的历史版本变化`：只读显式关键词过滤，不做模型语义猜测。没查到并不意味着没有变化：可能是相应版本对未保存 READY 的正文 diff。

## 运行条件、部署与验收

- 以 v1.38 源码升级；**没有 v1.39 数据库迁移**。工程库原有 `ei_requirement_units` 必须先按 Stage 1/v7 流程回填，并保持正确 `contents` 外键。章节变更只在已有 `specification_change_sets` 和 `specification_changes` READY 快照时可用。
- 运行账户需具有相关工程库只读权限；未授权或表不存在时该工具失败并返回检索警告，不会提升权限、不自动迁移、不写入。运行时不连接外网。
- 完整输入集合保护：单项目引用源候选扫描上限 240，单目标章节最多 8 个 Requirement Unit，单版本差异比较最多读取 400 条，不冒充全库结果；需要更大范围应通过正式离线批处理或分页 API 扩展。此限制为显式 fail-closed，可能使大型真实库暂时无结果。
- 执行 `python -m pytest -q tests/test_v139_structural_reference_and_chapter_diff.py`；完整回归 `python -m pytest -q`。这些是模拟环境测试，不能代替 PostgreSQL 17 真实性能、权限、Windows GUI 或 508/550 两份真实原始 PDF 的人工双端验收。
- 后续仍须建立 `同章节/父子章节/相邻块` 的全量可查询结构关系索引、语义关联待审展示、章节差异的批处理和在线入口、历史 BUG 版本映射实测，以及 Stage 4/5 的统一混合检索/工作流门禁。
