# 项目内置 Skills

这七项能力由 `app/skills/catalog.py` 固定注册，Markdown 描述不会自动注册可执行代码。

管理员可在“Skills 能力与离线验证”页面加载合成示例并运行，或调用 `scripts/run_skill.py`。离线快照不等于真实数据库事实，执行结果也不代表人工生产签收。

原问答路由和发布门禁继续使用其受控的在线能力；不要让 Open WebUI 用户上传的文档、票内容或 Skill 文本改变工具权限。

核心入库流程见 `docs/CORE_BACKEND_CN.md`。运行阶段回执记录在用户配置目录，不纳入业务 SQL 数据或向量证据。
