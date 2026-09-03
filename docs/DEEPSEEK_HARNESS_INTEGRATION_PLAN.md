# DeepSeek Harness 集成评估与实施方案

最后更新：2026-09-03

状态：**H0 项目内 Node Runtime、H1 最小权限 profile、fake 执行契约与受控单任务 Node Bridge 均已通过；RuntimeRouter 尚未接入，未授权任何客户任务委派。现有 Native Runtime 仍是默认执行路径。**

## 1. 结论先行

DeepSeek Harness 值得引入评估，但不应直接替换 AgentFlow 后端，也不应让总指挥把任务、权限和历史所有权全部交出去。推荐定位是：

> AgentFlow 继续作为产品控制平面，DeepSeek Harness 作为可插拔的执行后端之一。

建议采用的能力：

- append-only 会话事件、JSONL/SQLite 持久化和中断恢复思路；
- step/turn/tool 的结构化生命周期与流式通知；
- 上下文压缩、旧工具结果裁剪和稳定前缀组织；
- Cordis 插件化工具装配，以及 MCP Tools 接入方式；
- 沙箱模式与批准策略分离的安全模型；
- 长任务的 session 复用、执行轨迹和失败后恢复。

2026-08-19 的产品路线补充：总指挥将先以 AgentFlow Native Runtime 完成“任务输入 -> 计划确认 -> 父子任务 -> 交付汇总”的主链路，并同时认识文档助手和数据工作台。Harness 不替代这条主链路，也不成为总指挥的默认聊天模型；它只在明确证明“长任务计划复核、受控续办或隔离只读执行”比 Native Runtime 更有收益时，通过 `RuntimeRouter` 选择性启用。总指挥的高质量模型策略仍由 `ModelGateway` 按用户配置和能力选择，不能因 Harness 接入而锁死为 DeepSeek。

不能直接照搬的部分：

- 不能让 Harness 成为 API Key、权限决策、客户任务、产物和审计的唯一所有者；
- 不能绕开现有 `ModelGateway`，把产品锁死为 DeepSeek 单供应商；
- 不能把 SDK 默认的本地文件或 Bash 能力直接暴露给模型；
- 不能把“会话可恢复”宣传成“外部副作用严格只执行一次”；
- 不能宣称上下文缓存命中率恒为 100%。DeepSeek 官方只说明缓存默认开启并尽力命中相同前缀，不作 100% 保证；
- Python SDK 当前没有 Windows 生产 Runtime wheel，不能作为 Windows 首期入口；但官方 Node CLI 已提供 Windows Node 验证路径，因此首期改用项目内 Node Runtime。

## 2. 官方能力核验

截至 2026-08-14，官方 Python SDK 为开发者预览版 `0.1.0rc6`。发行包名是 `deepseek-harness-sdk`，Python 导入名是 `deepseek_harness`。它不是纯 Python Agent 循环，而是启动随包提供的 Harness Runtime 子进程，通过换行分隔的 JSON-RPC stdio 通信。

高层 Python API 的实际形态是：

- `DeepSeekHarnessConfig`：供应商、模型、工作目录、session 目录、Cordis 配置、环境变量、运行时路径和超时等；
- `DeepSeekHarness`：启动、关闭、创建 session；
- `Session.run(...)`：同步运行一次输入，通过 `on_notification` 回调接收事件；
- `RunResult`：返回 session ID、最终回复、结束原因、事件、通知和 session 根目录。

需要特别注意：

- 高层 `Session.run(...)` 是同步接口。FastAPI 不能直接在事件循环中调用，正式 Adapter 要放入受控 worker 或 `asyncio.to_thread`；
- SDK 会复用懒启动子进程和 session，但进程生命周期仍需由 AgentFlow 管理；
- append-only session log 可以保留完整 turn/step/tool 轨迹；恢复时会为中断 turn 写入 `interrupted` 终态，不会截断之前的完整事件；
- 上下文压缩可记录 start/summary/end，并在模型可见历史中用摘要替换旧表面内容；原始持久化事件仍需保留；
- MCP Client 当前主要桥接 MCP Tools，Resources 和 Prompts 尚不是同等成熟的公开能力；
- 官方默认组合包含本地文件和 Bash 能力，示例中的 `danger-full-access` 只适合隔离、可丢弃环境，不能作为 AgentFlow 默认配置。

## 3. Windows Node Runtime 路线

官方 Node CLI 包 `@deepseek-ai/dsh` 当前版本为 `0.1.0-rc.6`，可通过 `npx @deepseek-ai/dsh web` 启动。官方仓库要求 Node `>=22.19.0`，并维护 Windows Node 的原生 CI；CLI 依赖中也包含 PowerShell 的本地执行与沙箱模块。

AgentFlow 采用目录式发布，不要求最终只有一个物理文件。正式安装目录可以包含 `AgentFlow.exe`、Python 后端、Node Runtime、Harness 包、资源和用户数据；安装器只需创建桌面快捷方式指向 `AgentFlow.exe`。用户不需要单独安装或操作 Node。

首期固定约束：

- 使用项目内 `backend/runtime/deepseek_harness_node/` 的锁定依赖，不使用全局 npm 或浮动 `npx latest`；
- npm 缓存也固定到项目 `backend/runtime/.npm-cache/`，不修改开发机的全局 npm 配置或执行策略；
- 不运行 `dsh web` 作为 AgentFlow 产品界面，Qt 仍是唯一客户 UI；
- Node Runtime 只作为无界面子进程，由 Python Adapter 管理启动、事件、超时与关闭；
- 首个运行配置必须是只读、无 Shell、无文件写入、无联网 Tool、无 MCP。

### Python SDK 的后续位置

`deepseek-harness-runtime-bin` 当前官方生产 wheel 只覆盖：

- Linux x86_64；
- Linux aarch64；
- macOS 14 arm64。

目前没有 Windows wheel，因此 Python SDK 不能作为当前 Windows 首期 Runtime。不过它仍是未来的候选 Adapter：如果官方发布 Windows Runtime，AgentFlow 会把它作为 `PythonHarnessBackend` 与 Node 版并行评估，而不是推翻 Node 集成。

平台决策：

- **A. 项目内 Node Runtime，已选定。** 适合当前 Windows 开发、官方 CLI 和插件生态。
- **B. Python Windows Runtime，未来待评估。** 发布后做同协议 A/B 验收，按稳定性、体积、事件能力和打包成本决定默认后端。
- **C. WSL2 sidecar，不采用。** Node Windows 路线已可验证，不增加客户环境依赖。

### Node CLI 的实际接入边界

本轮对项目内 `0.1.0-rc.6` 的实际包内容进行了无密钥审查，不能把 README 中的“Runtime”笼统理解为可直接嵌入的实时 SDK：

- `dsh --profile headless "task"` 是**一次性批处理入口**：启动一个新的持久化 session，等待内部 Agent 静止，只把最后一段非空 assistant 文本写到 stdout 后退出；它不是交互式对话，也不会把 token delta 直接写给 Qt；
- `dsh web` 是官方 Web profile 的别名，不能作为 AgentFlow 的客户界面或后台服务；
- 默认 base profile 会装配 PowerShell、文件系统、网页搜索、子 Agent、Workflow 等插件；Windows 也包含 ACL 沙箱和 PowerShell 执行链。包“带有沙箱”不等于已经符合 AgentFlow 的权限模型；
- 默认本地凭据提供方按“继承环境 -> `$DSH_HOME/.credentials.yaml` -> 启动目录 `.env` -> `$DSH_HOME/.env`”解析。若直接在客户 workspace 中启动，可能意外读取客户项目的 `.env`，不符合 AgentFlow 的 Key 所有权边界；
- 官方 CLI 的 profile 使用 `package.json` 加 `cordis.patch.yml` 叠加配置，且一个 patch 会替换目标行的完整 `config`。因此不能随意写半截覆盖配置，必须用临时 profile 的 `--dump-config` 先验证组合结果。

由此确定首期方法：**AgentFlow 只把 Harness 当作隔离子进程，不把 Qt 直接连到 CLI stdout；Qt 仍只消费 AgentFlow 标准任务事件。真实运行前，Bridge 必须创建项目专属 `DSH_HOME`、从受控空启动目录启动、显式禁用默认危险 Tool，并且只在一次进程内注入当前获准 Provider 的临时凭据。** 任何一项未通过时，Router 继续回退 Native Runtime。

## 4. 目标架构

```text
Qt UI
  -> FastAPI / Commander
      -> RuntimeRouter
          -> NativeAgentFlowRuntime
          -> NodeHarnessRuntimeAdapter（可选、功能开关控制）
              -> Node Bridge -> @deepseek-ai/dsh subprocess
                  -> Cordis plugins / Tools / MCP

AgentFlow SQLite <-> task / attempt / permission / artifact / normalized event
Harness session log <-> turn / step / model chunk / tool call / tool result
```

### 所有权边界

| 能力 | 最终所有者 | Harness 的角色 |
| --- | --- | --- |
| 客户任务与 `task_id` | AgentFlow | 保存映射后的 `session_id` |
| Agent 路由与总指挥计划 | AgentFlow | 执行已批准的子任务 |
| 多供应商配置与 Key | `ModelGateway` / 本地安全存储 | 仅接收本次运行所需的最小配置 |
| 权限与人工批准 | AgentFlow Governance | 执行已裁剪的能力集合 |
| 会话内部事件与上下文 | Harness session log | 记录 turn/step/tool 并支持恢复 |
| 客户可见历史 | AgentFlow SQLite | 接收规范化、脱敏后的关键事件 |
| 用户/项目长期记忆 | AgentFlow SQLite | 只可接收本次明确允许的最小摘要，不拥有记忆写入权 |
| 文件与产物 | AgentFlow workspace/output 边界 | 只访问批准目录，产物回交 Verifier |
| 结果验证 | AgentFlow Verifier | 提供执行结果，不自行宣布产品交付成功 |

AgentFlow 数据库需要增加稳定映射，而不是复制 Harness 的每一个 token：

```text
task_id
attempt_id
execution_backend
harness_session_id
harness_session_root
harness_runtime_version
last_event_sequence
resume_state
```

Harness session 属于**执行记忆**，只保存某次后端运行所需的 turn、step 与恢复线索；AgentFlow 的会话、用户偏好、项目约束与可复用经验属于**产品记忆**。两者以 `task_id/attempt_id/session_id` 映射关联，但不互相复制原始对话、API Key、绝对敏感路径、原始表格行或未经授权的客户材料。即使 Harness 升级、禁用或切换 Provider，AgentFlow 的任务历史和长期记忆也必须可独立读取、编辑、删除和恢复。

## 5. Adapter 接口草案

不把 `read_file`、`run_code` 假定为 SDK 的固定 Python 方法。它们应是 Harness 内部注册 Tool，由 AgentFlow 根据 Agent 和权限策略选择性装配。产品侧只依赖稳定的执行协议：

```python
class ExecutionBackend(Protocol):
    async def execute_task(self, request, event_sink) -> ExecutionResult: ...
    async def resume_task(self, checkpoint, decision, event_sink) -> ExecutionResult: ...
    async def close(self) -> None: ...
```

H1 当前已实际落地 `execute_task(...)` 的非敏感请求、规范化事件和终态结果契约，以及无 Node、无模型、无文件副作用的 fake backend 回归。还新增了受控 `NodeHarnessBridge`：只有 `AGENTFLOW_NODE_HARNESS_ENABLED=true`、`read-only` 和 DeepSeek Provider 同时满足时，才会通过项目内 CLI 调用 `agentflow-readonly`；当前不会由任何任务路由选择它。它只临时向子进程注入本次 DeepSeek 配置，使用隔离启动目录，不写入 Harness 凭据文件；官方 headless 仅映射开始、心跳、最终结果和失败事件。`resume_task(...)`、`close(...)` 与真实 `RuntimeRouter` 仍待实现；上方是完整目标协议，不应误读为已交付接口。

最小权限 profile 已作为随项目发布的 `agentflow-readonly` 模板落在 `backend/runtime/deepseek_harness_node/agentflow_profile/`。预检只会把模板原子同步到 AgentFlow 数据目录下的专属 `DSH_HOME`，以无密钥、关闭遥测、空启动目录和 `read-only` 权限运行 `dsh --profile agentflow-readonly --dump-config`。当前已回读确认 28 项默认能力为禁用，包括 PowerShell、文件读取/搜索、写入编辑、网页搜索、子 Agent、Workflow、技能、命令与外部 settings；预检不会创建 `.credentials.yaml`。

未来 `NodeHarnessRuntimeAdapter` 的实现原则：

```text
Python Adapter <-> Node Bridge (JSON Lines / JSON-RPC) <-> dsh Runtime

Python 负责 task_id、权限、运行开关、受控环境、超时和脱敏审计。
Node Bridge 只负责启动锁定的 dsh、转发 SDK 通知、维护 Harness session。
dsh 负责内部 Agent loop、session、上下文压缩和已批准的 Tool 组合。
```

不能在路由、Agent 或 Tool 中直接实例化官方 SDK。所有调用只能经过 `RuntimeRouter -> DeepSeekHarnessRuntimeAdapter`，这样 SDK 版本变化或回退到原生 Runtime 时不影响 Qt/API 协议。

## 6. 流式输出与 Qt

Harness 通知需要先规范化，再进入现有任务事件通道：

| Harness 事件 | AgentFlow 事件 | Qt 呈现 |
| --- | --- | --- |
| turn/step start | `step_started` | 当前阶段与步骤 |
| text delta | `assistant_delta` | 流式正文，内存聚合后节流刷新 |
| reasoning delta | `reasoning_delta` | 默认折叠，不进入普通结果正文 |
| tool call | `tool_started` | 工具名、目的、批准状态 |
| tool result | `tool_completed` / `tool_failed` | 脱敏摘要，不展示原始大输出 |
| finish/interrupted | 终态事件 | 完成、失败、等待确认或可恢复 |

性能规则：

- token delta 可实时显示，但按 30 至 80 ms 聚合刷新，避免 Qt 控件每 token 重排；
- SQLite 不保存每个字符片段，只保存关键阶段、聚合文本块和 Tool 审计；
- 原始 Harness log 留在受控 session 目录，任务历史保存摘要、索引和版本；
- UI 关闭或客户端断开不能杀死已确认的后台任务，重新进入任务详情后从事件水位继续订阅。

## 7. 恢复、重试与副作用

Harness 的 append-only log 能解决“模型看到过什么、运行到哪个 turn/step”的恢复问题，但不能自动保证外部操作只执行一次。AgentFlow 必须额外提供：

- `task_id + attempt_id + stable_call_id` 幂等键；
- 写文件采用临时文件、验证、原子提交；
- 网络发布、付款、删除、Shell 写操作在执行前持久化批准和调用意图；
- 崩溃恢复时先查询调用结果，无法确定则要求用户确认，不能盲目重放；
- 重试预算、同类失败计数和退避仍由 AgentFlow 统一约束；
- Harness session 可以恢复，业务 task 是否恢复由 AgentFlow 状态机决定。

## 8. 权限、沙箱与 MCP

Harness 的 `read-only`、`workspace-write`、`danger-full-access` 是执行能力边界；AgentFlow 的“始终询问 / 风险时询问 / 完全访问”等是批准策略。两者必须分别映射：

- 文档助手默认 `read-only`，不装配 Bash；
- 代码工坊初期 `workspace-write + 风险时询问`，写入范围只限客户选择的 workspace；
- `danger-full-access` 不作为默认值，即使客户选择也保留审计和危险操作拦截；
- 网络访问单独授权。官方沙箱术语不能替代网络、进程、域名和成本治理；
- Tool schema、参数、超时、输出预算和风险等级先由 AgentFlow Registry 校验，再交给 Harness。

MCP 只通过统一 Tool Registry 和 AgentFlow `MCPGateway` 接入。当前官方 MCP Client 以 Tools 为主；Node CLI 虽包含 MCP Client 依赖，但首轮试点不装配 MCP。后续仅接一项只读、价值明确的 MCP 做验收，不能因为包已下载就自动开放外部能力。2026-09-03 起，MCP 的独立控制面、实施顺序和验收归入 `docs/LANGGRAPH_LANGCHAIN_MCP_INTEGRATION_PLAN.md`；Harness 只能消费已获准 Tool，不再单独拥有一套 MCP 配置、权限或审计。

## 9. 上下文缓存与成本指标

DeepSeek API 的上下文缓存默认启用，完全相同的前缀更容易命中。Harness 的 append-only 历史、稳定 system prompt 和稳定 Tool schema 有利于提高命中率，但任何动态时间、随机 ID、工具顺序变化、插件变化或摘要替换都可能打断前缀。

AgentFlow 只展示真实指标：

```text
prompt_cache_hit_tokens
prompt_cache_miss_tokens
cache_hit_ratio = hit / (hit + miss)
```

不使用“100% 命中”作为产品承诺，也不为了追求命中而把密钥、无关历史或过期工具结果塞入上下文。

## 10. 分阶段实施

### H0：项目内 Node Runtime 与无密钥探针，已完成

- 固定 `@deepseek-ai/dsh@0.1.0-rc.6`、Node 版本下限、项目内 npm cache 和 lockfile；
- 用 `--version`、`--help`、`--profile headless --dump-default-config` 验证官方 CLI，不读取 API Key、不调用模型；
- 增加 Python 可查询的 Runtime 状态和功能开关；
- 记录 Node 进程、包版本、Node 版本、探针错误和关闭路径。

当前验收记录：Windows Node `v22.22.3`、项目内 `dsh 0.1.0-rc.6`、默认 headless profile 配置探针、`GET /api/harness/runtime` 与 `scripts/verify_node_harness_runtime.py` 均已通过。探针主动剥离模型 Key 并关闭遥测；`AGENTFLOW_NODE_HARNESS_ENABLED` 仍为 `false`。

### H1：Adapter 骨架与准入基线

- 已完成官方 `headless` 的无密钥行为、profile 自动初始化、默认插件与凭据优先级复核；
- 已新增 `ExecutionBackend.execute_task(...)`、非敏感请求/结果/事件契约和 fake backend 验证；
- 已新增受控 Node Bridge，但 `RuntimeRouter`、恢复与关闭协议仍待实现，不能由 fake backend 或 Bridge 代替；
- 保持现有 Native Runtime 为默认路径；
- 增加 feature flag、版本记录、启动/关闭和故障回退；
- Bridge 只能从隔离启动目录启动，`DSH_HOME` 必须位于 AgentFlow 数据目录；不得在客户 workspace 中直接启动 CLI，也不得写入或复用 Harness 的 `.credentials.yaml`；
- 在没有逐事件官方接口前，`headless` 只能先映射为“任务开始 / 阶段心跳 / 最终结果 / 失败”四类 AgentFlow 事件，不能伪称 token 级流式；
- 用临时 profile 的 `--dump-config` 验证每一条 Cordis 覆盖；首轮禁用 Shell、文件写入、网页搜索、MCP、子 Agent、Workflow 和动态插件；
- 不改 Qt 页面，只复用现有 WebSocket 事件。

当前验收记录：`scripts/verify_node_harness_adapter.py` 已验证三条规范化 fake 生命周期事件；`scripts/verify_node_harness_profile.py` 已在临时 `AGENTFLOW_DATA_DIR` 中验证官方 CLI 接受 `agentflow-readonly`，并回读 28 项禁用能力、`read-only` sandbox、`ask` approval 与零凭据文件。两项均未调用模型。

### H2-H5 启动原则

- 不把 H2-H5 当作必须连续做完的框架清单。平台先确认客户问题和专业 Agent，再决定 Harness 是否比现有 Native Runtime 更合适。
- H2 可作为隔离工程试点验证真实官方 Runtime，但不会因此把任何客户任务自动切到 Node；`RuntimeRouter` 只为已确认的总指挥或专业 Agent 场景开放。
- H3 只在已批准场景需要文件写入、命令或其它外部副作用时启动；H4 只在该场景有明确、稳定且高价值的 MCP 数据源或工具时启动。
- H5 只在发行候选明确携带 Node Harness 时进入。若真实业务没有收益，Native Runtime 继续作为默认，已完成的 H0/H1 Adapter 只保留为可复用技术资产。
- 通用代码工坊已取消自研立项，不能再作为 H2-H4 的默认需求来源；未来接入成熟外部代码 Agent 必须另立受控 Adapter 方案。
- H2 的优先候选不再是代码场景，而是“总指挥计划复核/长任务续办”的只读试点：输入仅包含 AgentFlow 已规范化的目标、约束、已选材料元数据、可用且已准入的 action 与当前计划摘要；输出只能是计划缺口、停止点、替代步骤或恢复建议，不能直接创建子任务、写入记忆或批准权限。
- 2026-08-20：总指挥 C0 动作准入、C1 初始材料绑定/汇总与 C2 用户可控长期记忆初版已由 Native Runtime 跑通；这使 H2 后续可以读取稳定的脱敏计划摘要，并在用户开启记忆时接收最小记忆摘要，但**不构成启动 H2 的理由**。Node Harness 继续不接入客户任务，直到有经过确认的只读计划审阅或长任务续办场景。

### H2：平台只读真实试点

- 仅在 Windows 受控工作区安装精确锁定的 Node 版预览 Runtime；
- 用一项隔离、可丢弃、无客户副作用的真实任务验证官方 Runtime、临时凭据、阶段事件、日志和终态映射；
- 不迁移现有文档/PPT 交付链，不因试点通过就成为默认执行后端；
- 关闭 Bash、写文件、网络和 MCP；
- 先验证官方接口真实提供的任务开始、阶段心跳、最终结果和失败事件；只有官方接口支持时才增加更细粒度事件，不宣称 token 级流式；
- 验证长会话压缩、进程重启与 session 恢复，并区分“会话恢复成功”和“外部副作用已恰好执行一次”。
- 首个业务形态优先采用“计划审阅器”：总指挥先在 Native Runtime 中生成并校验计划，再由用户显式选择或受控实验开关调用 Harness 给出非绑定复核意见；AgentFlow Validator 必须重新校验意见，用户或总指挥 Native 路径决定是否形成新计划版本。
- 该试点不迁移文档助手、数据工作台或 PPT 的既有执行链。数据工作台在 D5.4 准入前也不作为 Harness 客户委派入口。

### H3：按需受控写入与批准

- 启动门槛：已有用户确认的专业 Agent 或总指挥场景，且 Native Runtime 无法以更低复杂度满足其写入或命令需求；
- 增加 workspace-write、命令策略和人工批准映射；
- 增加幂等调用、产物回读验证和崩溃后的不确定状态处理；
- 完成取消、超时、进程退出和后端关闭烟测。

### H4：按需 MCP 与扩展工具

- 启动门槛：已批准的客户工作流有明确数据源或外部系统，通用 HTTP/API Tool 不能更简单、稳定地满足需求；
- 先验证自定义 Cordis 配置及捆绑运行时闭包；
- 复用 LGM 支线已经验收的 `MCPGateway`，接入一项只读 MCP，验证 Harness 侧 Tool 映射、命名、超时、断线重连、权限和审计；
- 不建设无目标的 MCP 市场，不按客户临时主题不断堆 MCP；所有 MCP 统一经过 `MCPGateway`、Tool Registry、能力路由、权限和审计。

### H5：条件式正式发行评估

- 启动门槛：至少一个已验收的正式能力决定在发行版中依赖 Harness；
- Node Runtime、许可证、哈希、体积和升级策略通过；
- Windows 安装目录、Node Runtime 随包交付与子进程解包策略通过；
- 原生 Runtime 回退、多供应商不受影响、离线模式可用；
- 达到验收门槛后也只对已验证场景启用；是否成为客户默认执行后端需要独立产品决策。

## 11. 验收门槛

- 项目内 Node Runtime 可在无全局 npm、无全局 Node 配置修改的 Windows 环境启动；
- 后端启动、复用、重启和关闭不会留下子进程；
- 长任务在进程异常后能恢复到明确状态，不丢历史、不重复危险副作用；
- Qt 能实时看到阶段、工具、等待批准和终态；
- 权限拒绝、超时、模型失败、Tool 失败和日志损坏均有结构化结果；
- API Key、环境变量、绝对敏感路径和原始大输出不进入客户历史；
- AgentFlow 现有任务、权限、产物和 Verifier 仍是最终事实来源；
- Native Runtime 可一键回退，DeepSeek 之外的 Provider 继续可用；
- 缓存只报告实际 hit/miss token，不作 100% 宣传；
- 目录式安装、桌面快捷方式、体积、许可证和升级回滚通过真实 Windows 验收。

## 12. 官方资料

- 官方仓库：<https://github.com/deepseek-ai/deepseek-harness>
- Node CLI：<https://github.com/deepseek-ai/deepseek-harness/tree/master/apps/cli>
- Node SDK：<https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/sdk/client>
- Python SDK 指南：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md>
- Python SDK README：<https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md>
- PyPI SDK：<https://pypi.org/project/deepseek-harness-sdk/>
- PyPI Runtime：<https://pypi.org/project/deepseek-harness-runtime-bin/>
- 架构说明：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md>
- Session：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md>
- Persistence：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.zh.md>
- Compaction：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/compaction.zh.md>
- Sandbox：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sandbox.zh.md>
- MCP Client：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md>
- DeepSeek 上下文缓存：<https://api-docs.deepseek.com/guides/kv_cache>
