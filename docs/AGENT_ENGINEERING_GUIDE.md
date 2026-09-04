# AgentFlow Agent 工程方法

最后更新：2026-09-03

本文记录 AgentFlow 后续开发 Agent Runtime、Tool Registry、评估系统和记忆系统时采用的工程原则。它不是立即引入所有复杂组件的清单，而是避免项目走向“模型裸奔”的长期约束。

## 核心判断

AgentFlow 的 Agent 不应只追求“模型回答得像人”，而要追求用户任务真的完成、过程可控、失败可解释、成本可管理。

当前阶段仍以本地桌面应用和 SQLite 为主，不急着引入 Redis 或在线评估平台。2026-09-04 已完成 LGM0 的可选依赖/无副作用探针和 LGM1 的确定性 MCP Gateway 回归，但仍坚持以现有 Native Runtime 为行为基线：LangGraph 只作为可选 `ExecutionBackend`，MCP 通过独立 Gateway 接入，LangChain 只复用有实际收益的组件。依赖或测试 Gateway 已通过不等于客户能力已开放；详细边界见 `docs/LANGGRAPH_LANGCHAIN_MCP_INTEGRATION_PLAN.md`。

阶段 5 的核心目标不是继续收集更多 Agent 名称，而是完成第一个真正的模型工具循环。现有 manifest、Node Contract 和 Runtime action 只能说明 Harness 已有插槽，不能自动证明某个 Agent 已经实现。

## Agent、Tool 与 Runner 的判定

正式 Agent 至少包含：清晰的任务所有权、指令、模型策略、工具白名单、权限策略、模型可见上下文、Runtime 本地上下文、输入输出契约、Guardrails、停止条件和评估指标。它可以在简单步骤中不调用模型，但必须有能力根据中间结果决定下一步。

以下情况优先做 Tool，而不是新增 Agent：

- 输入输出确定、一次调用即可完成，例如读取文件、grep、格式转换、写入受控产物。
- 不需要独立任务所有权、专属提示词、不同权限策略或多轮决策。
- 只是某个页面上的按钮、某个 action 名称或产品展示标签。

拆分新 Agent 需要至少满足一项：

- 需要不同的工具或 MCP 能力面。
- 需要不同的权限、Guardrail 或审批策略。
- 需要不同模型、上下文策略或结构化输出契约。
- 需要明确接管某一分支的任务/对话所有权。
- 拆分后能显著提高 trace 可读性、评估清晰度或故障隔离，而不是只增加 prompt 和路由成本。

Agent 可用性不能只看 manifest 的 `enabled`。后续统一区分：

- `registered`：manifest 已发现并通过静态校验。
- `enabled`：用户/管理员允许在产品中使用。
- `runtime_ready`：Agent Definition、Runner、所需 Tool、模型能力和依赖均可真实执行。
- `health`：最近自检状态，例如 `unknown/healthy/degraded/unavailable`。
- `maturity`：`placeholder/experimental/mvp/stable`，用于控制 UI 文案和 Commander 路由。

Commander 只能把 `enabled && runtime_ready` 的 Agent 放进可执行计划。占位 Agent 可以展示长期方向或产生“尚未具备该能力”的建议，但不能伪装成会执行的步骤。

## 外部资料取舍

参考 `docs/飞书文档.txt` 后，结论是：吸收 LangChain / LangGraph 背后的 Agent 工程思想，同时保留后续接入 LangGraph 的路线。AgentFlow 已有自研 Workflow、SQLite checkpoint、权限确认和任务事件记录；阶段 4B 已达到出口，阶段 5 先用这些协议完成一个正式 Agent 闭环，再根据真实分支/恢复复杂度判断是否接入框架。

可以吸收：

- Model / Tool / State / Runtime 分层：模型负责推理，工具负责执行，状态负责恢复，Runtime 负责调度和治理。
- 结构化输出：Planner、工具参数、工具返回、评估结果都优先用 Pydantic / JSON schema 约束。
- State / Node / Graph 思维：每个执行步骤都应明确输入、输出、写入状态、失败策略和下一步路由。
- Human In The Loop：敏感 Action 节点必须暂停，等待用户批准、拒绝或后续扩展的编辑决策。
- checkpoint 思路：任务不是一段临时内存流程，而是可恢复的状态快照；当前先用 SQLite 实现。
- updates 事件流：不仅展示最终回答，也要展示模型调用、工具调用、权限中断、产物生成和评估结果。
- 编排模式库：Prompt Chaining、Routing、Parallelization、Orchestrator-Worker、Evaluator-Optimizer、Subgraph 都作为后续设计词汇。

当前暂不直接采用：

- 当前不引入 LangSmith；评估和可观测性先走本地 SQLite、日志、验证脚本和后续自建报表。
- 当前不把 LangGraph 直接替换成核心 Runtime；阶段 5/6 后，如果出现复杂分支、并行、子图、人工中断恢复或插件工作流，优先评估接入 LangGraph。
- 当前不引入通用联网搜索或示例里的外部服务；联网工具必须先经过权限、成本和数据泄露风险设计。
- 不照搬教学代码里的 `create_agent` 快速封装；AgentFlow 要保留可审计、可解释、可打包的产品边界。

## 资料可信度与 2026-07-10 复审

工程资料按以下优先级使用：

1. 官方文档和正式协议。
2. 可公开核对的源码、测试和本项目实际验证。
3. 第三方复原工程、文章和课程，只作为设计线索与反例来源。

`D:\CC\claude-code-main` 是第三方复原/扩展工程，不是 Anthropic 官方源码。它的价值在于完整展示五层架构、Tool 接口、权限、上下文压缩、子 Agent、workflow journal 和 UI 观察面，也在于它自己的审查报告真实暴露了集成风险；任何结论都不能仅因为“它看起来像 Claude Code”就直接写进 AgentFlow。

Codex / OpenAI 官方 Agent 文档带来的校准：

- 从一个聚焦 Agent 开始，只在工具面、权限、模型、输出契约或所有权确实不同的时候拆专家。
- Agent loop 的停止点必须明确：最终输出、等待审批、失败、取消、预算耗尽或最大轮次，而不是无限 `while tool_call`。
- manager 与 handoff 是不同编排语义。AgentFlow 阶段 5 选择 manager：Commander 保持最终所有权，小 Agent 作为有边界的专业能力；handoff 延后。
- Runtime 本地上下文和模型可见上下文必须分开。认证信息、数据库客户端、日志器、权限对象和内部路径不应默认进入 prompt。
- Input Guardrail、Tool Guardrail、Output Guardrail 与 Human Approval 解决不同问题，不能只靠一层“权限确认”包办全部安全。
- 审批是同一次 run 的 interruption。系统保存待审调用和 resumable state，审批后继续原 run；等待中的 run 不应伪装成已完成结果。
- Sandbox 与 Approval Policy 是两个维度：前者限制技术能力，后者控制何时打断用户。完全访问也不代表取消审计、危险提示或输出验证。
- 早期调试先看完整 trace：模型调用、工具调用、Guardrail、路由/委派和终态；行为稳定后再把代表性 trace 固化为离线数据集和回归评估。

这些是协议和工程思想参考，不代表阶段 5 直接引入 OpenAI Agents SDK。AgentFlow 必须保持 DeepSeek、OpenAI、Anthropic、Qwen 和本地/私有网关可替换，现有 SQLite、权限审计和 Qt API 也不能被某一家 SDK 的运行对象绑死；未来如评估 SDK，只能作为 `AgentRunner`/编排层的可选 adapter。

## DeepSeek Harness 外部运行时评估

2026-08-14 已按官方仓库、Python SDK、PyPI 平台包和子系统文档完成第一轮复核。DeepSeek Harness 的 append-only session log、恢复、上下文压缩、结构化事件、沙箱和 Cordis/MCP Tool 装配值得吸收，但产品接入必须遵守以下边界：

- 它只作为 `ExecutionBackend` 的候选实现，由 `RuntimeRouter` 选择；现有 Native Runtime 保留为默认与回退路径。
- Commander 继续持有客户任务和最终回复，AgentFlow SQLite 继续持有 task、批准、审计、artifact 和产品终态；Harness session 只负责执行内部的 turn/step/tool 轨迹。
- 高层 Python `Session.run(...)` 是同步接口，FastAPI 接入必须使用受控 worker，并把通知规范化后复用现有 WebSocket；不能阻塞事件循环，也不能把每个 token 写入 SQLite。
- 会话恢复不等于外部副作用 exactly-once。写文件、Shell、联网和发布必须使用稳定调用 ID、批准记录、幂等策略和不确定状态处理。
- 官方沙箱与 AgentFlow Approval Policy 是两个维度；网络、进程、成本和数据外发仍由 AgentFlow 单独治理。
- MCP 首轮只考虑 Tools，且必须先验证 Python 捆绑运行时是否包含目标插件闭包；不能因为源码仓库存在 MCP 包就假定已可用于发布版。
- DeepSeek 上下文缓存只记录真实 hit/miss token，不承诺 100%。
- 当前 Python production runtime wheel 没有 Windows 版本，但官方 Node CLI 已具备 Windows Node 验证路径。首期采用项目内锁定的 Node Runtime 和可回退 Adapter；Python Windows Runtime 发布后按相同协议做并行评估，不能为切换 SDK 推翻现有任务或权限边界。

详细架构、阶段和验收门槛见 `docs/DEEPSEEK_HARNESS_INTEGRATION_PLAN.md`。

从本地 Claude Code 复原工程中可吸收：

- 五层结构：Qt 交互层、Commander/Workflow 编排层、AgentRunner 循环层、Tool/Node Contract 工具层、ModelGateway 通信层。
- 工具具有统一 schema、输入校验、权限、只读/破坏性、并发安全、输出预算、进度和 UI 描述，而不只是一个 Python 函数名。
- 搜索结果、命令输出和大文件先做结果预算和按需持久化；旧工具结果优先 micro-compact，原始审计记录仍保留在数据库。
- Workflow 核心通过 Ports/Adapters 与模型、journal、进度、权限和主程序隔离；稳定事件 ID 比依赖完成顺序更适合并发观察。
- 子任务需要明确类型：一次性专家、后台 worker、长期 teammate、远端 agent 不能共用一个模糊抽象。
- 长任务需要 token/时间/步骤/工具预算、收益递减停止条件和终态通知，不能靠用户反复催“继续”。

明确不照搬：

- 不在阶段 5 引入可执行 JS workflow 脚本。该工程的自审已发现动态 `import()` 沙箱逃逸、`scriptPath`/名称路径越界等问题；“保证重放确定性”不等于“提供安全隔离”。
- 不采用“并发分支先各自过预算检查、结束后再累计”的实现。共享预算要在获取并发许可时预留或原子扣减。
- 不允许 `parallel`/`pipeline` 把异常静默转成 `null`；允许局部降级，也必须产生结构化错误和可见事件。
- 不让 journal 依赖并发完成顺序；恢复键要绑定稳定 step/call ID、输入摘要和版本，发散时明确重跑范围。
- 不把后台运行完成仅写入隐藏状态。完成、失败、取消和阻塞都必须桥接到 Qt 可见终态通知。
- 不让 Agent manifest 声明未兑现的 PDF/Word/Shell 等能力；产品文案、Node Contract、真实 Tool 和验收用例必须一致。

当前已经落地的命令治理第一步：

- 命令安全策略检查只做静态分类和执行预案，不执行命令；借鉴“只读低摩擦、高危默认拒绝、解析不清提高风险”的 fail-safe 思路，为未来任何 Shell 工具、外部代码 Agent Adapter 和专业 Agent 副作用操作打底。
- 检查结果携带当前权限模式下的预计动作、运行请求状态、批准提示、执行路线、cwd 规则、沙箱提示、审计骨架、规则 ID、破坏性提示和更安全替代做法。未来真正接 Shell Runtime 时仍须重新校验，静态预案不是执行许可。

## 执行模式

AgentFlow 第一版采用混合模式：

- 主流程使用 Plan and Execute：Commander 先给出结构化 `workflow_plan`，Workflow Engine 校验后执行。
- 局部异常使用 REACT 思路：真实 Runtime 执行某一步失败时，可以根据结构化错误决定重试、换工具、降级或请用户确认。
- 高风险步骤必须先进入权限确认：文件写入、Shell、联网、数据库变更、插件安装不能由模型直接执行。
- 长任务拆成子任务：每个子任务有独立 `task_id`、`step_id`、日志、产物和工具调用记录，最后再汇总。

这意味着“先规划”不是死板执行；“边想边干”也不能无限循环。计划负责稳定性，局部 REACT 负责恢复能力。

### 通用 AgentRunner 循环

阶段 5 的通用 Runner 应使用相同生命周期，不为每个内置 Agent 复制：

```text
准备 Agent Definition 与最小模型上下文
-> 调用 ModelGateway
-> 校验结构化输出或 Tool Calls
-> 运行 Input/Tool Guardrail 与权限策略
-> 执行 Tool，记录结果、耗时和错误
-> 将脱敏结构化结果反馈给模型
-> 达到真实停止点后返回或暂停
```

每轮必须递增 `turn_index`，每个工具调用必须有稳定 `call_id`。Runner 至少限制最大轮次、最大工具调用数、同类失败次数、单工具超时、总耗时和预算。无法解析的模型输出不能假装成功：先尝试回复中后续的完整 JSON object，仍不合格时最多进行一次“不开放 Tool、不添加事实”的格式修复；修复次数进入 trace 和最终任务输出，失败后再返回结构化失败，不能无限重试。只有任务协议预先声明且能完全由已验证证据重建的窄场景，才允许确定性降级；例如文档助手的 requirements 明确标记行提取，不能泛化为问答或语义结论。

## 状态、节点与图

后续新增 Agent 或工具前，先按 Node 方式定义清楚：

- LLM 节点：负责理解、分类、规划、生成，输出必须尽量结构化。
- Data 节点：负责读取文件、检索知识、查询数据库，要有缓存、超时和降级策略。
- Action 节点：负责写文件、发请求、改数据库、执行命令，默认需要权限和审计。
- Human 节点：负责用户确认、补充信息、编辑草稿或拒绝执行，必须能恢复到原任务。
- Verifier 节点：负责检查产物质量、风险和可用性，只给可验证问题，不做泛泛夸奖。

State 只保存下游确实需要、无法低成本重建或必须审计的信息，例如原始输入、结构化计划、步骤输出、产物引用、权限决策、错误类型和评估结果。格式化后的临时展示文本、可从其他字段推导出的内容，不应污染核心 State。

等待审批时，State 还必须保存原 run、原 step、待审工具及参数摘要、审批原因、审批版本和恢复游标。批准或拒绝后恢复原 run；只有显式 retry 才创建新的 attempt，不能用新 task 掩盖原执行链。

## 编排模式库

这些模式先作为设计参考，不等于立刻引入新框架：

- Prompt Chaining：适合当前 Document -> Code -> Report 的稳定串行流程。
- Routing：由 Commander 根据任务类型选择 Agent、工具和执行路径，是阶段 5 的重点。
- Parallelization：只在多个只读或独立步骤之间使用，例如多文档摘要；写操作和高风险步骤不能盲目并行。
- Orchestrator-Worker：适合以后批量文档、深度研究或多文件代码生成，必须先有预算、合并和失败收敛策略。
- Evaluator-Optimizer：适合报告润色、代码审查和结果质量门控，但循环次数必须有硬上限。
- Subgraph：适合未来把复杂 Agent 或插件工作流封装成可复用子流程，当前先用简单顺序链保持可读性。

## LangGraph 接入门槛

LangGraph 不是废弃项，而是阶段 5/6 之后要认真评估的编排框架。建议满足任一条件时启动接入设计：

- 单个 `workflow_plan` 出现多分支、回环、并行聚合或子图复用，自研状态机开始难以维护。
- Human In The Loop 不再只是批准/拒绝，而需要编辑参数、补充信息、暂停后跨会话恢复。
- 插件 Agent 需要把自己的小工作流作为可插拔子图接入 Commander。
- 需要把模型事件、工具事件、interrupt 和 checkpoint 统一成更标准的运行轨迹。

接入时不应推翻现有 `ModelGateway`、权限审计、SQLite 任务历史和 Qt 协议，而是把 LangGraph 当作后端编排实现之一，外部 API 继续保持稳定。

2026-09-03 评估结论：知识库 K4 已出现递归 Map-Reduce、逐节点 checkpoint、失败恢复和多阶段状态映射，达到 LangGraph 试点门槛。试点仍必须先经过 LGM0 依赖探针与 LGM3 后端骨架，并在 LGM4 以相同冻结输入做 Native/LangGraph 影子对照；在恢复语义、来源闭合、产物哈希和事件终态一致前，不允许切换默认执行路径。

## MCP 接入原则

MCP 是外部能力协议，不等于新的 Agent，也不归某个模型框架或 DeepSeek Harness 独占。AgentFlow 使用独立 `MCPGateway` 持有服务器配置、连接、能力发现、Tool 命名、权限、审计、超时和结果裁剪；LangChain 或 Harness 只能消费 Gateway 已准入的 Tool。

- 首期只开放 Tools，优先本地 `stdio`，远程只采用当前官方支持的 Streamable HTTP；Resources、Prompts、Sampling、Elicitation 后续逐项评审。
- 本地 MCP server 本质上是受控子进程：可执行文件、参数、工作目录和环境变量必须白名单化，凭据只保存 `secret_ref`，不得继承完整 `.env`。
- Tool 统一命名为 `mcp.<server_id>.<tool_name>`，先通过 Node Contract、权限和参数校验，再进入 MCP Client。
- MCP 返回内容必须受大小、类型和敏感信息检查；不能把外部工具的“成功”直接等同于客户任务完成，产物仍需 AgentFlow Verifier 回读。
- 不为临时主题堆服务，不做无目标的 MCP 市场；LGM2 的首个真实连接必须先由用户确认客户价值。

## 评估指标

评估 Agent 效果主要看两类指标：

- 任务成功率：用户目标是否完成，产物是否可用，是否需要用户反复修正。
- 执行效率：用了多少步骤、耗时多久、消耗多少 token、工具调用是否选对、参数是否填对、失败后是否快速收敛。

后续离线评估建议：

- 建一批固定测试用例，覆盖文档处理、代码生成、报告生成、权限拒绝、工具失败、长上下文等场景。
- 人工标注期望结果和关键断言，优先用确定性规则验证。
- 可以引入 LLM-as-Judge 辅助评分，但不能把它作为唯一真值；关键路径仍要人工抽查。
- 每次改 Runtime、Planner、Tool schema 后，都要跑离线用例，观察成功率、步骤数和失败类型变化。

后续上线评估建议：

- 记录用户反馈、失败率、取消率、重试率、权限拒绝率和工具错误率。
- 对新的 Planner 或 Runtime 策略先做灰度或 A/B，对比任务成功率和平均成本。
- UI 上让用户能明确反馈“结果有用 / 无用 / 失败原因”，但不要频繁打断用户。

## 上下文与记忆

上下文工程优先级高于单纯换大模型：

- 不把全部历史对话塞进 prompt；早期对话压缩成摘要，只保留目标、约束、用户偏好、关键决策和未完成事项。
- 中间结果写入数据库，需要时按 `task_id`、`step_id`、artifact 或 tool_call 读取回来。
- 长任务拆成子任务，每个子任务只带必要上下文，最终由 Commander 或 Report Agent 汇总。
- 支持长窗口模型可以作为兜底，但默认先用任务拆分、摘要和检索控制 token。
- 先压缩旧工具结果，再摘要整段对话。工具调用和工具结果必须成对保留，压缩边界要可审计，不能截出无对应结果的半个调用。
- 摘要至少保留目标、约束、已确认决策、来源引用、已完成步骤、失败原因、待审批事项和下一步；压缩后按预算重新注入最近且仍相关的文件/产物，不盲目恢复全部内容。

当前落地方式：

- 短期记忆先用 SQLite 保存任务状态、计划、步骤、日志、权限、产物和工具调用。
- 单机桌面场景暂不引入 Redis；如果未来需要多进程、多用户或高并发，再把会话状态抽象到可替换存储。
- 长期记忆已由 SQLite 的短事实记录承接，用户可查看、修改、停用、删除和清空；语义检索/向量召回仍属后续优化，不是当前正确性前提。
- 任务完成后的记忆提议必须是只读候选：只接受明确长期表达，最多提出有限条目，客户可编辑后明确确认；候选查询、模型推测和一次性任务都不得自动写入长期层。
- 记忆范围当前只允许 `global` 与 `project:<稳定标识>` 两种命名空间。它不能承载本机路径、文件权限、成员关系或完整项目管理语义；这些需求必须另行设计权限模型。

## 检索策略

AgentFlow 后续做知识库、代码检索和长文档问答时，先把“检索”与“狭义 RAG”分开。这里的狭义 RAG 指 chunk、embedding、向量库、相似度召回那套流程；它不是 Agent 必需组件，只是检索工具箱中的一种实现。

默认路线是 agentic search first：

- 代码和项目文件优先走 grep / ripgrep / glob / read file：函数名、类名、文件名、报错码、配置项、路径、ID、固定关键词、日志片段，都应该先用可审计的即时搜索工具定位。
- 语义理解再走 RAG：模块职责解释、跨文档归纳、模糊问题、类似方案查找、长文档问答，才需要向量检索、摘要、重排或 LLM 辅助总结。
- 标准链路优先是“工具按需搜索 -> 读取少量关键文件 -> 必要时语义补充 -> LLM 生成结论”，而不是直接把整个仓库向量化，也不是把长上下文一次性塞满。

落地原则：

- 代码仓库默认不做全量向量化；代码、配置、日志、接口名等强结构内容先靠精确检索解决。
- 向量索引优先给长文档、设计文档、知识笔记、用户上传资料、历史总结等“自然语言密集”内容使用。
- 检索路由应显式区分“定位”与“理解”两类诉求，后端协议中最好保留 `retrieval_mode`、`source_type`、`matched_by` 一类元数据，方便审计和调优。
- 如果 grep 已经精确命中，尽量不要再做高成本 embedding；如果用户提问模糊，再用 RAG 补语义。
- 精确搜索零命中只能说明字面没有匹配，不能推导为“资料不存在相关内容”。对已明确选择的一份材料，可受控回读一次并要求最终结论引用实际来源；若仍无证据，应返回澄清/补充材料建议，而不是编造否定结论或抛出底层格式错误。
- 长上下文模型可以作为兜底，但不能代替检索策略。上下文越大，越需要先筛选、归纳和排序，否则模型容易被无关内容稀释注意力。
- 后续如果实现 `RetrievalRouter`，应把 grep/glob/read、SQL/API 查询、RAG、Web search 都视为同级工具，由任务意图和数据类型决定路由。

这套路线的目标不是“技术看起来全都用了”，而是让检索更快、更稳、更便宜，同时保留复杂问答能力。对 Coding Agent 来说，RAG 应该是可选增强，不是默认底座。

### 已批准的知识库实现约束

2026-08-20 用户批准了本地知识库方案，详细规格见 `docs/KNOWLEDGE_BASE_PRODUCT_SPEC.md`。该决定不会推翻上述 agentic search 原则，而是把自然语言密集资料的 Hybrid Retrieval 正式纳入阶段 5K：

- 客户侧“知识库”由 `Knowledge Agent` 持有资料生命周期、可信问答和深度任务；关键词、向量、重排和来源定位仍是共享 `Knowledge Retrieval Service`，不能按算法拆 Agent。
- 默认路由为规则优先：精确实体走关键词，模糊语义走 Hybrid，多跳问题有限拆分，全篇任务走 Map-Reduce，普通问候不检索。
- Hybrid 初始链路为 BM25/关键词与 Dense 各自召回、RRF 融合、父块扩展、去重和可选 Rerank；任一高成本环节不可用时必须可解释降级。
- 分块采用结构优先的父子关系，子块用于匹配，父块用于恢复完整上下文；分片、Embedding 和检索 profile 都必须版本化。
- SQLite 保存文档、版本、来源、任务和审计事实；关键词和向量索引只是可重建派生数据。
- 增量向量复用只能发生在新建的独立 generation：旧代次必须仍是当前活动且 `ready`，并同时核对同资料库、相同 Index/Embedding Profile、相同子块 ID 和内容哈希。旧 Chroma collection 只能只读回取；目录缺失、损坏或单条向量不可读时直接重新嵌入，禁止复制目录、向 SQLite 落向量或跨 Profile 复用。
- 长窗口和 Provider Context Caching 是按能力启用的优化，不是默认替代检索；缓存命中只能根据真实响应统计。K5.7 已要求 K3/K4 在真实调用前持久化无正文的路由、实际输入字符数和内部预算；已确认窗口只代表未来可评估能力，不能自动开启整库直读，超预算必须停驻而非绕过检索/Map-Reduce。
- LangGraph 只优先评估 Map-Reduce、并行和 checkpoint 深度任务，普通问答保留 Native Runtime；DeepSeek Harness 不进入普通索引或问答路径。
- RAGAS/LLM Judge 只作离线比较，引用身份、更新/删除失效、权限和索引切换必须由确定性回归验证。
- K6 起任何 Retrieval 策略升级先在版本化的合成/脱敏题集上比较：`required` 回归不得退化，`diagnostic` 缺口用于决定是否值得继续实验。评测日志只保留题目 ID、聚合指标和耗时；不将客户问题、材料正文、文件名或向量写入评测产物。候选策略未证明收益前默认关闭。
- K7 已确认只做扫描件 / 图片型 PDF 的本地优先 OCR。OCR 是受控解析 Tool：文本 PDF 先走原有提取，图片型材料或无文本层 PDF 才明确提示并由客户确认准备本地模型；识别结果必须带页码/区域来源并继续受版本、generation、删除、Evidence Gate 和数据出站规则约束。K7.1-K7.3 已选定移动 OCR + 方向分类候选，并以可选 requirements、延迟导入、受控缓存/ready marker、图片/无文本层 PDF 解析和离线假引擎契约保证默认后端绝不下载或加载它；`image` / `region` 的既有知识库升级必须通过前向 migration 与 `foreign_key_check`，不得靠关闭约束长期运行。当前 Windows/Paddle CPU 路线固定关闭 oneDNN。复杂版面/表格/公式和云端视觉另行立项，不能借 OCR 名义默认扩张。
- K7.4.1-K7.4.2 已进一步固定客户准备和页级恢复边界：能力诊断只能检查依赖与 ready marker；只有 `confirm_download=true` 的专用后台准备入口可调用模型下载，重复请求必须去重，进程重启后以 capability 为准。准备状态不得夹带客户文件、缓存路径、下载地址或底层异常；Qt 用真实阶段和活动指示器反馈，不用伪百分比。实际 OCR 只在索引任务中显示 `ocr_recognizing`；只对 `ocr_page_failed` 自动重试一次，`ocr_no_text` 不重试，重试失败不撤回其它成功页。页级统计、版本、任务和材料表均不得保存 OCR 正文、原图、坐标或绝对路径，模型准备成功也不能伪称扫描件已完成。

## 文档处理链路

参考外部文档技能后的结论是：文档 Agent 不该把“找文件、读内容、提取要点、改写生成、写回结果”糊成一个黑箱动作，而应拆成可观察链路：

1. 定位：先明确用户给的是文件名、路径、链接、目录还是纯文本。
2. 读取：用只读工具拿到原文、摘要或结构化片段，并记录来源。
3. 分析：抽取需求、要点、约束、表格字段、章节结构等中间结果。
4. 生成：输出代码草稿、报告、摘要或结构化数据。
5. 验证：写入或生成产物后要做回读验证，不只相信“写入成功”的返回码。
6. 展示：把输出路径、artifact URI、来源和风险提示明确给用户。

这对 AgentFlow 的价值是：

- 便于把 workspace 本地文件、未来云文档、知识库文档统一进同一套 Harness 语义。
- 更容易做权限确认、失败定位、产物追踪和 UI 事件流展示。
- 以后扩展 PDF / Word / Excel / 云文档时，不需要推翻核心工作流协议。

## 工具调用

工具调用必须结构化、可验证、可审计：

- 优先使用模型原生 Tool Calls / Function Calling；返回值用 JSON schema / Pydantic 校验。
- 非 Tool Calls 模型必须明确工具名称、参数类型、必填字段和 JSON 输出格式，解析失败最多重试两次。
- 后端必须校验关键参数，不能只相信模型；必要时给安全默认值或直接拒绝。
- 工具返回结果要以结构化 JSON 喂回模型，并明确要求模型只能从返回数据中选择参数，避免凭空编造 ID、路径或选项。
- 每次工具调用都要记录 `task_id`、`step_id`、`tool_name`、参数摘要、状态、耗时、错误和权限需求。

失败处理规则：

- 同一工具同类错误连续失败达到阈值后停止重试，避免死循环。
- 单个工具要有超时，整体任务也要有超时。
- 模型网络超时要单独归类：保留已完成的工具轨迹，使用稳定停止原因和可重试的用户提示；不要把它伪装成模型回答质量差，也不要靠无限重试掩盖供应商或网络问题。
- 可降级工具要提供 fallback，例如主 API 失败后换备用 API，或从真实执行退回只读建议。
- 错误信息返回给模型时必须结构化，区分参数错误、权限拒绝、网络失败、超时、内部异常和用户取消。

## 多 Agent 协作

多 Agent 不是越多越好，必须让职责边界清楚：

- 能由一个聚焦 Agent 在相同工具、权限和输出契约下完成时，保持单 Agent；不要为了展示“多智能体”增加无价值 handoff。
- 每个 Agent 的角色写进 manifest 和 system prompt；专业 Agent 不能自行审批权限，文档交付能力不能因为调用外部 Runtime 就获得 Shell 权限。
- Agent 之间用结构化消息传递，至少包含 `task_id`、`step_id`、输入摘要、产物引用和错误信息。
- 第一版优先顺序链：Commander 持有目标与计划，已批准的专业 Agent 完成交付，共用 Verifier 回读文件、数据或状态。通用 Code Agent 已取消自研立项，Report 能力已并入 Document；历史协议名称不等于实际协作角色。
- 遇到 Agent 冲突时，引入仲裁者或人工确认；不能让多个 Agent 互相争论到无限循环。
- 审查型 Agent 的输出要偏向问题、风险和可验证建议，不能只给泛泛评价。
- 阶段 5 使用 manager 模式：Commander 负责计划、选择专业能力、合并结果和最终回复。小 Agent 不直接接管用户会话，也不能自行扩大工作区或工具权限。
- DeepSeek Harness、LangGraph、MCP 或外部成熟 Agent 都是按需执行能力，不是必须凑齐的架构标签。只有已批准的客户流程出现 Native Runtime 难以满足的恢复、工具编排、写入或外部系统需求时，才增加相应运行时复杂度。

## 成本与性能控制

真实模型和工具执行都要有预算意识：

- 每轮模型调用要记录模型、输入 token、输出 token、耗时和估算费用；拿不到 token 统计时先记录字符数和消息长度。
- 单任务设置最大步骤数、最大工具调用次数、最大重试次数和最大执行时间。
- 并发共享预算使用 reservation：分支开始前预留上限，结束后按实际消耗结算并释放余额；预算检查与并发许可获取要处于同一原子边界。
- 超预算时停止自动执行，给用户说明原因和下一步选择。
- 离线验证默认使用 mock，真实模型验证单独运行，避免无意识消耗额度。
- 高频路径不能反复扫描磁盘、重复解析 manifest 或重复创建昂贵对象；缓存必须说明失效策略。
- 知识库的性能建议只能使用无正文的阶段耗时、资源容量和队列事实。索引/深度任务必须经受控后台队列运行：同类任务 FIFO 串行，低配设备全局串行，中高配最多一条索引与一条深度链并行；普通检索与问答不能因长任务失去即时性。进程队列不等于可恢复业务状态，重启后的恢复仍只依赖 SQLite checkpoint，不能保存客户问题、文件名、正文、路径或设备身份来维持排序。

## 自我反思与规则升级

AgentFlow 的工程实践也要有“自我修正”机制，但要克制：

- 用户明确纠正、同类 bug 重复出现、某种交互持续让用户困惑时，要记录根因和修正方式。
- 规则升级依据应该是明确反馈或重复证据，不从用户沉默里臆测偏好。
- 复盘内容优先沉淀“为什么出错、以后如何规避”，而不是只写“这次失败了”。
- 对产品和 Runtime 来说，复盘结果最终要落到可执行规则：例如增加验证脚本、补权限边界、改状态提示、限制重试次数、减少 UI 遮挡等。

## 与当前项目的对应关系

已落地：

- Commander 结构化 `workflow_plan`。
- Workflow dry-run 和计划校验。
- SQLite 持久化任务、步骤、日志、权限、虚拟产物和模拟工具调用。
- SQLite 已承担第一版 checkpoint 职责，任务可以在内存缓存清空后恢复。
- 执行预算和运行指标协议：步骤数、工具调用数、权限请求数、估算 token、耗时、重试上限和工具超时。
- Runtime 状态机已包含 `pending`、`running`、`paused`、`waiting_permission`、`completed`、`blocked`、`failed`、`cancelled`，并可查询终态、允许控制动作和下一步可达状态。
- Runtime C3 已有显式后台受理入口、runtime task 派生/恢复、权限等待、协作式暂停/取消、继续、Tool 边界 checkpoint、append-only SQLite 事件和受控 outputs 文本产物；后台 worker 仅在当前进程内运行。服务启动时会把遗留的 `pending/running/waiting_permission` Runtime 停驻为 `blocked`，保留检查点并追加中断事件，绝不自动重复未知副作用；自动续办仍未实现。
- Commander C3 已将项目范围传入计划与长期记忆检索，并支持从已完成 Runtime 总指挥任务生成可编辑、显式确认的记忆候选；候选由确定性规则生成，服务端再次校验身份和内容后才写入 SQLite，并追加审计事件。
- 任务效果评估接口：基于已落库的 run、metrics、tool-calls 和权限审计，计算步骤成功率、工具成功率、效率分、阻塞/失败信号和下一步建议。
- `ModelGateway` 多供应商抽象。
- 本地知识库 K0-K5 产品与工程规格已获批准；K1 已完成资料生命周期和可选本地向量 generation，K2 已完成严格绑定活动 generation 的只读 Retrieval Service、RRF、父块去重、可解释降级和固定夹具。它仍未生成模型答案，`knowledge_agent.runtime_ready=false`，不能把资料库管理页或检索 API 当成可信问答能力。
- 权限请求和批准/拒绝审计协议。
- Qt 历史页已经承担第一版 updates 观察面，展示步骤、运行态、工具调用、产物、日志和评估。
- `verify_backend.py` 离线验证脚本。

### 流式体验边界

- 流式不等于把模型的每个 token 原样展示。对聊天、草稿等非结构化内容，可在后续采用 token stream；对文档助手这类需要来源与 JSON Guardrail 的任务，当前先流式展示已发生的阶段事实：任务受理、材料范围确认、模型回合、Tool 执行、来源校验和终态。
- 结构化终态必须在 Output Guardrail 通过后才渲染给客户。这样用户不会长时间面对静止页面，也不会把模型的中间猜测、未完成 JSON 或未验证引用当作正式结论。
- 事件流只作当前进程内的即时观察；完成后的任务、日志、Tool trace 和结果仍以 SQLite 为准。实时缓冲有数量上限，不能成为无界内存日志。

下一步逐步落地：

- 文档助手的只读理解闭环、共享 Agent Definition / AgentRunner 和跨 provider 结构化 Tool Calls 已实现；下一份文档工作台规格需要先确认 PDF/OCR、多文档、创作、改写、审校与导出的边界，不能把它们误算为当前 MVP。
- 让服务重启后遗留的 `running` 任务进入可解释的恢复/人工处理流程；不要把“已有 checkpoint”夸大成已自动续跑。
- 继续把模型调用、工具参数校验、Guardrail、权限、工具结果和终态串成同一条 trace/updates 时间线；当前已覆盖 Runtime/Tool/权限/控制事件，尚未提供 token 级模型流。
- 按已批准的阶段 5K 从 K0 技术试验开始，不先开启任意 Shell、插件、多 Agent 并发或未经评估的大型本地模型。
- 真实 token / 真实耗时 / 成本回填和工具调用准确性统计。
- 离线评估用例集和基础评分报告。
- 自动续办前的跨调用幂等键、副作用证据与人工恢复策略；当前只允许重启后安全停驻和显式 retry。

## 官方参考

- OpenAI Agents SDK 总览：<https://developers.openai.com/api/docs/guides/agents>
- Agent 定义与拆分：<https://developers.openai.com/api/docs/guides/agents/define-agents>
- Agent loop 与暂停：<https://developers.openai.com/api/docs/guides/agents/running-agents>
- Orchestration / handoff：<https://developers.openai.com/api/docs/guides/agents/orchestration>
- Guardrails 与审批：<https://developers.openai.com/api/docs/guides/agents/guardrails-approvals>
- Agent workflow 评估：<https://developers.openai.com/api/docs/guides/agent-evals>
- Codex 权限与安全：<https://learn.chatgpt.com/docs/agent-approvals-security>
