# 典型企业场景合成测试集

所有内容均为编造，编号 990xxx 不代表真实企业式样。只允许导入隔离验收库。

包含蓝牙音频两个版本、音频路由、触摸屏手套操作，以及 QA/BUG 表格。每份 PDF 有封面、版本、章节和物理页码。

PDF 与规范 JSON 从同一受控定义生成；这用于验证检索、版本和关联，不等于复杂企业 PDF 解析质量认证。

生成：python scripts/live_acceptance/synthetic_business_suite.py generate（需 reportlab/openpyxl）。
隔离验收：python scripts/live_acceptance/synthetic_business_suite.py run（必须已有 15432 隔离实例）。
