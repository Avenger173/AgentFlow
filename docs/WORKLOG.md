# AgentFlow 开发流水

## 2026-07-16

- 文档助手交互收敛：直接入口改为异步受理，Qt 拿到 task_id 后连接 WebSocket，按真实的材料范围确认、模型回合、搜索/读取、来源校验和终态更新页面；完成后才读取已通过 Guardrail 的结构化结果。实时缓冲仅用于当前进程观察，最终任务历史仍落 SQLite，并限制保留数量。
- 输出稳定性与呈现：OpenAI-compatible 的 Tool 获取阶段结束后会请求已声明的 JSON mode，减少“模型自然语言回答导致 JSON 协议失败”；离线 WebSocket 回归与隔离真实 DeepSeek 验证通过。文档页把抽象“输出方式”改为常用任务，并将结果渲染为分层结论、摘要、需求卡、待确认问题、来源和注意事项。

- 文档助手故障校正：定位到 `DocumentSourceRef.excerpt` 的展示协议上限为 360 字符，而读取 Tool 曾把更长的审计片段带入最终来源，导致已成功读取的文档在来源映射阶段失败并触发重复失败保护。现已分别限制模型片段、审计摘要和展示来源；隔离真实 DeepSeek 长 Markdown 验收完成读取、两条需求与来源映射。
- 多供应商密钥边界：本地模型配置从单个当前 provider 密钥升级为 provider 级 DPAPI 密钥映射，切换默认模型不会覆盖其他供应商的加密 Key。新增 Kimi / Moonshot OpenAI-compatible profile，按其请求字段与采样限制适配；连接和首轮 Tool Calling 已验证，最终结构化文档输出仍需额外收敛验证，未把它冒充为默认已验收链路。
- 产品范围校正：文档助手当前只读理解 MVP 与“完整文档工作台”拆开记录。PDF/OCR、多文档、创作、局部改写、审校、DOCX/PDF 渲染和版本管理已形成待确认 v1 草案，未提前实现。

- Commander 文档委派闭环：明确文件名的文档理解任务改为 `document_agent/analyze_document`，正式委派给与文档助手页面相同的 `AgentRunner`。纯搜索仍保留 `document.search_text`，避免无意义模型调用；缺少明确材料时先请求澄清。
- 父子任务可追溯：Commander 父任务保留计划、状态和轻量 `agentflow-task://<child_task_id>` 关联产物；文档助手子任务保留完整 Tool trace、来源和结构化结论，避免复制原文或重复审计数据。
- 运行性能边界：`POST /api/tasks/{task_id}/execute` 改为在线程中执行同步 Runtime，文档助手的 async Tool loop 在该线程内运行，避免模型/工具等待阻塞 FastAPI 事件循环。
- 回归调整：旧 Code/Report Runtime 只在显式底层回归中临时标记 ready，并向文档步骤提供明确 workspace 文件名；没有为兼容旧测试放宽多文档选择安全边界。
- 验证：`python -m compileall -q app scripts` 与 `python scripts/verify_backend.py` 已通过，覆盖 Commander 委派、子任务关联、纯搜索路由和权限恢复链路。

- UI 密度收敛：`mainwindow.ui` 将二级页从“全局页标题 + 大型重复 Hero”调整为“全局标题 + 紧凑渐变上下文条”。保留图标、渐变和状态信息，隐藏重复名称、移除 placeholder 页面的大块 fill spacer，并将未实现入口改为禁用状态，避免用户把演示输入误解为可执行功能；文档助手结果区最小高度上调。
- 历史页委派承接：复用既有产物工具栏呈现 `agentflow-task://`，不新塞一块卡片。预览会解释父任务委派关系，打开动作优先定位当前页子任务，必要时刷新第一页后定位；普通受控文件预览和打开逻辑保持不变。
- 验证：UI 变更后 `build/codex-debug` 的 Automatic UIC/MOC、编译与链接通过，`AgentFlow.exe` 成功链接。命令行直接运行需要 Qt Debug DLL 在 PATH；不把该环境差异误记录为 UI 或构建故障。

- 文档助手正式 MVP：新增稳定输入/输出 schema、通用 `AgentRunner`、OpenAI-compatible 与 Anthropic Tool Calls 适配、受控 `document.search_text` / `document.read_text` Tool、来源 ID 到相对路径/行号映射，以及 4 turn / 8 Tool Calls / 同类失败上限。模型不可用时使用确定性 mock 走同一 Runner 与审计链，不消耗真实额度。
- 可用性边界：Agent manifest 与 Registry 已实现 `runtime_ready/health/maturity`；Commander 只规划 `enabled && runtime_ready` 的 Agent。文档助手标记为 MVP ready，Code / Report 标记为 placeholder，旧 Runtime action 仅保留底层回归，不从客户入口伪装为已完成能力。
- 用户入口：`mainwindow.ui` 的文档助手页从占位输入框改为受控工作台，保留图标和 hero 渐变；支持 UTF-8 导入、明确选择材料、输出方式、异步运行状态、摘要/需求/待确认问题/参考来源显示。C++ 只做异步绑定、状态与结果渲染，布局仍由 Qt Designer XML 维护。
- 真实模型收口：OpenAI-compatible Tool 函数名与内部审计名分离，避免供应商拒绝带点号函数名；`document.read_text` 成功后关闭 Tool 获取阶段，强制模型基于已读取来源收束，避免无收益重复搜索。Output Guardrail 兼容模型在 JSON 前附说明/代码围栏及简写清单，但只继承模型已给出的来源 ID，不猜测来源。
- 验证：`python -m compileall -q app scripts`、`python scripts/verify_backend.py` 均通过；离线回归新增文档助手端到端、`runtime_ready` 与 Output Guardrail 断言。隔离临时工作区内的真实模型验收通过：UTF-8 中文 Markdown 经 1 次受控读取后返回带来源的 2 条结构化需求，无重复搜索；未输出或持久化 Key/测试材料。`build/codex-debug` 在本轮前已通过 UIC/MOC、编译和链接；本轮未改 Qt。

## 2026-07-10

- 阶段 5 开工前架构复审：结合当前仓库、初版规划、既有资料、OpenAI/Codex 官方文档，以及 `D:\CC\claude-code-main` 的实现和自审报告，确认阶段 4B 已达到出口，正式进入阶段 5，但首个正式小 Agent 尚未完成。
- Agent 路线校准：采用“Agent Definition + 通用 AgentRunner + 受控 Tool”，确定性单步能力归 Tool；只有职责所有权、工具面、权限、模型或输出契约实质不同才拆 Agent。阶段 5 默认由 Commander 保持 manager 所有权，不提前引入 handoff。
- Agent 可用性校准：规划新增 `registered/enabled/runtime_ready/health/maturity` 分层，避免 manifest 可见、`enabled=true` 或 Node Contract 存在被误解成真实可执行；未来 Commander 只路由 `enabled && runtime_ready` 的 Agent。
- Runtime 约束升级：模型可见上下文和 Runtime 本地上下文分离；审批作为原 run 的可恢复 interruption；Guardrail 分输入/工具/输出；并发预算要预留或原子扣减；后台任务必须主动发完成/失败/取消/阻塞终态。
- Claude Code 参考重新定级：本地项目是第三方复原/扩展工程，不作为官方事实源。吸收 Ports/Adapters、journal、工具预算、稳定事件 ID 等思想，同时记录其动态脚本沙箱、路径越界、并发预算竞态、静默吞错和终态通知断链等反例，阶段 5 不照搬 JS workflow。
- 文档助手待讨论草案已加入 Agent 规格：第一版限定受控 workspace 内 UTF-8 txt/md/markdown，只读 search/read，模型负责结构化摘要/需求抽取并追踪来源；PDF/Word/RAG/Shell/原文修改不进入 MVP。草案仍需用户确认后才能实现。
- 本轮只更新文档，没有修改后端或 Qt 行为；验证范围为 UTF-8、文档一致性、阶段与下一步检查。

## 2026-07-09

- 命令运行请求预览：`POST /api/workflow/command-policy/check` 在静态风险和执行预案之外，新增 `runtime_request_status/runtime_ready/permission_required/approval_prompt/block_reason_code/audit_record_preview`，把“可进入执行请求 / 等待批准 / 平台阻止”变成结构化字段。当前仍不执行 Shell，只为未来 Runtime Shell 工具准备审批和审计骨架。
- Qt 代码工坊同步展示运行请求状态、批准提示、阻止原因码和审计预览键，避免用户把静态检查误解成已经执行，也方便后续接入正式审批流。
- 命令风险可恢复提示：高危规则和检查响应新增 `safer_alternatives`，为递归删除、Git 强制恢复/清理/强推、远端脚本管道执行、数据库破坏性操作、Kubernetes 删除、Terraform destroy、Docker prune 等风险命令提供“先只读确认范围 / 生成清单 / 人工确认 / dry-run 或 plan”一类更安全下一步。
- Qt 代码工坊命令安全检查同步展示“更安全的下一步”，仍然不执行 Shell，只作为正式代码工坊 Agent 开工前的 Governance 解释层。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖规则目录和命令检查响应中的 `safer_alternatives`；`build/codex-debug` Qt CMake 构建已通过；本轮触碰文件 UTF-8 检查通过。
- 命令治理规则目录：新增 `GET /api/workflow/command-policy/rules`，以稳定 API 返回规则 ID、分类、默认动作、原因和破坏性提示；目录不暴露内部正则，也不执行命令，给后续 UI 说明、审计导出和 Runtime 壳复用。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖命令规则目录关键 ID、默认 block 动作和“不暴露内部正则”；本轮触碰文件 UTF-8 检查通过。
- 命令治理规则可审计：高危命令规则从单纯中文原因升级为 `rule_ids` + `destructive_warnings`，覆盖递归删除、Git 强制恢复/清理/推送、远端脚本管道执行、数据库 DROP/TRUNCATE/无 WHERE DELETE、Kubernetes 删除、Terraform destroy、Docker prune 等常见破坏性操作。
- Qt 代码工坊同步展示命中规则和破坏性提示，仍然只做执行前静态检查，不执行 Shell，不代表正式代码工坊 Agent 已开工。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖高危命令 `rule_ids/destructive_warnings`；`build/codex-debug` Qt CMake 构建已通过；本轮触碰文件 UTF-8 检查通过。
- 命令安全检查接入运行偏好预期：`POST /api/workflow/command-policy/check` 现在会在静态风险分类之外，结合当前权限策略返回 `effective_permission_policy/effective_action/effective_reason`，用于解释该命令预计会被放行、等待确认还是平台阻止。
- 代码工坊展示收口：Qt 解析新增字段，并在“命令安全检查”结果中展示当前权限模式和策略说明；这仍然只是 Governance 可视入口，不执行命令，不代表正式 Code Agent 已开工。
- 命令执行预案：继续参考 Claude Code 的工具边界和 cwd/危险命令思路，命令检查响应新增 `execution_scope/execution_route/cwd_policy/sandbox_hint/audit_fields/execution_notes`；Qt 结果框同步显示执行路线、cwd 规则、沙箱提示、审计字段和执行注意事项。当前仍不执行 Shell，只为未来 Runtime Shell 工具预留协议。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖默认 `smart_confirm`、`full_access` 联网命令放行预期、高危命令硬拦截和命令执行预案字段；`build/codex-debug` Qt CMake 构建已通过。

## 2026-07-08

- 后端受控产物预览接口：新增 `GET /api/tasks/{task_id}/artifacts/{artifact_id}/preview`，runtime 文本产物只允许从 `data/outputs` 下读取，并限制最大读取字节数；dry-run 虚拟产物返回不可预览原因。
- 安全边界：预览响应隐藏后端绝对 `output_path`，并拒绝非受控路径、缺失文件、非普通文件和非文本类产物，后续 Qt 预览按钮可切换到该接口。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖 dry-run 不可预览、runtime 可预览、截断读取和绝对路径不泄露。
- Qt 产物预览接线：`BackendClient` 新增受控预览请求、解析和信号；历史页预览按钮改为异步调用后端接口，返回后弹窗展示可预览状态、读取字节、截断状态、不可预览原因和脱敏元数据；切换任务时会丢弃过期响应，避免弹错任务。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过；`build/codex-debug` Qt CMake + Ninja 构建已通过。本轮未启动 GUI 烟测。
- Claude Code 参考工程吸收：开始把 `D:\CC\claude-code-main` 作为架构参考源，优先借鉴五层架构、工具即能力、权限即边界、上下文预算、子 Agent 类型拆分和长任务预算思想，不直接照搬代码。
- 命令安全策略：新增 `backend/app/workflow/command_policy.py` 和 `POST /api/workflow/command-policy/check`，可静态分类只读、诊断、修改、联网和高危命令，返回是否允许、是否需要确认、并发安全、默认超时、输出截断建议和风险原因；当前不执行 Shell，只为代码工坊和未来 Runtime 命令工具打底。
- 文档同步：`docs/AGENT_ENGINEERING_GUIDE.md` 补充 Claude Code 工程参考取舍，`SKILL.md` 补充参考工程使用规则，`docs/PROJECT_STATUS.md` 更新命令治理能力和验证基线。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖 `rg` 只读、`python -m compileall` 诊断、`pip install` 联网和 `rm -rf && git reset --hard` 高危分类。
- Qt 代码工坊命令检查入口：在 `mainwindow.ui` 中新增“命令安全检查”卡片，保留代码工坊原有 hero 渐变、图标和标题；`BackendClient` 接入命令策略 POST 接口，`MainWindow` 负责按钮、回车、请求中状态、风险徽章和结果说明。
- 边界：这一步只是代码工坊 Agent 正式实现前的 Governance 可视入口，不执行命令，也不等于确认了 Code Agent 的完整能力；后续仍需先讨论命令白名单、审批策略、工作目录和高危禁止项。
- 验证：`build/codex-debug` Qt CMake 构建已通过；`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过。
- 运行偏好配置闭环：新增 `GET/PUT /api/settings/runtime-preferences`、`runtime_preferences.json` 本地仓储和 Qt 系统设置页“运行偏好”卡片，可保存权限确认模式和 Agent 语言风格；Commander 的 `preference_applied` 会读取该设置作为计划偏好快照。
- 边界：运行偏好只影响默认确认策略和表达风格，不保存任务正文或密钥，也不允许绕过 Runtime 权限边界和审计记录。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过；`build/codex-debug` Qt CMake 构建已通过。
- 计划偏好可核对：Qt 已解析 `workflow_plan.preference_applied` 的完整快照，并在调度台和历史计划摘要中展示本次权限策略与语言风格；旧历史任务缺字段时使用兼容默认值，展示结果不参与 Runtime 授权。
- 验证：UTF-8 检查、`python -m compileall -q backend\app backend\scripts`、`python backend\scripts\verify_backend.py` 和 `build/codex-debug` Qt CMake 构建均已通过；本轮未改布局，未启动 GUI 烟测。
- 真实模型人格生效：LLM 聊天会把 `professional/concise/warm/creative` 映射为受控表达指令，同一次请求只读取一次偏好并与 Commander 计划快照共用；未知值回退专业风格，提示词明确禁止人格影响事实、权限、安全边界和验证标准。
- 验证：新增四种人格和未知值回退的离线断言，`compileall` 与 `verify_backend.py` 均通过；未发起真实模型请求，不消耗 API 额度。
- Permission Policy 执行闭环：新增确定性策略裁决层并接入 Runtime。受控文件写入可在 `auto_approve/full_access` 下自动批准；联网、数据库、Shell、插件、未知权限和高风险操作继续确认或硬拦截。自动裁决在请求落库后写入 `platform_policy:*` 决策记录，并生成可见事件，人工决策优先且不会被覆盖。
- 验证：离线策略断言覆盖四种模式、未知值回退、联网确认和高危命令硬拦截；完整 API 回归覆盖自动批准后直接完成、审计记录、事件日志，以及 `smart_confirm` 下等待、拒绝、批准和恢复。`compileall`、`verify_backend.py`、Qt CMake 构建均通过；未调用真实模型。
- Permission Policy 前端可解释性：Qt 权限协议解析出策略、动作和理由；历史页折叠区显示决策来源与审计备注，平台自动批准使用独立徽章和“无需操作”状态，避免与用户手动确认混淆。真实执行弹窗同步展示计划快照中的权限模式。
- 验证：相关 C++ 文件 UTF-8 检查和 `build/codex-debug` Qt CMake 增量构建通过；本轮未改后端行为，未重复运行后端全量回归，也未启动 GUI 烟测。

## 2026-07-07

- 重读项目文档和阶段路线：当前仍处于阶段 4B/5 交界，不直接开写未确认的小 Agent，也不跳到插件、RAG、视觉或打包。
- updates 状态快照补充复盘载荷：`task_state_snapshot.payload` 现在包含确定性 `evaluation` 和 `task_retrospective`，复用已有 metrics/tool-calls/permissions/artifacts，不额外扫描文件、不调用模型。
- Qt 事件流展示复盘卡片：历史页事件流和调度台关键状态事件会显示任务复盘、综合评分、步骤/工具/产物/权限/重试事实、警告和下一步建议，减少用户看原始 JSON 的成本。
- 验证：UTF-8 检查通过；`python -m compileall -q backend\app backend\scripts` 通过；`python backend\scripts\verify_backend.py` 通过；`build/codex-debug` Qt 构建通过。本轮未启动 GUI 烟测。
- 历史页事件流复盘置顶：当事件超过当前展示上限时，最新 `task_state_snapshot` 的复盘会固定显示在事件流顶部，下面仍保留原始事件顺序，避免用户翻不到最终结论。
- 验证：`mainwindow.cpp` UTF-8 检查通过；`build/codex-debug` Qt 增量构建通过。本轮只改 Qt 展示逻辑，未重跑后端全量验证。
- 历史页事件流最近优先：长任务时复盘卡片下方改为展示最近事件，而不是最早的 `connected/task_started` 流水；早期事件仍通过提示说明可在日志和详情区查看。
- 验证：`mainwindow.cpp` UTF-8 检查通过；`build/codex-debug` Qt 增量构建通过。本轮未改后端协议。

## 2026-06-25

- 调度台运行阶段表达收口：dry-run 统一显示为“预演”，runtime 统一显示为“真实执行”，避免用户把预演日志误认为已执行真实工具。
- 调度台执行按钮会按当前状态切换为“提交中 / 执行中 / 等待权限 / 执行完成”，真实 Runtime 提交后不再继续显示一个可误点的“开始执行”。
- updates 状态事件补充用户下一步：预演完成后审查并开始执行，等待权限时进入历史页处理，真实执行完成后查看产物，失败/阻塞时查看步骤和工具审计。
- 验证：`mainwindow.cpp/mainwindow.h/mainwindow.ui` UTF-8 检查通过；`build/codex-debug` 增量构建通过，`AgentFlow.exe` 成功链接。本轮未改后端协议和 `.ui` 布局。
- 调度台 updates 增加低频 HTTP 兜底：当前任务处于非终态时每 3.5 秒刷新，网络失败后 6 秒重试；完成、失败、取消、阻塞、需要澄清或切换任务后停止，避免 WebSocket 短暂断开时界面长期停在旧状态。
- updates 失败信号补充 task_id，调度台和历史页同时请求同一接口时只更新对应任务，避免较晚返回的网络错误串页；旧状态事件缺少 `payload.mode` 时保留当前已知模式。
- 验证：`backendclient.cpp/.h`、`mainwindow.cpp/.h` UTF-8 检查通过；`build/codex-debug` MOC/UIC、编译和链接通过。本轮未运行 GUI/WebSocket 断线实测。

## 2026-06-17

- Commander 文档路径理解链路：用户明确提供 `.txt/.md/.markdown` 受控文档路径，并要求归纳、分析、整理或要点时，计划现在会生成 `document.read_text -> document.extract_requirements`，避免只读取文件却没有产出可供后续 Agent 使用的 `document.context`。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖路径型文档的 `read_text -> extract_requirements` 规划与 Runtime 上下文消费。本轮未改 Qt 源码。
- Runtime 文档提取边界：`document.extract_requirements` 现在会区分普通用户目标摘要和前置文档工具上下文；如果前面已经执行 `document.read_text/search_text` 但没有产生命中或预览，会以 `missing_document_context` 结构化失败，避免后续 Code/Report 基于空上下文假装完成文档分析。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖搜索无命中后提取要求的失败路径。本轮未改 Qt 源码。

## 2026-06-16

- 阶段审查：当前仍处于阶段 4B/5 交界处；4B 最小 Runtime、SQLite 历史、权限审计、受控 outputs 和 Qt 历史页验收面已基本接通，阶段 5 的 Document/Code/Report 仍以安全内置工具链和 Node Contract 先支撑 MVP 演示，正式小 Agent 功能继续按“先讨论方案再实现”的规则推进。
- 历史页上下文展示：Qt 历史详情新增 `document.context` 摘要展示，步骤卡、工具调用卡、产物卡和产物预览弹窗都会显示来源步骤、搜索命中和受控读取预览，帮助用户判断 Code/Report 产物是否真实消费了前置文档上下文。
- 事件流上下文展示：后端 updates 的 `artifact_created/artifact_planned` 事件现在会携带对应 step 输出和 tool_calls 审计；Qt 历史页事件流与调度台事件流可直接展示 `document.context`，不用再从产物名称反推来源。
- Workspace 文档入口：新增 `GET /api/workspace/documents/{document_name}` 安全预览接口，只返回受控 workspace 相对文件名和限制长度文本，不暴露后端绝对路径；Qt 导入成功提示会显示短预览，帮助用户确认导入材料。
- Node Contract 收口：补准 `document.extract_requirements` 契约，明确它消费前置搜索/读取结果，输出 `context`，写入 `document.context`，并暴露上下文相关评估信号；这只是阶段 5 链路契约完善，不是正式小 Agent 功能开工。
- 文档上下文路径收口：`document.read_text` 结果新增 workspace `relative_path`；传给 Code/Report 的 `document.context` 优先使用相对路径，不再携带本地 data 目录绝对路径，减少产物和 UI 暴露本机路径。
- Code/Report 契约对齐：补充 `code.generate_code` 和 `report.compose_markdown` 的 `document_context` 输入契约，并验证生成的代码草稿和报告不写入本地 data 目录绝对路径。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖 workspace 安全预览、Document 提取节点契约、Code/Report 文档上下文契约、文档上下文相对路径和 artifact update 关联 step/tool payload；Qt `build/codex-debug` 构建通过记录仍沿用上一轮，本轮未改 Qt 源码。

## 2026-06-12

- 总指挥方案补充：在 `docs/AGENT_SPECIFICATIONS.md` 中加入澄清问题、完成标准、计划版本、预算预估、执行控制、Agent 冲突处理、任务复盘和工作区边界。
- 命令治理补充：新增命令与工具执行策略草案，把命令分为只读定位、诊断验证、修改型、联网和高危命令；后续代码工坊 Agent 必须单独确认命令白名单、审批策略和禁止项。
- 开发准则同步：`SKILL.md` 已加入命令风险分级、高危命令默认禁止或强确认、保留风险提示和审计记录等规则。
- 总指挥落地规格：补充 `workflow_plan` 协议字段草案、`steps[]` 建议字段、澄清与直接规划规则，以及 MVP 落地验收清单；本轮仍为方案文档，未改运行时代码。
- 总指挥协议落地：用户确认总指挥方案后，后端 `WorkflowPlan` / `WorkflowStep` 已加入 schema/version、计划 ID、意图、完成标准、偏好快照、预算预估、工作区边界、下一步动作、命令策略、成功标准和重试建议等字段；Commander 会填充这些字段，并对明显含糊任务返回澄清问题。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python backend\scripts\verify_backend.py` 已通过，并覆盖新协议字段和 `next_action=ask_clarifying_questions` 路径。
- Qt 调度台接入：`BackendClient` 已解析 `workflow_plan` 的计划摘要、预算预估、工作区边界，以及步骤级工具名、成功标准、命令策略、重试/超时配置；AI 调度台会展示澄清问题、完成标准和预算信息，遇到 `next_action=ask_clarifying_questions` 时右侧进度停在“待补充信息”。Qt `codex-debug` 构建已通过。
- 历史页计划回看：新增 `GET /api/tasks/{task_id}/plan` 按需读取任务对应的 Commander `workflow_plan`；Qt 历史详情接入计划摘要、澄清问题、完成标准、预算预估、工作区边界、计划步骤、成功标准和命令策略。验证：`python -m compileall -q backend\app backend\scripts`、`python backend\scripts\verify_backend.py`、Qt `codex-debug` 构建均通过。
- 调度台执行承接：在 `mainwindow.ui` 的调度台摘要栏增加“开始执行”和“查看历史”按钮；调度台可直接把 dry-run 转入真实 Runtime，提交前再次确认，成功后自动跳转历史页并聚焦 runtime task。需要澄清的计划不会启用执行入口。验证：Qt `codex-debug` 构建通过。
- 调度台运行反馈：根据 runtime updates 缓存当前模式、状态、待权限和产物数量；承接按钮会在“查看历史 / 处理权限 / 查看产物”之间切换，事件流也会提示用户去历史页确认权限或预览产物。验证：Qt `codex-debug` 构建通过。

## 2026-06-11

- 文档整理：`docs/PROJECT_STATUS.md` 改为当前状态、文档分工、最近验证基线、下一步和注意事项，不再保存逐轮临时验证流水。
- 归档策略：有复盘价值的历史开发记录继续写入本文件；用户提供的原始参考资料例如 `docs/飞书文档.txt` 保留为资料源，不在未确认前删除。
- 当前阶段：阶段 4B/5 交界，后端最小 Runtime 已接通安全内置工具，Qt 历史页已能展示运行状态、updates、产物和 verification。
- 最近验证基线：后端 `compileall`、`verify_backend.py` 和 Qt `build/codex-debug` 构建均已有通过记录；真实模型和 GUI 烟测按需要单独运行。
- Agent 方案流程：新增 `docs/AGENT_SPECIFICATIONS.md`，明确每个 Agent 先独立讨论职责、边界、属性、权限和验收标准；本轮只写总指挥 Agent 草案，未进入功能实现。
- Agent 偏好补充：在方案文档中加入权限确认策略、语言风格/Agent 人格、成本偏好、执行风格、输出详细度、记忆开关和失败处理策略；同时在 `SKILL.md` 固化“人格不能越过安全边界、权限策略属于平台”的原则。

## 2026-06-09

- 阶段：阶段 4，Workflow Engine dry-run 收口，开始把权限确认从 UI 提示推进到后端审计闭环。
- 本轮：新增 Runtime 权限请求持久化，dry-run 会把需要确认的敏感步骤写入 `runtime_permission_requests`。
- 后端：新增 `GET /api/tasks/{task_id}/permissions` 和 `POST /api/tasks/{task_id}/permissions/{request_id}/decision`。
- 边界：当前接口只记录 pending/approved/denied 决策，不触发真实文件写入、联网、Shell 或插件执行。
- 本轮补充：新增 `ModelGateway`，把 DeepSeek / OpenAI / Anthropic / Qwen / 自定义 OpenAI-compatible 统一抽象到模型网关层，业务层不再直接拼厂商 API。
- 后端：新增 `GET /api/models/providers`，用于只读查看可用 provider profile 和当前解析状态，后续可直接接 Qt 的模型设置页。
- 前端：Qt 模型密钥页已接入 `/api/models/providers` 只读概览，显示当前运行时、provider profile、transport、默认模型和 Key 配置状态，并支持本地搜索与手动刷新。
- 前端：历史任务页顶部“权限确认”警示条已从日志解析升级为读取 `/api/tasks/{task_id}/permissions`，确认已阅会按 request_id 逐条写入后端 decision 审计。
- 本轮前端补充验证：模型页接入后，`build/codex-debug/AgentFlow.exe` 已通过增量构建；`python scripts\verify_backend.py` 已再次通过。
- 验证：`python -m compileall -q app scripts` 已通过；`python scripts\verify_backend.py` 已通过，并覆盖 pending 查询、approved 写入、SQLite 恢复、retry 权限请求和模型 provider profile 列表；`build/codex-debug/AgentFlow.exe` 已通过构建；`python scripts\verify_llm.py` 已通过。

### 模型配置安全存储

- 本轮：后端新增 `GET /api/models/config` 和 `PUT /api/models/config`，用于读取/写入当前模型 provider、Base URL、模型名和 thinking 设置。
- 安全边界：API Key 只允许出现在写入请求中，保存时使用 Windows DPAPI 加密，响应只返回 `api_key_configured`、`api_key_source` 等脱敏状态。
- 持久化：非敏感配置写入 `data/model_config.json`；配置仓储使用 mtime/size 轻量缓存和临时文件替换，避免频繁解析和半截 JSON。
- 网关：`ModelGateway` 解析顺序调整为 Agent 明确配置优先，其次本地安全配置，再回退 provider 专属环境变量、通用 `AGENTFLOW_LLM_*` 和旧 DeepSeek 兼容变量。
- 验证：`python -m compileall -q backend\app backend\scripts` 已通过；`python scripts\verify_backend.py` 已通过，并在临时 `AGENTFLOW_DATA_DIR` 中确认响应和配置文件都不包含明文测试 Key。

### Qt Debug 退出崩溃修复

- 问题：用户在 Qt Creator Debug 目录运行后退出，出现 `Run-Time Check Failure #2 - Stack around the variable 'window' was corrupted`。
- 修复：`main.cpp` 将 `MainWindow` 从栈变量改为堆分配，并在 `QApplication` 析构前显式释放；`MainWindow::~MainWindow()` 退出时先断开后端/网络异步信号，再停止本窗口自动启动的后端并释放 UI。
- 判断：高度怀疑与 `MainWindow` 头文件新增成员后 Debug 目录旧对象文件/退出期异步信号叠加有关；本次改动强制重编 `main.cpp`，并收紧退出生命周期。
- 验证：`build/codex-debug` 和 `build/Desktop_Qt_6_11_0_MSVC2022_64bit-Debug` 均已通过构建；Qt Creator Debug exe 已启动 6 秒后正常关闭，退出码 0；关闭后 `8765` 无残留监听。

## 2026-06-08

- 阶段：阶段 4，Workflow Engine dry-run 收口。
- 本轮：历史任务页接入 `cancel/retry` 控制按钮，按钮位于右侧详情卡内，布局仍维护在 `mainwindow.ui`。
- 前端：`BackendClient` 新增任务控制响应结构、`requestTaskCancel()`、`requestTaskRetry()`，窗口类只处理信号、状态刷新和任务定位。
- 交互：`retry` 成功后会刷新历史列表，并尽量定位到新生成的 dry-run 任务；控制请求失败时会在右侧详情区显示错误。
- 验证：`build/codex-debug` 目录下 Qt CMake + Ninja 构建已通过；`backend/scripts/verify_backend.py` 已通过。

### 权限确认区

- 本轮：历史任务详情改成顶部“权限确认”警示条，布局继续维护在 `mainwindow.ui`，不再只把确认需求混在日志文本里。
- 前端：`MainWindow` 从任务日志中提取 `confirmation_required` 事件，生成敏感步骤复核列表，并提供“确认已阅”按钮。
- 边界：当前仍是 dry-run 阶段，“确认已阅”只表示本次查看已复核；细节默认折叠，避免右侧详情区被单一模块切碎。
- 验证：`build/codex-debug` 目录下 Qt CMake + Ninja 构建已通过；Qt Creator 的 `build/Desktop_Qt_6_11_0_MSVC2022_64bit-Debug` 目录也已通过构建；`uic` 校验已通过。

### Runtime 权限协议壳

- 本轮：在 `backend/app/schemas/workflow.py` 中新增 `RuntimePermissionRequest` 和 `RuntimePermissionDecisionRecord`。
- 用途：后续真实执行器在写文件、联网、Shell、插件调用前先创建权限请求，前端或安全策略再写入决策记录。
- 边界：当前只定义协议模型，不接真实执行，不改变现有 dry-run 行为。
