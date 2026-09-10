# AgentFlow 记忆系统 MEM-0 基线报告

> 状态：初始 MEM-0 快照；MEM-1 已完成，后续结果见 `docs/AGENT_MEMORY_MEM1_ACCEPTANCE.md`
>
> 评测日期：2026-09-10
>
> 数据边界：48 例夹具和 2 个实现探针均为合成、脱敏文本；脚本只创建临时 SQLite，不读取开发数据库、客户材料、模型配置，也不调用网络或模型。

## 1. 可重复运行方式

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_commander_memory_quality.py --mode baseline
```

`baseline` 如实输出通过、失败和 `not_supported`，以建立现状快照；不因已知缺口返回失败码。后续阶段验收使用：

```powershell
.\.venv\Scripts\python.exe scripts\verify_commander_memory_quality.py --mode gate
```

`gate` 只要存在未通过的 required 用例就返回 `1`，当前返回 `1` 属于预期，直到相关阶段修复并通过验收。

## 2. 夹具契约与重复性

| 类别 | 用例数 | 当前结果 |
|---|---:|---|
| 状态更新 | 12 | 12 `not_supported` |
| 任务进度与恢复投影 | 8 | 8 `not_supported` |
| 长会话 Compaction | 8 | 8 通过 |
| 长期记忆检索 | 8 | 7 通过，1 失败 |
| 范围隔离 | 6 | 5 通过，1 失败 |
| 隐私边界 | 6 | 6 通过 |
| **合计** | **48** | **26 通过，2 失败，20 未支持** |

48 个分类夹具之外，报告还包含两项必须通过的确定性实现探针：Intent 是否保留超过 2200 字符上下文后的最新要求、以及 `last_used_at` 是否等待成功计划或回复。合计 50 项检查。

同一提交连续运行三次，40 个 required 检查的结果状态完全一致，签名为：

`4415bbaed8650314d1d74c97ae8e6806b5df0729a297700ae5252d47170eb5ae`

## 3. 当前指标

| 指标 | 基线值 | 解释 |
|---|---:|---|
| required 通过 / 失败 / 未支持 | 20 / 4 / 16 | 未支持项不能计为通过；失败包含 2 个实现探针 |
| 状态字段准确率 | 0.0% | 尚无 `ConversationWorkingState` |
| 任务恢复一致率 | 0.0% | 尚无 Runtime 到会话状态的最小投影 |
| 长期记忆 Recall@3 | 83.33% | 6 个有正向期望的检索例中 5 例命中 |
| 跨范围泄漏 | 0 | 现有 scope 过滤仍阻止错误范围进入返回结果 |
| 检索 P95 | 40.722 ms | 当前小规模、临时 SQLite 记录，仅作 MEM-0 事实记录 |
| 全用例 P95 | 138.134 ms | 含 500 条跨项目噪声的合成压力例 |

性能数字随 Windows 设备和瞬时负载变化，不作为 MEM-0 放行门槛；MEM-5 才以 10,000 条本地记忆的 BM25/Hybrid 延迟作为准入指标。

## 4. 已复现的确定性失败

| 用例 | 现象 | 根因证据 | MEM-1 修复目标 |
|---|---|---|---|
| `retrieval_project_exact_under_noise` | 500 条其它项目较新噪声存在时，当前项目目标未进入 Top 3 | `memory_repository.py` 先调用无 scope 的 `list_long_term_memories(limit=200)`，再用 Python 过滤范围 | SQL 层先按 `scope + enabled + user_confirmed` 过滤，再排序和 LIMIT |
| `scope_noise_does_not_hide_current_project` | 同类压力下当前项目记忆没有被召回，但没有返回其它项目内容 | 同一先 LIMIT 后筛选路径 | 与上例共用 scope-before-limit 修复与回归 |
| `probe_intent_uses_latest_conversation_tail` | 超过 2200 字符时，最新要求没有进入 Intent JSON payload | `commander_intent.py` 对 `conversation_context` 使用前段 `[:2200]` 截断 | 使用结构化状态和最新尾部上下文，且保留固定预算 |
| `probe_memory_usage_waits_for_success` | 仅检索记忆、尚未成功生成计划或回复时，`last_used_at` 已被更新 | `commander_memory.py` 在检索函数内立即调用 `mark_long_term_memories_used` | 把使用时间更新移动到实际成功注入计划或回复之后 |

这两项都属于 required 失败，`--mode gate` 会在 MEM-1 前阻止后续阶段出口。

## 5. 明确记录的未支持能力

`ConversationWorkingState`、冲突覆盖/待确认、任务状态投影、重复事件幂等和重启后会话工作状态恢复目前尚未实现。20 个 `not_supported` 用例用于固定 MEM-2 的验收目标；它们不能被现有聊天摘要、`last_task_id` 或任务表中的独立记录替代。

现有 26 项通过结果证明短期归档摘要、有限近轮窗口、项目会话切换、跨项目读取拒绝、长期记忆开关和敏感内容拦截仍可工作，但不代表完整记忆架构已经达标。

## 6. MEM-0 结论

MEM-0 的目标是建立脱敏夹具、真实失败快照和稳定门禁，而不是让旧实现通过未来能力测试。该目标已完成。MEM-1 已按本报告的 expected 结果修复最新上下文截断、长期记忆 scope-before-limit 和 `last_used_at` 成功语义；本报告保留为修复前的证据快照，不改写失败结果。
