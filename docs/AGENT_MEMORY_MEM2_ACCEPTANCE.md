# AgentFlow 记忆系统 MEM-2 验收记录

> 状态：MEM-2 出口已通过；MEM-3 待执行
>
> 验收日期：2026-09-10
>
> 数据边界：验证只使用合成夹具、临时 SQLite、mock Runtime；未读取客户材料、未调用网络或真实模型。

## 已交付

| 能力 | 实现边界 | 验收事实 |
|---|---|---|
| 结构化会话状态 | 独立 SQLite 表、Pydantic 协议、revision 乐观并发和有界事件去重 | 用户状态更新、恢复、重复事件和重启快照均由同一 Reducer 验证 |
| 受控字段更新 | 仅从已成功归档的用户消息提取目标、预算、格式、材料范围、页数、时间范围、语言、表格与确认决策 | 12 个状态更新夹具全部通过；模糊预算修改保留旧值并写入 `pending_confirmation` |
| 任务状态投影 | Workflow checkpoint 成功落库后投影 active task、步骤、下一动作和 open item | queued/running/paused/resumed/completed/failed 的 8 个恢复夹具全部通过，重复事件 revision 增量为 1 |
| 已验证结果边界 | 只有 `mode=runtime` 且有登记 artifact 的完成 checkpoint 才更新 `latest_verified_result` | API 回归识别出并修正 dry-run 虚拟 artifact 被误认成交付的风险 |
| 前向迁移 | 旧库不重写会话、消息或长期记忆；状态行首次写入时惰性补齐 | 独立脚本验证摘要、消息、task/plan 指针和长期记忆数量保留，并覆盖真实 Runtime checkpoint 投影 |

涉及实现：[conversation.py](D:/project/AgentFlow/AgentFlow/backend/app/schemas/conversation.py)、[conversation_repository.py](D:/project/AgentFlow/AgentFlow/backend/app/database/conversation_repository.py)、[conversation_working_state.py](D:/project/AgentFlow/AgentFlow/backend/app/services/conversation_working_state.py)、[task_repository.py](D:/project/AgentFlow/AgentFlow/backend/app/database/task_repository.py)。

## 阶段门禁

```powershell
cd D:\project\AgentFlow\AgentFlow
backend\.venv\Scripts\python.exe backend\scripts\verify_commander_memory_quality.py --mode gate --gate-profile mem2
```

48 个分类夹具和 3 个既有探针全部通过：41 个 required、10 个 diagnostic；状态字段准确率 `1.0`，任务恢复一致性 `1.0`，跨范围泄漏数 `0`。

## 回归验证

- `verify_conversation_working_state.py`：旧库前向 migration、惰性状态行、重复写入与真实 Runtime checkpoint。
- `verify_commander_memory.py`：FastAPI 聊天后从恢复 API 读取同一会话的活动任务；dry-run 不登记验证结果。
- `verify_commander_memory_quality.py --mode gate --gate-profile mem2`
- `verify_commander_intent_routing.py`
- `verify_commander_plan_revisions.py`
- `verify_backend.py`
- `python -m compileall backend/app backend/scripts`
- `python -m pip check`

以上均通过。本阶段没有 Qt 改动，未运行 Qt 构建或人工桌面验收。

## 下一阶段

MEM-3 统一 Intent、Planner 和 Reply 的 `ContextEnvelope` 与模型感知预算；不得把当前“Prompt 已读同一快照”误写成三条模型路径已经共享统一预算或动态裁剪。
