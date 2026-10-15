# Stage 1 v1.23 — DCM 通信与初期状态来源审查

## 目标与边界

本版为**导出的 Pipeline JSON** 增加 DCM（Data Communication Module）通信相关状态/时序**来源信号**诊断。其用途是协助人工审查车载 Telematics、APP 按钮、DCM 通信前后、初期值、通信失败及例外语句。新模块 `app/engineering_intelligence/stage1_dcm.py` **不是** DCM 协议解析器、状态机或自动判定引擎；绝不自动宣称 DCM 已通信、按钮应显示、UI 已安装或适用地区例外。

- 识别带有 `DCM` / `ＤＣＭ` 独立缩写的原始物理文本行（含 `DCM通信`）；不将 `ABCDCM20` 等子串视作 DCM。
- 对同一行提取**词面提示**：`INITIAL`（初期/默认状态）、`BEFORE`（明确的 DCM 通信前）、`AFTER`（明确的 DCM 通信后）、`SUCCESS`、`FAILURE`；允许多个提示在同一行并记为 `MULTIPLE_PHASES_UNRESOLVED`，不作简化裁决。
- 对**不直接含 DCM**、但明确提到 Telematics/APP 按钮及初期状态的行，只在同一个来源章节另有 DCM 行时作为 `DCM_RELATED_UI_SOURCE` **邻近上下文候选**保留；绝不确认两个描述属于同一功能或状态机。
- 同一章节存在多种时序阶段时报告 `DCM_MULTIPHASE_SECTION_REQUIRES_REVIEW`，只表示**同节共现**，不能证明事件排序、状态迁移、翻译等价或因果。
- 对含 `ただし`/`except`/`unless` 的行报告例外范围未核实；对 `なし`/`非表示`/`not` 等否定表达报告否定的对象和作用域未核实；不按这些提示推断真实操作规则。
- 每个 DCM 匹配行建立 `DCM_SOURCE` 脱敏指针，带有同节 Telematics/APP 初期信息的行则使用 `DCM_RELATED_UI_SOURCE`：1-based JSON content 索引、Unicode 码位区间、SHA-256、页面和审核标记。不把原文、文件名或任意章节标题写入诊断报告。默认限制分析 1000 个 content、100 万字符、8000 行，超限时 `dcm_probe_complete: false`。
- `SUPPORTING` 类型文件不做 DCM 要求抽取；不能把封面或 QA/BUG 表格视为正式要求。

## 完整审查命令（Windows PowerShell）

```powershell
Set-Location 'C:\mission\Python\DocumentIntelligenceConsole_Enterprise_v1.23_CN_FULL'

.\.venv\Scripts\python.exe .\scripts\review_stage1_pipeline_json.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report-out 'C:\authorized\stage1_v123_dcm_review.json'

.\.venv\Scripts\python.exe .\scripts\verify_stage1_pipeline_evidence.py `
  --json 'C:\authorized\DCM.pipeline.json' `
  --report 'C:\authorized\stage1_v123_dcm_review.json'
```

要求 Pipeline JSON 顶层遵守既有 `document`/`metadata`/`sections`/`contents` 结构与 21MM/24MM/COMMON 项目范围。原始 DCM PDF 若未导出 Pipeline JSON，须先使用经授权的工程导入路径制作导出文件，不能拿任意示例 JSON 代替生产来源证明。报告不可覆盖原文；离线复核检查输入与报告一致性，**不是**原始 PDF 原生布局或 SQL 入库证明。

## DCM 重点人工审核矩阵

| 核对内容 | 只读诊断可提供 | 人工仍需确认 |
|---|---|---|
| Telematics / APP 钮初期值 | 含 DCM 的行，初期标记和来源字符范围 | 哪个产品/地区/软件版本/上电模式定义初期值 |
| DCM 通信后安装/显示 | 通信后标记和同行动作词面存在 | 是否成功通信、是否已装、何时更新、按钮是否持久化 |
| 通信前后条件差异 | 同节多阶段风险，分别回源 | 跨行条件是否继承、先后顺序、是否同一用例 |
| 通信失败 / NA 例外 | 失败或例外词面提示 | fallback、NA 专用条件、动作是否禁止/豁免 |
| JA/EN 版本 | 保留 v1.22 原有独立来源证据 | 双语是否互译、哪一版本权威 |
| PDF 跨页状态表 | 保留 v1.22 双页片段指针 | 原生几何、表头延续、合并单元格及条件延续 |

## 工程验收状态

所有 v1.23 DCM 定向测试均使用合成字符串，且离线运行。不包含经确认的真实 DCM PDF / JSON、人工 Gold 标签或审批结果；不与 PostgreSQL、Qdrant、Ollama 通信，也不更改任何审批、数据表或版本关系。业务方需要另行提供经授权原始文件、保留来源 SHA 的入库凭据与人工标注，才能验收真实准确率。**本次交付为完整源码 ZIP，不是 EXE。**
