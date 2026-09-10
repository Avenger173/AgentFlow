# AgentFlow 记忆系统 MEM-1 验收记录

> 状态：MEM-1 出口已通过；MEM-2 待执行
>
> 验收日期：2026-09-10
>
> 数据边界：全部验证使用合成夹具、临时 SQLite、mock 或受控失败 Runtime；未读取客户材料、未调用网络或真实模型。

## 1. 已交付修复

| 问题 | 修复 | 验收事实 |
|---|---|---|
| 最新会话要求被丢弃 | Intent 2200 字符上下文改为保留前缀锚点和最新尾部，中间明确标记省略 | 超过上限的合成对话中，最新要求仍进入 Intent JSON payload，长度保持 2200 字符 |
| 其它项目噪声导致当前项目漏召回 | SQL 先过滤 `scope + enabled + user_confirmed`，在最多 200 条同范围候选内词面排序后返回 Top 3 | 两个 500 条跨项目噪声 required 用例均命中当前项目记忆；Recall@3 为 100%，跨范围泄漏为 0 |
| 失败请求错误刷新使用时间 | 检索与写入 `last_used_at` 分离；mock 回复、LLM 回复、稳定计划和计划修订成功后才写入 | 仅检索不更新；真实 LLM 服务函数的受控失败也不更新；既有 mock 成功路径仍记录使用时间 |

涉及实现：[commander_intent.py](D:/project/AgentFlow/AgentFlow/backend/app/services/commander_intent.py)、[memory_repository.py](D:/project/AgentFlow/AgentFlow/backend/app/database/memory_repository.py)、[commander_memory.py](D:/project/AgentFlow/AgentFlow/backend/app/services/commander_memory.py)、[llm_chat.py](D:/project/AgentFlow/AgentFlow/backend/app/services/llm_chat.py)、[mock_chat.py](D:/project/AgentFlow/AgentFlow/backend/app/services/mock_chat.py)、[plan_revision.py](D:/project/AgentFlow/AgentFlow/backend/app/workflow/plan_revision.py)。

## 2. 阶段门禁

```powershell
cd D:\project\AgentFlow\AgentFlow
backend\.venv\Scripts\python.exe backend\scripts\verify_commander_memory_quality.py --mode gate --gate-profile mem1
```

连续三次通过；本阶段 required 结果签名一致：

`5e1a84461f5aec4384187780f07fd5e19195a88a6d31ffe4eed900761dfab449`

当前评测集为 48 个分类夹具和 3 个实现探针，共 51 项：31 项通过、20 项 `not_supported`、0 项失败。20 项未支持均属于 MEM-2 的 `ConversationWorkingState` 或 Runtime 会话投影目标，不能算作 MEM-1 失败，也不能写成已实现能力。

全量命令 `--mode gate` 仍预期返回 `1`，因为它包含全部未来阶段的 required 用例；MEM-7 前不得把该结果误报为最终通过。

## 3. 回归验证

- `verify_commander_memory_quality.py --mode baseline`
- `verify_commander_memory.py`
- `verify_commander_intent_routing.py`
- `verify_commander_plan_revisions.py`
- `verify_backend.py`
- `python -m compileall -q app scripts`
- `python -m pip check`

以上均通过；未触碰 Qt、SQLite schema 或 UI，因此本阶段没有 Qt 构建和 migration 人工验收项。

## 4. 下一阶段

MEM-2 将新增独立 `ConversationWorkingState`、前向 migration、状态 Reducer 与最小 Runtime 投影。当前聊天摘要、最近 task/plan 指针和独立任务表继续保留，但不再被当作结构化当前工作状态的替代品。
