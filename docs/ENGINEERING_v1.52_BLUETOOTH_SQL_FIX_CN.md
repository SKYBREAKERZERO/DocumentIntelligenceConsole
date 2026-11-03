# v1.52 Bluetooth Audio 精确事实检索修复与现场验收

## 真实现场观测（用户提供的 PowerShell 结果）

- `documents`：`Bluetooth Audio function spec v2.52.pdf`，版本 `v2.52`，document_id=`21MM:UNSPECIFIED:NAME:a349727f3f5156c17204@v2.52`。
- `documents`：`550_Bluetooth Audio Function Spec_Cover_Sheet_Ver.2.50改(電子承認).xlsx`，版本 `v2.50`，document_id=`21MM:EXCEPT_NA:550@v2.50`。本文件不是 SPEC_BODY。
- v1.51 `/api/v1/retrieval` 真实返回 `SPEC_LOOKUP`、`IndeterminateDatatype`、零 evidence。
- 先前按名称模糊检索的 20 条 Bluetooth 记录不能证明 550 PDF 已绑定到指定规格书版本。

## 代码修复

- v1.51 版本参数 `%s IS NULL` 的 SQL 类型推断缺陷改为 `%s::text IS NULL`，并使文本版本值 `%s::text`、spec-code 数组 `%s::text[]` 显式定型。
- 严格区分已导入正文和核验通过的 550 MAIN SPEC_BODY 版本。存在 PDF 不等于绑定到 `specification_versions`。
- SQL 查询失败也允许独立、只读地检查 `documents` 中的文件记录，但不会因此把文件提升为可信式样版本。
- 无正文版本绑定时禁止生成“已认证目录”或“两个历史版本变更”。
- 裸文件名快速路由以及 Windows PowerShell 5.1 UTF-8 BOM 兼容性改进。

## Windows 现场步骤

1. 将 v1.52 完整包解压到新的 `C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.52_CN_FULL`。
2. 使用 Python 3.12 虚拟环境安装依赖；正常停止 v1.51 Backend 并确认 18080 已释放，然后启动 v1.52。
3. 在工程根目录执行 `python .\scripts\diagnose_spec_registration_v152.py --scope 21MM --term "Bluetooth Audio" --spec-code 550`。
4. 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_v152_spec_facts.ps1`。
5. `IndeterminateDatatype` 不应再出现；若诊断为 `IMPORTED_SPEC_BODY_WITHOUT_VERSION_LINK`，应先检查数据导入及规格版本绑定，**不得自行把 v2.50 Cover Sheet 作为旧正文**。

## 生产验收边界

本包只完成本地代码和模拟测试，无法访问用户 Windows PostgreSQL、Qdrant 与客户资料做端到端验收。现场验证前不宣称 550 与 v2.52 的版本关联已通过。无自动数据库迁移、重索引或写入。
