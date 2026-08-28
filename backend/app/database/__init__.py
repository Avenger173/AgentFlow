"""AgentFlow 本地持久化模块。

当前阶段先使用 SQLite 保存 dry-run 任务状态、step 级结果、日志和权限审计；真实任务
执行器接入后再扩展 artifacts、tool_calls 等表。
"""
