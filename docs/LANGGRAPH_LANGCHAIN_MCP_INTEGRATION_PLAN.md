# LangGraph、LangChain 与 MCP 平台集成计划

最后更新：2026-09-04

状态：**LGM0、LGM1、LGM2 与 LGM3 已完成。MCP、LangGraph 与 LangChain 依赖已在开发后端环境锁定；项目已提供一个默认停用、固定边界的 Wikimedia 公开资料 MCP 客户闭环，以及一张仅用于开发验证的 LangGraph 确定性测试图。Native Runtime 仍是唯一默认执行路径；尚未迁移客户任务，也未引入通用远程 MCP 连接。**

本文是三项技术进入 AgentFlow 的实施依据。目标不是为简历增加名词，而是用成熟框架和开放协议改善复杂工作流恢复、外部工具接入、组件复用和长期可维护性。任何阶段只有产生可验证的客户价值并通过回归后，才能写入“已实现”状态。

相关文档：

- [Agent 工程指南](AGENT_ENGINEERING_GUIDE.md)
- [DeepSeek Harness 集成方案](DEEPSEEK_HARNESS_INTEGRATION_PLAN.md)
- [本地知识库规格](KNOWLEDGE_BASE_PRODUCT_SPEC.md)
- [开发路线](DEVELOPMENT_ROADMAP.md)
- [当前进度](PROJECT_STATUS.md)

## 1. 结论

三项技术不处于同一层，也不互相替代：

| 技术 | AgentFlow 中的定位 | 首期决定 |
| --- | --- | --- |
| MCP | 外部工具、资源和系统的标准连接协议 | 优先实现官方 Python SDK 驱动的 MCP Tool Gateway |
| LangGraph | 复杂、有状态、可恢复工作流的可选执行后端 | 先以知识库深度任务做影子运行和小范围试点 |
| LangChain | 模型、消息、Tool 等组件的标准化工具箱 | 只引入被实际使用的组件，不替换稳定业务服务 |

总体原则：

1. AgentFlow 继续拥有 Commander、`task_id`、会话、权限、审计、模型路由、产物和客户交付协议。
2. LangGraph 只负责获准工作流内部的状态图、节点调度、Checkpoint、Interrupt 和流式事件。
3. MCP 只经过 AgentFlow 的统一 Tool Gateway、权限裁决和审计，不能由模型或外部 Runtime 绕过控制面直连。
4. LangChain 只在减少真实重复代码时使用，不为凑技术栈重写 ModelGateway、Hybrid RAG 或现有 Tool。
5. Native Runtime 始终保留。新后端必须可按任务和图版本灰度启用，并能在未产生副作用前安全回退。

## 2. 要解决的现有不足

### 2.1 Runtime 复杂度持续增长

现有 Native Runtime 已完成文档、数据、知识库、PPT 和总指挥组合任务，但复杂分支、并行、权限中断、Map-Reduce、恢复和父子任务逻辑逐渐集中到大型模块中。继续手工扩展会提高以下风险：

- 状态转移分散，新增阶段容易遗漏恢复入口；
- 并行节点失败后，已完成分支和待恢复分支难以统一表达；
- 权限批准前后的幂等边界需要每条业务链重复实现；
- UI 事件、SQLite checkpoint 和 Runtime 内部状态容易出现三套事实；
- 新专业 Agent 容易复制流程控制代码，而不是只实现业务节点。

LangGraph 的价值是把上述复杂编排收敛为显式 State、Node、Edge、Checkpoint 和 Interrupt。它不替代业务 Tool，也不自动解决 AgentFlow 的权限和交付问题。

### 2.2 外部能力接入缺少统一协议

当前外部能力主要由项目内专用 Adapter 接入。对于稳定且与产品强绑定的 Provider，这种方式仍合理；但如果未来连接客户数据库、协作平台、云盘、搜索或行业系统，每个连接都单独设计发现、参数、调用、认证和生命周期，会形成大量重复代码。

MCP 用于统一：

- 能力发现；
- Tool 参数 schema；
- 调用与结构化结果；
- 本地 stdio 与远程 Streamable HTTP 传输；
- 服务端版本、能力和状态管理。

MCP 不能替代：

- 客户权限确认；
- Tool 风险分级；
- 业务结果验证；
- 来源真实性判断；
- 产物回读；
- 密钥安全存储。

### 2.3 已有组件与生态难以复用

AgentFlow 已有多供应商 ModelGateway、Tool Calling 和自研 RAG。全面迁移到 LangChain 没有收益，反而可能引入协议重复和行为变化。但以下场景值得选择性复用：

- LangGraph 节点需要标准消息、Tool 或 Runnable 类型；
- MCP Tool 需要转换为图节点可使用的 Tool；
- 新 Provider 有成熟且稳定的 LangChain Adapter，而 ModelGateway 尚无对应实现；
- 新工作流需要 middleware、动态 Tool 过滤或结构化输出策略。

## 3. 目标架构

```text
Qt Desktop
  -> FastAPI / WebSocket
      -> Commander + AgentFlow Control Plane
          - conversation / memory
          - workflow_plan / action admission
          - permission / audit
          - model route / secret store
          - artifact / delivery / verifier
          -> RuntimeRouter
              -> NativeRuntime                 [默认]
              -> LangGraphExecutionBackend     [可选]
              -> DeepSeekHarnessBackend        [可选，既有 H 线]
          -> ToolGateway
              -> NativeToolAdapter
              -> MCPToolAdapter
                  -> MCP Client Manager
                      -> local stdio server
                      -> remote Streamable HTTP server
          -> ModelGateway
              -> configured provider/model
```

### 3.1 控制面所有权

| 能力 | 最终所有者 | 外部框架的职责 |
| --- | --- | --- |
| 客户请求、会话和长期记忆 | AgentFlow | 只接收本次获准的最小摘要 |
| `task_id`、计划版本和动作准入 | AgentFlow | 通过映射 ID 执行已批准图 |
| Provider、模型和 API Key | ModelGateway / SecretStore | 只接收当前调用所需的运行时对象 |
| 权限策略和人工确认 | AgentFlow Governance | LangGraph Interrupt 或 MCP 请求只映射为等待状态 |
| 工作流内部图状态 | LangGraph | 保存最小、可序列化、无凭据状态 |
| MCP 连接与能力发现 | MCPGateway | 受 AgentFlow 配置、权限和生命周期控制 |
| Tool 结果验证和脱敏 | AgentFlow ToolGateway | 外部结果不能直接进入客户回复 |
| 产物、来源和交付卡 | AgentFlow | 框架只返回候选结果和状态事件 |

## 4. MCP 设计

### 4.1 SDK 与传输选择

- 协议核心优先使用官方 MCP Python SDK，避免把 MCP 生命周期绑定到 LangChain。
- 本地首期使用 `stdio`，由 AgentFlow 启动并监管受控子进程。
- 远程首期只支持 `Streamable HTTP`；不为新连接实现已经被替代的 SSE 传输。
- SDK 必须在实施时锁定经过验证的精确版本，并记录其支持的 MCP 协议版本；升级必须重跑兼容性测试。
- `langchain-mcp-adapters` 只作为 LangGraph/LangChain 节点适配候选，不能成为权限和连接配置的唯一所有者。

### 4.2 MCPGateway 模块边界

建议新增：

```text
backend/app/mcp/
  contracts.py          # 非敏感连接、能力、调用和结果协议
  config_store.py       # 仅保存 server 配置和 secret_ref
  client_manager.py     # session、启动、重连、关闭和取消
  capability_cache.py   # 有版本和 TTL 的能力目录
  tool_adapter.py       # MCP Tool -> AgentFlow AgentTool
  result_guard.py       # 大小、类型、脱敏和注入边界
  audit.py              # 连接/发现/调用事件投影
```

MCP Tool 对外统一命名为：

```text
mcp.<server_id>.<tool_name>
```

名称必须经过 ASCII 规范化和冲突检查；原始名称只留在受控元数据。模型只看到当前任务允许使用的有限 Tool 描述，不能一次加载全部 MCP Server 的全部能力。

### 4.3 首期 MCP 能力范围

首期只开放 `Tools`：

- `list_tools` / 能力发现；
- Tool JSON Schema 读取与规范化；
- `call_tool`；
- 文本和受限结构化结果；
- 超时、取消、进程退出和一次受控重连；
- 调用审计与客户可见阶段事件。

首期不开放：

- Resources 自动注入模型上下文；
- Prompts 自动改写 Agent 系统提示；
- Sampling 让 MCP Server 反向调用客户模型；
- Elicitation 绕过 AgentFlow UI 向客户索取信息；
- 任意二进制内容、无限订阅或后台常驻通知；
- 未经确认安装 npm/pip 包或执行客户提供的任意命令。

### 4.4 权限与安全

本地 stdio MCP 本质上会启动进程，因此首次添加和每次命令变化都视为命令执行权限：

- 配置使用结构化 `command + args`，禁止拼接 shell 字符串；
- 可执行文件解析为绝对路径并校验允许目录；
- `cwd` 固定为 MCP 专属空目录或明确获准目录；
- 子进程环境使用白名单构造，不继承项目 `.env`；
- Key 只保存为 DPAPI `secret_ref`，不写入 JSON、日志或 Tool 描述；
- stdout 只用于协议，诊断输出进入受限 stderr 日志；
- 设置启动、发现、单次调用、空闲和关闭超时；
- 限制文本长度、结构深度、列表数量和二进制大小；
- Tool 描述与返回内容均视为不可信输入，不能改变系统权限或计划；
- 远程 MCP 额外触发联网权限，并校验 HTTPS、重定向和目标主机。

### 4.5 MCP 客户体验

MCP 是平台连接能力，不新增“某某 MCP Agent”。首期在插件管理或系统设置中增加紧凑的“MCP 连接”区域：

- 连接名称、类型、本地/远程地址摘要；
- 已启用/已停用/需要授权/连接失败；
- 最近检测时间与能力数量；
- 测试连接、查看 Tools、启用、停用、删除；
- 每个 Tool 的权限、风险和是否允许总指挥自动选择。

普通客户不需要理解 JSON-RPC。总指挥只在任务确实需要外部系统且已有可用连接时提出调用；写入、联网和命令类调用仍按权限策略确认。

## 5. LangGraph 设计

### 5.1 ExecutionBackend 适配

建议新增 `LangGraphExecutionBackend`，实现与 Native/DeepSeek Harness 一致的稳定接口：

```text
execute_task(request, event_sink) -> ExecutionResult
resume_task(task_id, resume_input, event_sink) -> ExecutionResult
cancel_task(task_id) -> ControlResult
close() -> None
```

`RuntimeRouter` 按以下事实选择后端：

- Agent/action 是否声明支持该后端；
- 图版本是否已通过准入；
- 当前任务是否只读或包含副作用；
- 客户是否启用试验功能；
- 依赖和 Checkpointer 是否健康；
- 是否已经执行 Tool 或产生副作用。

禁止让 LLM 自己选择 Runtime。Router 的选择必须是确定性策略并写入任务快照。

### 5.2 状态模型

首期 Graph State 只允许 JSON 可序列化字段：

- AgentFlow `task_id`、`conversation_id` 和计划版本；
- `graph_id`、`graph_version` 和当前阶段；
- 已准入 step/node ID；
- 材料、知识库、数据集的稳定引用和版本哈希；
- 有限的来源 ID、子任务 ID、结果摘要和错误分类；
- 权限 interruption 的不透明请求 ID；
- 预算已用/剩余、重试次数和取消标志。

不得进入 checkpoint：

- API Key、Authorization Header；
- ModelGateway Runtime 对象、数据库连接、文件句柄；
- 未经裁剪的整份文档、DataFrame、Embedding 或模型原始思考；
- 本机绝对路径和客户未选择的材料。

### 5.3 Checkpoint 与现有 SQLite 的关系

- 隔离试验先使用内存 Checkpointer，不写客户数据。
- 真实本地试点使用官方 SQLite Checkpointer，并放在独立的 `data/langgraph-checkpoints.db`，避免与 AgentFlow 主任务事务相互阻塞。
- AgentFlow 主数据库只保存 `task_id <-> thread_id/checkpoint_id/graph_version` 映射、摘要和客户可见状态。
- `thread_id` 使用 AgentFlow 生成的不透明 Runtime task ID，不使用文件名、用户名或提示词。
- 禁止为了保存 DataFrame 等对象启用 pickle fallback；节点应保存稳定引用，由 Tool 在执行时重新受控读取。
- Graph 升级后，旧任务固定使用创建时的 `graph_version`；不能用新图静默恢复旧 checkpoint。

### 5.4 Interrupt 与副作用

LangGraph Interrupt 映射到 AgentFlow 的 `waiting_permission` 或 `waiting_input`：

1. 节点先完成参数构造和幂等键生成；
2. AgentFlow 保存权限请求；
3. Graph `interrupt` 只保存不透明请求 ID 和客户可读摘要；
4. 客户批准、拒绝或编辑后，经 AgentFlow API 调用 `resume_task`；
5. 节点从头重跑时，通过幂等键确认副作用尚未执行或读取已完成结果。

任何不可重复副作用都必须放在 Interrupt 之后，并具备“准备、执行、验证、登记”四段状态。产生副作用后不允许静默换到 Native Runtime 再执行一次。

### 5.5 流式事件映射

LangGraph 的 `updates`、`tasks`、`tools`、`messages` 和 lifecycle 事件只映射到现有 AgentFlow updates/WebSocket 协议：

| LangGraph 事件 | AgentFlow 事件 | 客户展示 |
| --- | --- | --- |
| graph/node start | `step_started` | 正在处理的真实阶段 |
| node update | `step_progress` | 有限状态摘要 |
| tool start/end/error | `tool_call_*` | 工具名、状态、耗时和权限 |
| interrupt | `confirmation_required` / `input_required` | 原因和可选动作 |
| checkpoint | 内部审计 | 默认不占聊天正文 |
| graph completed/failed | `task_completed` / `task_failed` | 结论、警告和下一步 |

未经输出契约和 Verifier 校验的 token 不直接作为正式结论写入聊天。允许流式展示时，也必须标记为生成中，并在失败后明确撤销草稿状态。

## 6. LangChain 的有限职责

LangChain 不作为新的总控制层。以下条件满足时才引入对应组件：

- LangGraph 必须依赖 `langchain-core` 的消息或 Tool 类型；
- MCP Tool 到图 Tool 的转换使用成熟 Adapter 明显减少协议代码，并且不绕过 AgentFlow Governance；
- 某个新 Provider 只通过稳定 LangChain Adapter 才能满足多模态、结构化输出或流式要求；
- 动态 Tool 过滤、middleware 或 Runnable 确实替代了可测量的重复代码。

明确不做：

- 不用 LangChain 重写 ModelGateway 的 Provider、Key 和 Route Profile；
- 不用 LangChain Loader/Splitter 重写已经通过来源锚点验证的 PDF/DOCX/父子分块链；
- 不用 LangChain VectorStore 重写现有 Chroma generation、FTS5 和 RRF；
- 不接 LangSmith 作为前置依赖；现有 SQLite、任务历史和回归脚本继续承担本地可观测性；
- 不同时维护两套 Agent 对话历史或长期记忆。

实施后只有实际 import、运行并通过专项回归的 LangChain 组件，才能写入项目技术栈。

## 7. 替代与保留矩阵

| 现有能力 | 新技术可能带来的优化 | 决策 |
| --- | --- | --- |
| Native AgentRunner 单 Agent Tool 循环 | LangChain `create_agent` | 暂不替换；现有循环有稳定多 Provider/输出修复契约 |
| K4 Map-Reduce 编排和恢复 | LangGraph StateGraph + Checkpointer | 优先影子对照；通过后可替代内部调度代码 |
| Commander 组合 DAG | LangGraph 并行节点/子图 | K4 试点通过后再评估，不先迁移客户主入口 |
| AgentFlow 权限等待 | LangGraph Interrupt | 只做映射和恢复，不让框架拥有权限决策 |
| 专用外部 API Adapter | MCP Tools | 新增通用外部能力优先 MCP；现有稳定 Provider 不强迁 |
| ModelGateway | LangChain Model interface | 保留；必要时增加单向 Adapter |
| Hybrid RAG 检索算法 | LangChain Retriever | 保留算法与评测；可暴露只读 Retriever Adapter |
| DeepSeek Harness MCP 插件 | AgentFlow MCPGateway | MCPGateway 作为唯一控制面；Harness 只能消费获准 Tool |
| SQLite 任务/会话/产物 | LangGraph Checkpointer/Store | 保留主库；Graph checkpoint 使用独立数据库 |

## 8. 分阶段实施

阶段编号 `LGM` 只属于本文，不覆盖现有 C、D、H、K、R 里程碑。

### LGM0：基线冻结与技术探针

目标：证明依赖可安装、可打包、可关闭，并冻结迁移前事实。

任务：

- 记录当前启动耗时、核心回归、K4 恢复案例和 Runtime 事件契约；
- 核验 Python 3.11/3.13、Windows、Qt 自动后端环境和许可证；
- 在可选 requirements 中锁定 MCP、LangGraph、SQLite Checkpointer 与必要 LangChain 组件；
- 增加 capability probe，默认不 import 重依赖、不连接 MCP、不创建图；
- 定义 `RuntimeBackendDescriptor`、MCP contracts 和稳定错误码；
- 禁止修改现有客户任务路由。

出口：

- 主应用未启用新功能时，启动耗时和常驻内存相对基线无明显回退；
- 依赖缺失只显示“能力未准备”，不让后端启动失败；
- Native 全量回归保持通过。

#### LGM0 实施记录（2026-09-04）

- Windows 11 / CPython `3.13.13` 的实际后端 `.venv` 已按
  `backend/requirements-agent-runtime.txt` 安装并锁定：`mcp==2.1.1`、
  `langgraph==1.2.11`、`langgraph-checkpoint-sqlite==3.1.1`、
  `langchain-core==1.6.1`；四项安装包元数据均为 MIT。默认
  `requirements.txt` 不引用该可选文件，发行包体与正式 PyInstaller 验证仍留给 LGM7。
- 新增无副作用 `RuntimeBackendDescriptor` 与 `PlatformCapabilityDescriptor`：
  Native 为当前唯一 `ready` 后端；LangGraph、MCPGateway、LangChain Adapter
  即使依赖已安装也保持 `ready=false`。`GET /health` 只通过包元数据报告状态，
  不 import SDK、不创建图/SQLite Checkpointer、不启动子进程或连接 MCP。
- MCP 首期稳定契约已冻结：服务器和 Tool 的规范化名称、允许传输类型以及错误码均已定义；
  尚未接受 command、URL、密钥或客户 Tool schema，因此没有任何外部执行面。
- 已新增独立进程启动基线脚本。当前同机连续 5 次 `total_ready_ms` 为
  `2758/2455/2452/2456/2387`，中位数 `2455 ms`。此基线只用于后续同脚本、
  同环境的回归比较，不能与并发或冷磁盘条件下的旧测量混为“优化结果”。
- 已通过：`verify_lgm0_platform_probe.py`、`verify_backend.py`、
  `verify_knowledge_deep_task_map.py`、`verify_node_harness_adapter.py`、
  `compileall app scripts`、`pip check` 和 `git diff --check`。全部为本地确定性验证，
  未调用真实模型、未读取客户材料、未连接外部 MCP Server。

LGM0 通过的含义仅是“依赖可控、默认关闭、Native 基线未破坏”，不等于客户已经获得
MCP、LangGraph 或 LangChain 功能。其后的 LGM1 确定性 MCP Gateway 内核已完成，记录如下。

### LGM1：MCP Client/Gateway 内核

目标：完成无 LLM、无客户数据、无外部副作用的协议闭环。

任务：

- 实现 MCP 配置、ClientManager、Tool 发现、Schema 规范化和调用协议；
- 建立项目内确定性测试 MCP Server；
- 支持 local stdio 的启动、调用、取消、崩溃、重启和关闭；
- Tool 结果进入现有参数验证、权限、审计和脱敏链；
- 覆盖恶意名称、冲突 schema、超大结果、超时、协议错误和进程泄漏。

出口：

- 一个测试 Tool 可通过 `MCPGateway -> AgentFlow ToolGateway` 完成调用；
- 每次调用均有结构化 tool_call、耗时、错误和权限事实；
- Qt 退出后无 MCP 子进程残留；
- MCP Tool 不能读取未授权文件、环境变量或网络。

#### LGM1 实施记录（2026-09-04）

- 新增项目内唯一的 `agentflow-test` stdio 服务与短生命周期 `McpClientManager`。它只提供
  回显、整数求和、可取消延迟和大结果边界测试；不读取文件/环境变量/网络/模型，也没有
  FastAPI route、数据库配置或 Commander action。
- 启动配置只接受绝对 Python 可执行文件、受控 backend cwd、最多 12 个参数及
  `PYTHONUTF8`/`PYTHONIOENCODING` 白名单环境变量；不会继承项目 `.env`。每次发现或调用
  都在 SDK 的 `async with Client` 内完成，调用终止、超时、取消和服务异常退出后均关闭子进程。
- Gateway 已实现 Tool 目录发现、名称规范化、schema/参数 JSON 边界、文本/结构化结果裁剪、
  API Key/Authorization 样式脱敏、最大大小/深度/集合数限制和无正文短期审计。测试服务异常
  退出后可重新发现新进程；超大结果、未知 Tool、非法 schema 与关闭后的调用均明确失败。
- 新增 `WorkflowToolCall` 脱敏投影，验证 MCP 调用可以使用既有审计结构而不记录原始参数或
  结果正文。它尚未写入客户任务；LGM2 必须先通过 Action Admission、权限和 Verifier 才能落库。
- 已通过 `verify_lgm1_mcp_gateway.py`：真实 stdio 启动/发现/调用、超大结果拦截、取消、
  子进程异常退出与重新发现、无正文审计、关闭后拒绝重启均覆盖。未连接外网、未使用真实
  MCP Server、未调用模型或读取客户材料。

LGM1 交付的是受控协议内核，不是面向客户的“已支持 MCP”。真实连接、配置持久化、权限 UI
和 Commander 调用必须等待 LGM2 的首个客户场景确认。

### LGM2：首个客户 MCP 闭环

目标：证明 MCP 解决真实任务，而不是只展示连接成功。

首个产品场景必须在开发前由用户从候选中确认：

- 只读项目/协作平台查询；
- 受控公开资料检索；
- 客户已有业务系统的只读查询。

任务：

- 在插件管理/设置中完成连接、检测、Tool 列表和权限 UI；
- Commander 能按能力目录选择一个明确获准的 MCP Tool；
- 普通问答不加载无关 Tool schema；
- 结果经 Verifier/DeliveryCard 回到同一会话；
- 远程连接增加 HTTPS、认证和联网权限边界。

出口：

- 客户一句自然语言可触发一次真实、可解释的 MCP Tool 调用；
- 结果、来源、失败和权限在会话与历史中一致；
- 关闭连接后 Commander 不再承诺该能力；
- 未批准的 MCP 调用为零。

实施结果（2026-09-04，已完成）：

- 已选择“受控公开资料检索”作为唯一首期场景，并实现随应用发布的 `public-reference` stdio MCP Server；它只暴露 `search_wikimedia`，只读取固定的 `zh.wikipedia.org` Action API，最多返回 3 条标题、链接、摘要与抓取时间。
- 插件管理页只提供连接启用、协议/Tool 检测和停用。它没有 URL、命令、目录、环境变量、代理或密钥输入；连接默认停用，启用本身不联网，检测只发现固定 Tool、不读取公开页面。
- Commander 仅在客户明确提出“联网检索 / 查百科 / 公开资料”等意图且连接已启用时，才计划 `mcp.public-reference.search_wikimedia`。实际调用要求 `network + shell` 权限确认；`shell` 仅指启动项目随附的受控 stdio 子进程，不接受客户命令。
- `MCPGateway` 继续执行 Tool 目录、参数、JSON 结果和审计 Guard；结果还必须验证固定来源域名、Provider、范围和重复来源，之后才通过既有 Runtime/DeliveryCard 回到同一会话。客户看到的是可打开来源与事实边界，不是内部日志或原始 HTTP 正文。
- 本阶段不把公开检索并入 Native 组合 Runtime，不在计划中暗示与文档、数据或知识库可以并行汇总；组合执行、远程 Streamable HTTP 和第二个外部系统连接均留待独立需求确认。
- `verify_lgm2_public_reference.py` 覆盖默认停用、启用、真实 stdio Tool 发现、准入/权限、来源契约、Runtime 投影和交付卡；其 `--live` 模式已实际读取固定 Wikimedia 接口并验证来源为 `https://zh.wikipedia.org/wiki/...`。真实请求使用可追溯 User-Agent，避免被资料源错误拒绝。

### LGM3：LangGraph ExecutionBackend 骨架

目标：在不接客户业务的情况下验证图、事件和恢复映射。

任务：

- 实现 RuntimeRouter 和 `LangGraphExecutionBackend`；
- 建立包含分支、并行、Tool、Interrupt、失败和恢复的确定性测试图；
- 接入独立 SQLite Checkpointer；
- 映射 AgentFlow task/thread/checkpoint/graph version；
- 把图事件投影到现有 WebSocket、任务历史和 metrics；
- 验证取消、关闭和进程重启。

出口：

- 测试图从失败检查点恢复时不重跑已完成节点；
- Interrupt 批准后恢复同一 task，不创建割裂任务；
- 任务历史只有一套客户状态，不暴露框架内部对象；
- 功能开关关闭后完全走 Native Runtime。

实施结果（2026-09-04，已完成）：

- 新增确定性 RuntimeRouter；它不读取自然语言、不接受模型选后端，只允许显式启用、已准入、只读且无副作用的 lgm3_deterministic_fixture:v1 内部图进入 LangGraph。任何客户任务、未知图版本或写入任务均明确回退 Native Runtime。
- LangGraphExecutionBackend 只接受固定夹具，不注册 API 或 Qt 入口，不读取客户材料，不调用模型、MCP 或外部服务。它以独立 SQLite 文件维护不透明 task/thread 映射，图内覆盖准备、并行分支、无副作用 Tool、Interrupt、失败、恢复、取消和关闭。
- HarnessRuntimeEvent 已补齐等待确认与取消语义，并可投影为既有 TaskLogEvent 形状；投影不携带框架节点名、SDK 对象或原始消息。LGM3 尚未把测试事件写入客户历史或 WebSocket，真实落库与 UI 投影只能在 LGM4 业务影子试点中随同一个受控任务链实现。
- verify_lgm3_langgraph_execution_backend.py 已在临时目录真实创建 SQLite checkpoint，覆盖客户任务拒绝路由、并行、同 task/thread 的 Interrupt 恢复、关闭并重建后恢复、失败节点不重跑已完成分支、协作式取消和事件投影。未使用模型额度、网络、客户文件或主任务数据库。

### LGM4：知识库深度任务影子迁移

目标：用最适合 LangGraph 的既有 K4 Map-Reduce 证明真实收益。

任务：

- 将范围冻结、Map、分层 Reduce、来源验证和报告资格封装为业务节点；
- Native 与 LangGraph 使用同一输入夹具、模型 fake、Tool 和输出契约；
- 影子模式只比较计划、节点、恢复和结果，不向客户重复交付；
- 覆盖 Map 中断、Reduce 中断、限流等待、人工取消、恢复、部分完成和 generation 变化；
- 比较代码复杂度、checkpoint 数量、重复模型调用、恢复正确率和耗时。

迁移门槛：

- 既有 K4 固定回归 100% 通过；
- 已完成节点在恢复时不重复调用模型；
- 来源闭合、覆盖范围和报告资格不低于 Native；
- 任务终态、UI 阶段和 artifact 与现有协议一致；
- 维护复杂度确有下降，不能只因“图能运行”就迁移。

出口：

- 先以开发开关允许客户主动选择 LangGraph 试点；
- 经过真实材料验收后，才讨论是否将 K4 默认切换；
- Native K4 至少保留一个发布周期作为回退。

### LGM5：Commander 组合任务试点

前置：LGM4 完成并证明 LangGraph 有收益。

目标：改善文档、数据、知识库组合任务的显式 DAG、并行、汇总和子图状态。

任务：

- 每个专业 Agent 作为独立、按 invocation 隔离的子图或 Tool；
- Commander 保持 manager 和最终回复所有权；
- 失败分支不取消已完成的独立只读分支；
- 写入、联网和 MCP 节点通过 Interrupt 等待 AgentFlow 权限确认；
- 同一任务可在 Qt 中查看简化依赖和真实运行状态，内部事件默认折叠。

出口：

- 组合任务恢复不会重复已完成专业子任务；
- 部分完成时只汇总实际成功结果；
- 不绑定材料的普通聊天不进入 LangGraph；
- 不增加客户必须理解的框架概念。

### LGM6：LangChain 组件收敛

目标：在已有 LangGraph/MCP 实现中评估 LangChain 是否确实减少代码。

候选：

- `langchain-core` 消息与 Tool 类型；
- StructuredTool / ToolNode 适配；
- `langchain-mcp-adapters` 的受限转换层；
- 动态 Tool 过滤或 middleware；
- ModelGateway 到 LangChain ChatModel 的单向包装。

每项采用条件：

- 删除或显著简化一段自研胶水代码；
- 不丢失多 Provider、思考模式、用量、超时和错误分类；
- 不绕过 MCPGateway、权限、审计和输出验证；
- 有独立回归和清晰维护边界。

没有满足条件的组件不引入。允许最终项目只直接依赖 LangGraph 和官方 MCP SDK，而不依赖完整 LangChain。

### LGM7：稳定化、打包与默认策略

目标：决定哪些能力进入正式桌面发行。

任务：

- 依赖许可证、SBOM、版本锁定和升级策略；
- PyInstaller/目录式发行的 Python 包、MCP 子进程和 Node Harness 共存验证；
- 冷启动、常驻内存、并发、关闭和离线行为测试；
- MCP 连接备份、迁移、禁用和密钥清理；
- LangGraph checkpoint 版本迁移、清理和旧任务回读；
- Native/LangGraph/Harness 的支持矩阵和故障回退说明。

出口：

- 新机器无需全局安装 LangGraph、LangChain、MCP 或 Node 包；
- AgentFlow.exe 仍是唯一客户启动入口；
- 离线、依赖损坏和外部 Server 不可用时，基础 Native 能力仍可使用；
- 卸载/删除连接不会删除客户原文件或历史产物。

## 9. 测试与评估

### 9.1 MCP 回归

- stdio/Streamable HTTP 连接、发现、调用、取消和关闭；
- Server 崩溃、无响应、协议版本不兼容和能力变化；
- 重名 Tool、非法 JSON Schema、超长描述和超大结果；
- Tool 描述/结果中的提示注入、路径、密钥样式和 HTML；
- 本地命令/cwd/env 白名单；
- 远程 HTTPS、认证失败、重定向和联网权限；
- Qt 退出后无进程、端口和 session 残留；
- 每次调用都有完整但脱敏的审计记录。

### 9.2 LangGraph 对照

- Native 与 LangGraph 对相同 fixture 的最终结构化结果一致；
- Map/Reduce 任一节点失败后的精确恢复；
- 同一 superstep 其它成功节点不重复执行；
- Interrupt 前后副作用幂等；
- 取消、限流等待、权限拒绝、版本变化和进程重启；
- Checkpoint 不包含 Key、正文、绝对路径或 DataFrame；
- graph version 固定和旧任务回读；
- 事件顺序、客户终态和历史页一致。

### 9.3 性能门槛

- 未启用新能力时不 eager import LangGraph/MCP，不自动连接 Server；
- 普通聊天、文档问答和数据分析不新增模型调用；
- 冷启动中位数相对同机基线回退超过 10% 时不得合入默认路径；
- MCP 能力目录使用 server/version/TTL 缓存，不能每轮对话重新发现；
- 只向模型暴露当前任务候选 Tool，记录 schema 字符数和上下文成本；
- LangGraph checkpoint 保存增量摘要和引用，不保存大对象；
- 大型任务比较恢复后的重复调用数，不能只比较首次运行耗时。

### 9.4 真实验收纪律

- 本地 deterministic/fake 测试全部通过后，才进行一次明确目标的真实 Provider/MCP 验收；
- 真实验收只记录 Provider、模型、Server、Tool、耗时、状态和脱敏摘要；
- 不输出 Key、Authorization、客户原文和模型思考；
- 不用连续真实调用代替协议调试；
- 外部服务失败时保留 checkpoint，并明确区分网络、认证、限流、协议和结果校验失败。

## 10. 灰度、回退与删除

建议功能开关：

```text
AGENTFLOW_MCP_ENABLED=true
AGENTFLOW_LANGGRAPH_ENABLED=false
AGENTFLOW_LANGCHAIN_ADAPTERS_ENABLED=false
```

规则：

- `AGENTFLOW_MCP_ENABLED` 是部署级总开关，默认开启仅用于显示随应用发布的连接描述；每条 MCP 连接仍默认停用，未经客户启用绝不启动子进程、发现 Tool 或联网。部署将其设为 `false` 时必须彻底拒绝 MCP 调用；
- Runtime 后端、graph ID/version、MCP Server/Tool 快照写入任务历史；
- 第一次 Tool 或副作用前，后端准备失败可以回到 Native 并明确记录；
- 第一次 Tool 或副作用后禁止自动换后端重跑；
- MCP Server 禁用后不再进入 Commander 能力目录，但旧任务审计仍可读取；
- 删除连接只清理配置、缓存和专属 session，不删除由历史任务生成的正式产物；
- LangGraph 试点失败可关闭路由并继续使用 Native，不要求迁移全部旧 checkpoint。

## 11. 明确不做

- 不把现有工作流一次性重写为 LangGraph；
- 不为简历安装没有实际调用的 LangChain、LangGraph 或 MCP 包；
- 不建立无审核的 MCP 市场或允许任意命令一键安装 Server；
- 不把所有已有 HTTP Provider 强制包装为 MCP；
- 不让 MCP Tool 自动获得文件、联网、Shell、数据库或模型权限；
- 不把 LangGraph checkpoint 当作长期记忆数据库；
- 不把 LangChain memory 与现有会话/长期记忆并行保存；
- 不启用 LangSmith 作为本阶段依赖；
- 不因框架接入恢复通用代码工坊立项；
- 不改变知识库已验证的 FTS5、Chroma、BGE、RRF 与 Evidence Gate 默认算法。

## 12. 官方依据与版本纪律

实施前必须重新核对这些官方资料，因为 SDK 和协议仍可能变化：

- LangChain Agents：<https://docs.langchain.com/oss/python/langchain/agents>
- LangGraph Persistence：<https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph Interrupts：<https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangGraph Streaming：<https://docs.langchain.com/oss/python/langgraph/streaming>
- LangGraph Subgraphs：<https://docs.langchain.com/oss/python/langgraph/use-subgraphs>
- LangChain MCP：<https://docs.langchain.com/oss/python/langchain/mcp>
- MCP 官方架构：<https://modelcontextprotocol.io/specification/2025-06-18/architecture>
- MCP Python SDK：<https://github.com/modelcontextprotocol/python-sdk>

文档记录的是架构约束，不锁死未来版本。每次依赖升级必须记录：精确包版本、Python/Windows 支持、协议版本、许可证、破坏性变化、包体和回归结果。

## 13. 下一次开发起点

LGM0、LGM1、LGM2 与 LGM3 已完成。下一阶段候选是 **LGM4：知识库深度任务影子迁移**；它
只能使用冻结输入、模型 fake 和影子结果验证状态、事件与恢复，不能直接切换客户任务或替换 Native Runtime。
新的 MCP 连接、远程 MCP、LangGraph 客户路由与 LangChain 适配仍必须先由用户确认具体产品价值。

推荐顺序：

```text
LGM0 基线与依赖探针
  -> LGM1 MCP Gateway 内核
  -> LGM2 首个真实 MCP 客户闭环
  -> LGM3 LangGraph ExecutionBackend
  -> LGM4 K4 深度任务影子迁移
  -> LGM5 Commander 组合任务试点
  -> LGM6 LangChain 组件收敛
  -> LGM7 打包与默认策略
```

任何阶段未达到出口，都停在该阶段修复，不通过提高模型轮数、放宽权限、复制一套状态或隐藏失败来推进里程碑。
