"""小影科技校招笔试 · 题二 内容任务规划 Agent。

模块分工（见 docs/02-solution.md）：
- schema   输入校验 / 规范化 / 指纹；计划、步骤、动作参数、脚本产物的 pydantic 模型
- store    SQLite 持久化（runs / plans / steps / estimates / ledger / saved_plans）
- plan     计划校验、费用与时间估算（三口径、模拟时钟、最低可行费用）
- tools    七个模型动作：执行前校验 → 执行 → 记账；故障注入
- llm      OpenAI 兼容客户端与 MockLLM（同接口）
- loop     决策循环：模型选动作 → 代码校验/执行 → 观察回模型；步数上限
- trace    追加式事件日志与审阅版导出
- report   从库组装终报、调整说明、面向运营的结论
- main     CLI：入口校验、身份分流、--resume / --new-task、dump
"""
