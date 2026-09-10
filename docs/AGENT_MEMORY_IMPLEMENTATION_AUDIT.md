# AgentFlow 记忆管理实现对照与改进

> 对照基线：`docs/Agent开发技术要点（持续更新）.md` 当前第 5 章
> 审计日期：2026-09-10
> 原则：以真实调用链和离线回归为准，不把框架名或规划项当作已实现能力。

后续实施顺序、数据契约和量化出口统一以 `docs/AGENT_MEMORY_DEVELOPMENT_PLAN.md` 为准；修复前的 MEM-0 夹具、指标和失败证据见 `docs/AGENT_MEMORY_MEM0_BASELINE.md`，MEM-1 与 MEM-2 验收见 `docs/AGENT_MEMORY_MEM1_ACCEPTANCE.md`、`docs/AGENT_MEMORY_MEM2_ACCEPTANCE.md`。

## 1. 当前真实架构

### 1.1 短期记忆

调用链为 `POST /api/chat` -> `prepare_conversation()` -> `get_conversation_context()` -> Commander/LLM Prompt -> `persist_successful_conversation_turn()`。

- SQLite 的 `commander_conversations` 保存会话范围、摘要、材料引用、最近 task/plan 指针和摘要水位。
- `commander_conversation_messages` 保存脱敏且有长度上限的用户/助手消息；完整任务执行状态仍由 `workflow_runs`、事件和 checkpoint 表负责。
- `commander_conversation_working_states` 保存有 revision 的会话级当前目标、有效约束、待确认字段、open item、活动任务和最小已验证结果。用户消息只通过白名单 Reducer 更新字段，Runtime checkpoint 成功落库后才投影任务事实。
- `conversation_id + project_scope` 共同隔离会话；范围变化会创建新会话，不复用旧材料。
- 模型上下文采用一次性 `ContextEnvelope`：结构化工作状态优先，其后是已确认长期记忆、早期确定性摘要/材料引用/task-plan 指针和 token 预算内的完整最近轮次。客户可分页回看归档，模型不会读取全部历史；Intent、Planner 审计和 Reply 不再各自截断或重选上下文。

### 1.2 长期记忆

调用链为运行偏好 `memory_enabled` -> `retrieve_commander_memory_context()` -> `search_long_term_memories()` -> 最多 3 条短摘要注入计划和模型上下文。

- `long_term_memories` 保存用户偏好、项目约束和经验，支持 global/project 命名空间、启停、编辑、删除、来源任务和最近使用时间。
- 写入必须由用户明确确认；任务结束后只生成可编辑候选，不会把聊天内容静默升级成永久画像。
- 当前检索为标签、标题、摘要的可解释词面打分，并为 global 用户偏好提供很小的基础分；不是向量 + BM25 混合检索。
- 程序性记忆不放在该表中，而由根 `SKILL.md`、Agent 定义和 Workflow/Node Contract 承担。

## 2. 对照矩阵

| 文档要求 | 当前实现与证据 | 状态 | 结论 |
|---|---|---|---|
| 最近对话、回复进入短期记忆 | `conversation_repository.py` 持久化 user/assistant，异步 Runtime 追加最终交付 | 达标 | 失败的模型回合不写入，避免伪造已完成对话 |
| 保存工具结果和执行中间态 | 完整 Tool/Step 状态留在任务表；成功 checkpoint 投影 active task、步骤、下一动作和 open item 到会话状态 | 达标 | 原始日志不进入 Prompt；无会话关联的独立工具任务不伪造会话状态 |
| 摘要 + 最近原文 | Repository 保留约 18k token、最多 20 条原文；模型注入改由 `ContextEnvelope` 按动态预算选择确定性摘要和完整 user/assistant 对 | 达标 | 已核验窗口按公式分配，未知 Runtime 使用 16,384-token 回退；均是本地估算，不等同于 Provider usage |
| 摘要保留目标、约束、TODO、标识符 | 摘要按 `[目标]/[约束]/[待办]/[结果]` 分类；Working State 注入目标、有效约束、未完成事项、active task/plan 和已验证 artifact | 达标 | 当前为确定性抽取，不额外消耗 LLM；复杂语义更新仍可能漏判 |
| 明确修改覆盖旧结构化状态 | 白名单 Reducer 覆盖预算、格式、材料范围、数量与时间范围；不明确变更进入待确认 | 达标 | 当前只支持已登记字段，不把自由文本误当成结构化事实 |
| 会话/线程隔离与恢复 | 稳定 conversation_id、project_scope、SQLite 归档、Working State revision 和 Runtime checkpoint | 达标 | 重启 JSON 与重复 checkpoint 均由离线夹具验证；LangGraph 不是聊天主链路 |
| 清理、归档和沉淀 | 消息可分页归档，长期记忆可删除；会话没有 TTL、归档状态或删除 API | 未达标 | 长期桌面使用会积累数据，隐私和体积策略不完整 |
| 长期记忆外部持久化 | SQLite 表、global/project 命名空间、跨会话按需读取 | 达标 | 不依赖上下文窗口存活 |
| 语义/情景/程序性记忆 | 偏好/约束对应语义，experience + source_task_id 对应轻量情景，SKILL/Workflow 对应程序性 | 基本达标 | 情景记忆的文件变更、工具轨迹仍在任务历史，不在通用记忆检索中 |
| RAG 式按需检索 | 开关开启后按 query 取 Top 3，不全量注入 | 达标 | 数据规模小时方案合理 |
| 向量 + BM25 混合排序 | 记忆表当前只有词面启发式打分 | 未达标 | 先建立召回评测和规模阈值，再复用现有知识库 FTS/向量能力 |
| 压缩前自动沉淀长期记忆 | 任务后可推导候选，保存前必须确认；未在 compaction 前触发 | 部分达标 | 不采用静默永久写入是正确的隐私取舍，可在压缩前自动生成“待确认候选” |
| 记忆总开关和用户控制 | 默认关闭；关闭时不读取记忆表；支持启停、编辑、单删和按范围清空 | 达标 | 隐私边界优于无提示自动记忆 |

## 3. 本轮修复

此前真正影响连续性的不是“数据库没有数据”，而是模型实际只看到最近 6 条消息、每条最多 420 字，早期摘要又是 1400 字内的截断流水账。数据库虽然保存了更多内容，Prompt 注入阶段仍会丢掉大量可用上下文。

本轮完成：

- 最近窗口由固定 8 条升级为“最多 20 条 + 约 18k token”双预算，短消息可以保留更多轮，长消息会提前压缩。
- Prompt 不再二次裁成 `6 x 420` 字，改为注入预算内的完整脱敏消息。
- 单条会话归档上限由用户 1800 / 助手 2200 字统一提高到 8000 字，减少长要求和长交付在下一轮消失。
- 摘要改为目标、约束、待办、结果四类，并保留 task_id；上下文公开摘要水位与估算 token 数，便于回归和诊断。
- 保留完整任务状态与 Tool trace 的独立存储，不把原始日志、隐藏推理或未校验 Tool 输出直接塞进聊天 Prompt。
- 新增独立 Working State：已成功归档的用户输入以白名单覆盖/合并字段，含糊修改进入待确认；Runtime checkpoint 才更新任务进度，只有真实 Runtime 已登记 artifact 才可登记验证结果。

## 4. 后续优先级

1. **P1：保留期和删除能力。** 增加会话删除、归档与可配置 TTL，默认不自动删除客户仍在使用的记录，并提供按 project_scope 清理和审计计数。
2. **P1：压缩前候选。** 在旧消息进入摘要前，只生成待确认长期记忆候选，不直接写长期表；候选应去重、可编辑、可拒绝并记录来源会话/任务。
3. **P2：记忆检索评测。** 先建立中文偏好、同义表达、跨项目隔离和误召回数据集；当记忆规模或 Recall@3 证明词面方案不足时，再接 FTS5 BM25 + 现有向量索引做混排。
4. **P2：LLM 摘要准入评估。** 只在信息保留率有量化提升、Provider 失败可回退且 usage/cost 可记录时，再与现有确定性摘要比较；否则保持确定性方案。

## 5. 简历可用表述

- 设计并实现 Agent 分层记忆架构：基于 SQLite 的会话隔离与可恢复归档、统一 ContextEnvelope 动态预算、结构化摘要、可版本化的当前工作状态，以及 global/project 命名空间的用户确认式长期记忆。
- 将 Agent 记忆与 Workflow 状态解耦：会话层只注入受控摘要和最小工作状态，任务进度、Tool trace、checkpoint 和审计事件独立持久化；只有可信 checkpoint 可投影任务事实，避免原始日志和虚拟产物污染 Prompt。
- 建立隐私优先的记忆治理：长期记忆默认关闭，支持候选复核、显式确认、敏感信息/绝对路径拦截、启停编辑删除和跨项目隔离。
- 采用评测驱动的检索演进策略：小规模记忆先使用可解释本地词面排序，预留基于 FTS5 BM25 与向量索引的混合召回升级路径。

## 6. 验证基线

- `python -m compileall -q backend/app backend/scripts`
- `python backend/scripts/verify_commander_c6_conversation.py`
- `python backend/scripts/verify_commander_memory.py`
- `python backend/scripts/verify_conversation_working_state.py`
- `python backend/scripts/verify_commander_memory_quality.py --mode gate --gate-profile mem2`
- `python backend/scripts/verify_commander_memory_proposals.py`
- `PYTHONUTF8=1 python .../skill-creator/scripts/quick_validate.py .`

以上均使用临时 SQLite 或静态文件检查，不调用真实模型、不联网、不读取客户材料，也不修改现有客户会话数据。
