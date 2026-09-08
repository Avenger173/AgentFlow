# LGM5.7 开发者试点手册

状态：真实 Native/Graph 对照、资源测量与故障回退验证已完成；最终准入因资源门槛拒绝。

此手册只面向项目开发者，不是客户功能说明。LangGraph 仍未注册到 Qt、FastAPI 或 RuntimeRouter；
Native Runtime 仍是客户唯一默认路径。

日常代码验收由 `backend/scripts/verify_lgm57_trial_cli.py`、资源探针和 Native 重试回归在临时 SQLite
中自动覆盖，不需要客户或开发者手动执行本手册命令。下列准备步骤只用于已经单独批准的真实开发者试点；
没有真实候选计划时，不应为了运行命令手工创建任务、修改 SQLite 或消耗模型额度。

## 目的

LGM5.7 需要用同一份已完成的 C6.4 只读组合计划，分别准备 Native 基线与 Graph 候选，后续再受控
比较调用集合、事件/交付、来源/产物、恢复、Native 回退和资源基线。准备不代表试点通过，也不会
让客户任务切换到 LangGraph。

## 前置条件

- 在 Windows PowerShell 中运行，项目位于 `D:\project\AgentFlow\AgentFlow`；
- 已使用桌面端或已有流程生成一份状态为 completed 的 C6.4 多材料只读 dry-run 计划；
- 不要把 API Key、材料正文、文件路径或截图中的敏感信息贴入命令输出；
- 当前准备命令不要求打开 LangGraph 开关，也不会调用 Provider。

## 1. 查看候选计划

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe -X utf8 scripts\prepare_lgm57_composition_trial.py --limit 20
```

预期看到一条或多条 `task_id=... plan_id=... plan_digest=... actions=...`。只会显示任务标识、
计划摘要前缀和 Agent/action 类型；不会输出客户目标、材料名、正文、模型名或凭据。

影响：只读取本地 SQLite 中最近完成计划的受控结构；不联网、不调用模型、不写任务历史、不读取
workspace 文件。

若显示“未找到”，说明当前尚无完成的 C6.4 组合计划；不要手改 SQLite 或伪造任务 ID。

## 2. 显式准备 Runtime 对

从上一步复制一条 `task_id`，执行：

```powershell
$taskId = 'task_llm_123abc' # 只替换引号内的示例值，填入上一步实际显示的 task_id
.\.venv\Scripts\python.exe -X utf8 scripts\prepare_lgm57_composition_trial.py --source-task-id $taskId --confirm-prepare
```

不要输入 `<task_id>`：PowerShell 会把尖括号解析为重定向运算符，而不是命令参数。

预期看到 `native_runtime_task_id=...` 与 `graph_candidate_runtime_task_id=...`。二者必须不同，且输出会
说明“未调用模型、未读取材料正文、未联网”。

影响：会在 AgentFlow SQLite 中写入两条内部 Runtime 检查点、候选根步骤的受控摘要 artifact 和事件。
它不会读取 workspace 文件、调用专业 Agent、调用模型、联网或写入客户输出目录。候选的文档、数据、
知识库专业步骤仍为 `pending`。

## 3. 当前停止点

准备完成后不要把这两条内部任务当作客户任务执行。一次已授权的真实候选对照已完成，且故障回退验证
确认 Graph 初始化异常会在专业调用前停止并要求 Native 重试；但独立进程实测为 Native `1569ms / 159MiB`、
Graph `2012ms / 189MiB`。Graph 启动和常驻内存均超过当前相对 Native 的 10% 门槛，最终准入已登记为
`rejected`。

因此当前不得注册客户 Router/API/Qt，也不能声称 LangGraph 已接管实际业务。Native Runtime 仍是唯一客户
默认路径。若未来要重新讨论门槛，必须先以独立资源收益证据和新的用户审批为前置，而不是重复消耗模型
额度重跑同一候选。

## 自动回归

以下命令只供开发维护使用，使用临时目录，不读取客户材料、不调用模型或网络：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe -X utf8 scripts\verify_lgm57_resource_probe.py
.\.venv\Scripts\python.exe -X utf8 scripts\verify_lgm57_native_retry_probe.py
```

第二条会用目录形式的假 checkpoint 注入 Graph 初始化故障。成功只表示 bridge 失败收束和 Native 重试守卫
可用，不表示 LangGraph 获得客户准入。

## 失败信息

请提供命令的脱敏标准输出/错误输出、Git commit、`source_task_id` 与两条 Runtime ID（若已生成）。
不要提供 `.env`、API Key、材料内容、完整任务 JSON、绝对文件路径或数据库文件。
