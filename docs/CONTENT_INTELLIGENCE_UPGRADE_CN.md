# 内容智能与现象检索增量升级

本版本保留现有目录、接口、数据库和 Skills，新增以下通用能力：

- 一键建库同时安装 `ei_requirement_units`、`ei_content_relations`、版本映射、回填收据和 Stage-1 v2 派生表。
- 后续 SPEC_BODY 入库自动尝试生成内容单元；现有文档可用 `scripts/build_engineering_intelligence.py` 增量回填。
- 内容单元保存 `document_id/content_id/section/page/start/end/text_hash`，不截断原文建立虚假证据。
- 文档级引用、正文明确引用、入站/出站关系和已审核内容关系分别标识，不把语义相似当正式依赖。
- 式样目录、身份、目录、正文、位置和关系采用确定性 PostgreSQL 输出，不经过模型引用格式门禁。
- “总结/分析、不要正文”使用 `SPEC_SUMMARY`：由正文位置确定性归纳，不占用 Ollama；每条结论携带章节、页码和 content_id，CPU-only 机器也能快速返回。
- 测试人员只输入现象也会进入 `PRE_TICKET_CHECK`：先查 QA/BUG，再继续检索 21MM/24MM/COMMON 式样片段。
- 历史票与式样候选同时返回；相似度仅用于发现候选，不证明同一问题、根因或式样符合性。
- Open WebUI 结果继续显示 section/page/Sheet/content_id；新增需本机 Bearer 认证的精确定位接口 `/api/v1/intelligence/content/{scope}/{content_id}`，不为方便跳转而匿名暴露企业正文。

## 当前本机验收

- 21MM：2,215 个当前主本体已有完整回填收据，`ei_requirement_units` 共 328,784 行。
- 24MM：159 个当前主本体已有完整回填收据，`ei_requirement_units` 共 144,978 行。
- 24MM/150 多轮“分析而非正文”返回结构化摘要及位置，不触发引用门禁。
- 24MM/150 CPU-only 实测约 1 秒返回，不再因大模型生成超时而出现空白或 `response incomplete`。
- “蓝牙连接后切换用户，Profile没有恢复”返回 8 个历史票候选和 8 个式样片段候选。
- “508 的 1.2.1 引用 111，对应 111 哪段内容”返回来源 section/content_id 和目标片段；目标被明确标为 `SEMANTIC_CANDIDATE`、`human_confirmed=false`。
- 迁移为新增式；执行前备份位于本机 `local_backups/`，交付 ZIP 不包含备份、凭据、运行日志或生产数据。

## 证据边界

- 明确“式样编号 + 章节”且目标章节唯一时，才可建立确定性章节连接候选。
- 仅有式样编号的正文引用属于文档级引用，目标内容只能列为候选。
- 仅向量相似的内容必须标记“相关候选”，不能称为正式引用。
- 当前严格回填没有生成可冒充为已确认关系的 `ei_content_relations`：语料中没有同时满足“明确目标章节 + 唯一目标内容单元”的无歧义连接。文档级明示引用仍可查询，目标片段只作为候选展示。
- 是否符合式样、是否应起 BUG 仍需适用版本映射、完整条件与人工复核。
