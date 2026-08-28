# AgentFlow

AgentFlow 是一个 C++ Qt 桌面端 + Python FastAPI 后端的多 Agent 工作流平台。当前目标不是一次性做完完整商业系统，而是按阶段跑通一个可演示、可扩展、可打包的最小闭环：Qt 前端启动本地后端，后端提供 Agent 注册、聊天、任务和 WebSocket 日志，Commander Agent 只调度已正式就绪的内置 Agent；通用 Code Agent 已取消自研立项，原 Report 方向已并入 Document Agent。

## 核心文档

- 当前进度与最近验证：[docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md)
- 阶段路线与门槛：[docs/DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md)
- Agent 方案确认：[docs/AGENT_SPECIFICATIONS.md](docs/AGENT_SPECIFICATIONS.md)
- 数据工作台 D5 产品规格：[docs/DATA_WORKSPACE_PRODUCT_SPEC.md](docs/DATA_WORKSPACE_PRODUCT_SPEC.md)
- 本地知识库产品与工程规格：[docs/KNOWLEDGE_BASE_PRODUCT_SPEC.md](docs/KNOWLEDGE_BASE_PRODUCT_SPEC.md)
- K7 扫描件 / 图片型 PDF OCR 计划：[docs/K7_OCR_PRODUCT_PLAN.md](docs/K7_OCR_PRODUCT_PLAN.md)
- 知识库 K0.3 契约与迁移设计：[docs/KNOWLEDGE_BASE_K0_CONTRACT.md](docs/KNOWLEDGE_BASE_K0_CONTRACT.md)
- Agent / Harness 工程方法：[docs/AGENT_ENGINEERING_GUIDE.md](docs/AGENT_ENGINEERING_GUIDE.md)

## 当前阶段

日期：2026-08-21

阶段判断：**项目处于阶段 5“内置 Agent MVP”；文档助手的只读理解、关键信息卡、结构化大纲、Markdown 草稿预览/确认保存、分章节创作预览、草稿事实核验、本章只读审校、单建议与多建议修订预览、版本链 v1、历史快照恢复预览、父版本双栏差异、受控手动章节修订与重新核验、固定内置模板交付预览、同根草稿三方章节合并预览、跨文档问答/整合/对比、长文档上下文压缩，以及 PDF/DOCX 解析扩展已完成。** 后端骨架、Qt 通信、Agent Registry、Workflow、SQLite、权限/审计、任务历史和 ModelGateway 已支撑正式 AgentRunner。文档助手可从 Qt 页面导入并选择受控 txt/md/markdown、PDF、DOCX，使用受控 search/read Tool 生成带来源的结构化结论；文本引用按行，PDF 引用按页，DOCX 引用按段落或表格。Markdown 草稿可在详情页命名、二次确认后保存到项目根 `output/document_drafts/`；后端只写 UTF-8 Markdown、默认拒绝覆盖同名文件，并将产物和保存事件追加到原任务历史。用户还可从已完成草稿中选定一个章节、输入本章指令，派生独立任务重新读取同一受控材料，得到仅含该章节、保留原章节 ID/标题且带来源的创作预览；也可在不改写草稿、不修改已保存 Markdown 的前提下重新读取相同材料，核验哪些表述有来源支持、哪些仍待确认，或审校单章的原文片段、候选建议、理由和材料依据。审校后，用户可选择一条建议，或勾选同章的 2 至 6 条建议生成“修改前/修改后”的独立版本预览；批量模式要求每个原文片段唯一、彼此不重叠，并按原文位置安全合并。每个可保存草稿快照还会记录当前版本、根草稿和直接父任务、结果详情与 Markdown artifact 可追溯回旧快照；用户可从详情或历史任务把任一已完成文档草稿快照恢复为新的独立预览，并在独立双栏阅读窗口与直接父版本对比。已核验草稿还可在独立“模板与交付”工作区选择项目方案、PRD 或会议纪要；系统只重组已有章节和来源、列出未匹配结构，不调用模型、不读取材料、不写文件，确认后仍复用 Markdown 命名与二次确认保存。对于同根的两份已核验完整草稿，系统会找共同祖先后做三方章节比较：未同时改动的章节自动保留，冲突必须由用户逐项选择当前、候选或共同祖先版本，再建立独立 `merge_preview`；旧任务、旧文件和原正文都不被覆盖。项目方案 PPT v1 已支持从已核验草稿生成逐页计划，经用户确认后写出可编辑 `.pptx`，并重新打开文件核对页数、标题、内容要点和来源页后才写入任务历史。OCR、Excel、自由 PPT 编辑、完整自由编辑版本链、自定义企业模板、DOCX/PDF 写入交付尚未实现；遗留 Code 和 Report 页面仍可见但未就绪，Code 已取消自研立项，Report 产品能力已并入 Document Agent。

阶段 5 采用纵向推进：文档助手已从 Qt 导入、AgentRunner、搜索/读取工具、结构化提取、来源追踪、任务状态到用户结果跑通，并已用真实 UTF-8 中文材料完成 Tool Calling、连续压缩、“精确搜索零命中后受控回读”、跨文档问答与整合验收；直接入口采用“即时受理 -> WebSocket 阶段事件 -> 已校验结果”。分章节预览和草稿事实核验沿用同一 Runner：前者只返回一章，后者只返回来源支持与待确认问题；两者都由 Runtime 恢复原草稿范围、重新读取受控材料，任何失败都不覆盖用户当前草稿。历史恢复预览只复制已完成任务中的 Pydantic 快照，作为新的直接子版本；父版本差异则是 SQLite 快照的只读双栏比较，二者均不调用模型、Tool 或文件写入。现在还完成了受控手动章节修订、固定内置模板交付预览与同根草稿三方合并预览：后者只恢复两个已核验版本和共同祖先，冲突必须逐项由用户选定，随后才创建新的独立版本。项目方案 PPT、项目文档审查与论文审查的首个真实交付闭环现已完成；Report 不再独立规划，通用 Code Agent 不再自研。Commander 保持 manager 所有权，小 Agent 是有边界的专业能力；新的专业 Agent 一次讨论一个，用户确认后才进入规格和实现。

本轮更新：项目方案 PPT v1、项目文档审查 v1 与论文审查 v1 已完成。PPT 只消费来源已核验的 Markdown 草稿，自动执行项目材料预检后再返回逐页计划；客户仅需确认最终写入，系统才生成可编辑 `.pptx` 到 `output/document_presentations/`。预检复用项目审查的范围、验收、责任、节点、风险依赖与术语规则，但不为每次预览额外创建历史任务；主动“项目审查报告”仍用于需要完整问题清单的场景。系统拒绝过期计划、同名覆盖与任意路径，并会重新打开文件核对页数、标题、内容要点和来源页后才写入任务历史。论文审查独立检查结构、参考文献区、引用映射、图表线索、标题格式和可读性。Qt 主工作台现在直接提供这三项入口：选择材料后，审查报告可直接运行；PPT 自动进入“草稿核验 -> 材料预检 -> 计划预览 -> 用户确认导出”。摘要、问答、提取、大纲、跨文档整合与 PDF 整理仍保留后端兼容/历史回放能力，但不再作为客户主入口。

2026-08-03 更新：PPT 制作 V2.2 已完成可见闭环。客户可从文档助手的“智能制作 PPT”输入一句需求，查看系统生成的创作简报、视觉方向与逐页计划，再明确确认导出新的可编辑 PPTX；计划阶段不联网、不写文件，导出后会回读验证并写入任务历史。导出层已支持封面、议程、图文陈述、信息卡、交付表格、总结与来源等版式；Pexels 是当前唯一批准的图库 Provider，只有用户勾选外图并在确认框授权时才会按封面和指定正文页的语义槽位读取最多六张图片，并保留作者、来源页和许可证说明。未配置素材源或联网失败时会安全降级为内置版式。图片生成、第三方模板、客户数据图表和 PowerPoint 原生动画仍需逐项确认与验收。

2026-08-03 更新：PPT 制作 V3 的第一、二项视觉路线已进入真实反馈修正。四套主题不再只替换颜色，而是分别使用商务、技术、叙事和强调对比的构图规则，并将内容页限制为受控的对比、流程、时间线、关键点、观点与图文版式。内置主题与版式始终生效；配图来源才由客户选择“不添加额外配图 / Pexels 摄影图片 / Seedream AI 配图”，计划快照会锁定选择，确认导出后才调用对应 Provider。Seedream 图片在内存中校验后嵌入 PPTX，默认最多四张、请求无可见水印；任务历史仍只保存模型、页面意图与提示词摘要，不保存图片字节或 Key。已收到首次真实 PPTX 生成成功的客户反馈，当前正在修正 Qt 总超时与内容密度。

2026-08-13 更新：PPT 制作 V3 的第三项已完成通用数据主题的真实端到端表图验收。客户选择“导出时智能补充资料与数据”后，计划阶段只生成研究蓝图；确认导出时默认由已配置模型按受控对象、指标和图表合同直接生成演示数据，并写入可编辑的 PowerPoint 原生 Table/Chart。客户明确选择“联网核验”时，ResearchGateway 才按有限并发检索公开证据；该模式保留来源、单位与期间的严格审查。交付层会重新打开 PPTX，核对每个数值单元格，避免 artifact 声称有图表而文件为空。

2026-08-14 更新：PPT 数据规划不再依赖图表关键词或固定单表。简短主题由专用规划模型主动选择 3 至 5 个互补视图、指标分组和数据量，详细类型/数量则由 Harness 固化为硬合同；客户原句能够确定的对象范围不可被模型擅自扩大。普通创作默认使用明确标注的 AI 数据草稿，联网核验才进入来源优先路径。原生图表支持横向条形、面积、饼图和环形图；Seedream 使用官方单图参数、DPAPI 安全 Key 与有界重试，服务端限流时会回退内置版式。

2026-08-14 更新：PPT 制作 V3 第四项的手动淡入转场已通过客户实际放映。正文点击入场时间线虽已写入 PowerPoint 原生 Open XML 并通过 ZIP/XML 回读，但客户放映时未显示该效果，因此不算已交付并已暂停；恢复时必须先用真实 PowerPoint 最小样本验证，而不能只看内部 XML。文本、图片、Table 和 Chart 始终保持可编辑。详细边界见 `docs/PPT_NATIVE_MOTION_DESIGN.md`。

2026-08-14 更新：DeepSeek Harness 已确定采用官方 Node Runtime 作为 Windows 首期可选 `ExecutionBackend`，并已在项目内精确安装 `@deepseek-ai/dsh@0.1.0-rc.6`，完成无密钥 CLI、默认 profile 与 FastAPI 状态探针。H1 已固定非敏感 `execute_task`/事件/结果契约及 fake 后端回归，并以 `agentflow-readonly` profile 实际回读确认 `read-only` / `ask` 与 28 项默认能力禁用；受控单任务 Bridge 也已实现，但尚未有 Router 或客户 Agent 调用它。官方默认 profile 带有 PowerShell、文件、网页搜索和 `.env` 凭据回退，因此 Node 任务功能开关继续保持关闭，直到完成隔离 `DSH_HOME`、临时凭据与真实事件映射。它不会替换 Commander、ModelGateway、权限审计、任务历史和 Verifier；最终采用目录式安装和桌面快捷方式，不强求物理单文件；现有 Native Runtime 保留默认和回退路径。详细方案见 `docs/DEEPSEEK_HARNESS_INTEGRATION_PLAN.md`。

已有内容：

- Qt Widgets + CMake 前端雏形位于项目根目录。
- 前端已有侧边栏、页面切换、Agent 页面占位、图标资源和 QSS 主题。
- Qt 前端已通过 `QNetworkAccessManager` 接入 `/health`、`/api/agents` 和 `/api/tasks`；任务历史页已能分页、筛选并读取任务日志。
- Qt 调度台已接入 `/api/chat`，并会把模拟回复、`workflow_plan` 和 WebSocket 任务日志写入界面。
- 后端已新增 `ModelGateway` 和 `GET /api/models/providers`，模型供应商不再硬编码为单一 DeepSeek，当前可按 profile 统一接入 DeepSeek、Kimi / Moonshot、OpenAI、Anthropic、Qwen 和自定义 OpenAI-compatible 入口。
- 后端已新增 `GET /api/models/config` 和 `PUT /api/models/config`，本地模型配置会写入 `data/model_config.json`；API Key 按 provider 分别保存 Windows DPAPI 密文，切换默认模型不会覆盖其他已配置供应商，接口响应不回显 Key 明文。
- 后端已新增 `POST /api/models/test`，可用当前表单的 provider/base_url/model/thinking/API Key 做一次轻量连通性测试；测试不会保存配置，也不会回显 Key 明文。
- Qt 模型页已接入 `/api/models/providers`、`/api/models/config` 和 `/api/models/test`，能展示当前运行时、provider profile、transport、默认模型和 Key 配置状态，并支持本地搜索、手动刷新、保存配置、写入新 Key、清空本地 Key 与保存前测试连接。
- Qt 历史任务页顶部“权限确认”警示条已接入 `/api/tasks/{task_id}/permissions` 和 decision 接口，“确认已阅”会把 pending 权限审计写回后端。
- Qt 启动时会通过 `BackendManager` 探测或自动启动本地 FastAPI 后端。
- 后端已新增 `AgentRegistry`，从内置 manifest 读取总指挥、文档助手、代码工坊和遗留报告助手；后两项均为未就绪的历史兼容占位，Report 产品能力并入文档助手，通用 Code Agent 不再自研。
- `AgentRegistry` 已加入 manifest `mtime/size` 轻量缓存，常规请求不会重复解析 YAML。
- 项目根 `agents/` 已作为用户 Agent manifest 目录预留，当前只扫描元数据，不执行插件代码。
- 后端已新增 `Commander` 初版规划服务，mock 模式和真实 LLM 模式共用同一套结构化计划生成逻辑。
- `workflow_plan` 已预留 `required_permissions`、`risk_level`、`requires_confirmation` 和 `validation_errors`，为后续执行前确认做准备。
- `workflow_plan` 已包含 `summary`、`max_risk_level`，每个 step 已包含 `reason` 和 `expected_output`，方便用户审查 Commander 为什么这样拆任务。
- Commander 初版规则规划已能识别用户输入中的 `.txt/.md/.markdown/.pdf/.docx` 受控文档线索，并把明确文件名交给正式 Document Agent；真正的 workspace 边界仍由 Runtime 校验。
- 后端已新增 `GET /api/workspace/documents` 和 `POST /api/workspace/documents`，可列出/导入受控 workspace 文档；支持 1MB 以内 UTF-8 txt/markdown 和 10MB 以内 PDF/DOCX。文本走 UTF-8 内容协议，PDF/DOCX 走受限 Base64 传输；后端不接收任意本机路径。
- 文档助手保留 PDF 整理基础能力：支持合并、提取、旋转和删除页面；操作确认后只在 `output/document_processing/` 生成新 PDF，后端重新打开并校验页数，原文件不会被修改、覆盖或删除。该能力当前不占用文档助手主入口，后续仅在确认真实客户场景后再决定是否独立呈现。
- 后端已新增 Workflow Engine dry-run：`/api/chat` 返回 `workflow_run`，WebSocket 会按 `task_id` 推送对应 dry-run 步骤日志，并在敏感步骤前发出 `confirmation_required` 警告事件。
- 后端已新增 `GET /api/tasks`，可分页查询工作流历史任务摘要，并支持按 `status`、`mode`、`max_risk_level`、`requires_confirmation` 筛选。
- 后端已新增 `GET /api/tasks/{task_id}`、`GET /api/tasks/{task_id}/steps`、`GET /api/tasks/{task_id}/metrics`、`GET /api/tasks/{task_id}/evaluation`、`GET /api/tasks/{task_id}/runtime-state`、`GET /api/tasks/{task_id}/artifacts`、`GET /api/tasks/{task_id}/tool-calls` 和 `GET /api/tasks/{task_id}/logs`，用于查询任务状态、step 级结果、执行预算/运行指标、任务效果评估、Runtime 状态机快照、产物、工具调用和日志。
- 后端已新增 `GET /api/tasks/{task_id}/permissions` 和 `POST /api/tasks/{task_id}/permissions/{request_id}/decision`，用于记录和查询权限请求审计。
- 后端已新增 `POST /api/tasks/{task_id}/execute`，可从 dry-run 派生 runtime task，也可在 runtime task 权限批准后继续执行；当前只写受控 outputs 目录，不执行 Shell、用户代码、联网或插件。
- Qt 任务历史页已接入 `/api/tasks`、`/api/tasks/{task_id}/steps`、`/api/tasks/{task_id}/metrics`、`/api/tasks/{task_id}/evaluation`、`/api/tasks/{task_id}/runtime-state`、`/api/tasks/{task_id}/artifacts`、`/api/tasks/{task_id}/tool-calls` 和 `/api/tasks/{task_id}/logs`，支持状态/模式/风险/确认需求筛选、当前页关键词搜索、分页、选中任务 step 概览、运行指标、任务评估、工具调用、产物和日志查看；静态布局已收回 `mainwindow.ui`，并保留原有图标、`heroCard` 背景和整体视觉层级。
- Qt 任务历史页已增加当前选中任务轻量自动刷新，并在详情区下方增加紧凑产物工具条：可选择产物、弹窗预览、复制受控路径/URI，并只对后端声明的 `agentflow-output://` runtime outputs 文件启用打开。
- Qt 任务历史页已提供显式“开始执行/继续执行”按钮，可调用 `POST /api/tasks/{task_id}/execute` 从 dry-run 派生 runtime task，或在权限批准后继续 runtime task。
- 调度台可通过输入框旁的项目范围图标选择 `global` 或 `project:<稳定标识>`，用于隔离长期记忆检索；任务历史仅对已完成 Runtime 总指挥任务提供“记住约束”，候选必须经过可编辑的明确确认才会保存。
- 后端重启时，遗留的 Runtime 不会被自动重跑：`pending/running/waiting_permission` 会安全转为 `blocked` 并保留审计，客户复核后可 retry 创建新的执行记录。
- Qt 调度台“＋”按钮与文档助手共用 workspace 导入：支持 UTF-8 txt/md/markdown、PDF、DOCX；保存后的文件名会自动带入任务输入框，Commander 可把明确的 PDF/DOCX 文件名委派给文档助手。
- Qt 调度台的 `/api/chat` 请求已单独放宽到 120 秒超时，避免真实模型调用被 3 秒短超时误杀。
- 后端已新增 `POST /api/tasks/{task_id}/cancel` 和 `POST /api/tasks/{task_id}/retry`；当前 dry-run 已完成任务不可取消，retry 会基于缓存计划生成新的 dry-run。
- 后端已新增 SQLite 最小持久化，dry-run/runtime 任务状态、计划、step 级结果、执行预算/运行指标、Runtime 状态、产物、工具调用、日志和权限审计会写入 `data/agentflow.db`。
- DeepSeek OpenAI-compatible 真实聊天已可选接入，开发默认模型为 `deepseek-v4-flash`，但 DeepSeek 只是默认示例配置，不是架构上唯一模型入口。
- `backend/` 已有 FastAPI 最小后端，包含健康检查、Agent Registry、模拟聊天和 WebSocket 任务日志。
- 初版规划文档为 `AgentFlow_初版规划.md`。
- 分阶段开发路线沉淀在 `docs/DEVELOPMENT_ROADMAP.md`，用于区分 MVP、Beta 和长期版，避免范围失控。
- Agent 工程方法沉淀在 `docs/AGENT_ENGINEERING_GUIDE.md`，后续 Runtime、Tool、评估、记忆和成本控制优先参考它。

下一步优先级：

1. 保留真实模型的输出恢复、超时、来源收束和性能回归，不为扩展功能破坏现有 Harness 基线。
2. 文档助手后续聚焦三个已认可方向：文件转换与处理、智能文档/PPT 制作、文档审查；摘要、问答、提取、大纲和整合作为内部能力或兼容入口。
3. PPT V3 第三步已完成；第四步已交付原生淡入转场，正文点击入场未通过客户实际放映验收并暂停。后续功能继续按确认顺序推进；若恢复动画，先完成真实 PowerPoint 最小样本验收，再讨论有限的动效偏好和主题化节奏，同时保持无动画降级和可编辑性。
4. 数据工作台 D1-D5.4 已完成工程 MVP：单个 Excel/CSV 的受控导入、画像、白名单聚合、可编辑 Excel 交付、下一步建议、确认后的 PNG 图表看板、安全字段加工新副本，以及 Commander 的单数据集只读委派。D5.3 支持四则计算、日期拆分、排名/占比、分段、累计/环比和文本首尾清理；所有加工先在内存预览，确认后才以 `202 -> 事件流 -> 终态补读` 新建 `output/data_transformations/` 下经过工作簿回读验证的 Excel 副本。D5.4 仅允许总指挥显式绑定一份已导入数据集，创建本地只读分析子任务并回传脱敏结论、源哈希和统计数量；不传递原始/预览行，不生成或修改文件。数据 Agent 的 `runtime_ready` 仅对该 action 生效，Qt 客户交互复核仍待完成。
5. 原报告助手并入文档助手；RAG 算法和索引作为平台 Retrieval Service，获批的 Knowledge Agent 持有资料生命周期、可信问答和深度任务，Evaluator/Verifier 继续作为共用验证层。通用 Code Agent 已取消自研；LangGraph、插件、Shell、Redis 和真正单文件 exe 只按已确认专业能力的实际需求推进。
6. DeepSeek Harness H2 可做隔离只读真实试点；数据工作台 MVP 使用 Native Runtime，Router、H3 写入/批准、H4 MCP 和 H5 发行继续按真实场景进入。

## 技术路线

桌面端：

- C++17 或更高版本
- Qt 6 Widgets
- CMake
- `QNetworkAccessManager` 用于 HTTP
- `QWebSocket` 用于任务日志和流式输出
- `QProcess` 用于启动和管理本地后端进程
- Qt 入口固定使用 Fusion 样式，主要视觉仍由 `mainwindow.ui` 的 QSS 接管，避免 Qt 6.11 Debug 下 modern Windows style 插件导致启动期崩溃。
- 当前暂不批量启用运行时 `QGraphicsDropShadowEffect`，避免 Qt 6.11 + MSVC Debug 退出期触发堆损坏；视觉层次优先由 QSS 和 Designer 布局维护。

后端：

- Python 3.11+
- FastAPI
- Uvicorn
- Pydantic
- SQLite
- asyncio

Agent 与工作流：

- 自研 Agent Definition、通用 AgentRunner、Agent Registry、Tool Registry 和 Workflow Engine 作为第一版基础
- 所有真实 LLM 调用统一走 `ModelGateway`，Agent / Workflow / Tool 层不直接拼厂商 API
- OpenAI Agents SDK、LangGraph 等当前只作模式参考，未来如接入也只能位于可替换 adapter 层，不能破坏多供应商与现有任务/权限/Qt 协议
- 当前 `BaseAgent` 只定义接口边界，manifest entrypoint 也只是预留；阶段 5 先做受控内置 AgentRunner，不动态执行第三方 Agent 代码
- 确定性单步能力归 Tool；只有职责所有权、工具面、权限、模型策略或输出契约明显不同时才拆新 Agent
- `registered/enabled/runtime_ready/health/maturity` 分离，Commander 只把真正可执行的 Agent 放入 runtime 计划
- 阶段 5 采用 Commander manager 模式，专业 Agent 作为有边界能力；handoff 等确有专家接管会话需求时再引入
- 模型可见上下文与 Runtime 本地上下文分离，审批作为原 run 的可恢复 interruption
- 当前先吸收 LangGraph / LangChain 的 State、Node、Graph、HITL、checkpoint 和 updates 事件思想；阶段 5/6 跑稳内置 Agent MVP 后，正式评估把 LangGraph 接入复杂分支、并行、子图或插件工作流
- 后续知识库和代码检索采用 agentic search first：精确定位先走 grep/ripgrep/glob/read，模糊问答、长文档理解和跨文档归纳再由 RAG 补语义
- 工作流计划使用结构化 JSON
- 状态、日志、产物和工具调用都要可追踪

打包方向：

- 第一阶段：开发环境运行 `AgentFlow.exe` + `python -m uvicorn ...`
- 第二阶段：PyInstaller 打包 `backend/agent_server.exe`
- 发布阶段：Qt 主程序 + 后端 exe + agents/data/config/logs 目录一起进入安装包
- 真正单文件 exe 不作为初期目标；更稳妥的商业形态是单安装包或便携目录，桌面只暴露一个启动入口

## 推荐目录

当前 Qt 前端已经在根目录，为避免破坏 Qt Creator 工程，短期保留现状：

```text
AgentFlow/
├─ README.md
├─ SKILL.md
├─ AgentFlow_初版规划.md
├─ CMakeLists.txt
├─ backendclient.cpp
├─ backendclient.h
├─ main.cpp
├─ mainwindow.cpp
├─ mainwindow.h
├─ mainwindow.ui
├─ styles.qss
├─ icons/
├─ backend/
│  ├─ requirements.txt
│  ├─ requirements-dev.txt
│  ├─ main.py
│  ├─ app/
│  └─ scripts/
├─ agents/
├─ data/
├─ docs/
└─ packaging/
```

后续如果前端文件继续增多，再迁移为 `client-qt/src` 结构。

## 阶段路线

阶段 0：项目骨架

- FastAPI 后端可启动
- `/health` 可访问
- `/api/agents` 返回内置 Agent 列表
- Qt 能检测后端状态

阶段 1：基础聊天

- `/api/chat` 支持模拟回答
- Qt 聊天输入和响应显示跑通
- WebSocket 模拟推送任务日志

阶段 2：Agent Registry

- 定义 `BaseAgent`
- 定义 `manifest.yaml`
- 扫描 `agents/` 和内置 Agent
- Qt 显示真实 Agent 列表

阶段 3：Commander Agent

- 根据用户任务生成结构化 `workflow_plan`
- 前端展示计划
- 先用规则和模拟数据，之后再接 LLM

阶段 4：Workflow Engine

- 校验 DAG
- 串行执行基础 steps
- 写入任务状态和日志
- WebSocket 实时推送

阶段 5：内置 Agent 最小闭环

- 先实现通用 AgentRunner 的最小模型工具循环
- 第一个正式 Agent 为 Document Agent：读取/搜索 txt/markdown、PDF、DOCX，提供摘要、需求、关键信息卡、结构化大纲、Markdown 草稿预览/确认保存、分章节创作预览与跨文档理解，并追踪来源
- 通用 Code Agent 已取消自研；遗留 Report manifest/action 不算正式能力，报告交付并入 Document Agent
- 第一条验收链为“上传文档 -> 搜索/读取 -> 提取/回答 -> 来源/错误/历史可见”

阶段 5K：本地知识库与可信检索（K0.1-K0.3、K1.1-K1.6、K2、K3、C4、K4.1-K4.15、C5.1-C5.3、K5.1-K5.8 工程闭环已完成；深度任务已开放资料库页与总指挥的受控入口）

- K0 已固定接口、来源、评估夹具、Pydantic 契约、迁移/版本切换规则，并实测 Windows 下的关键词、向量引擎、Embedding、Rerank 和打包代价
- K1 已完成本地资料库闭环：带 checksum 的 SQLite migration、受控副本/不可变版本、可追溯父子分块、可恢复 FTS5 generation、Chroma/FastEmbed Adapter、后台索引/取消/删除恢复，以及 Qt 的资料库管理工作台。关键词索引无需下载模型；客户明确确认本机 BGE 模型下载后，新 generation 才会额外建立语义向量。
- K2 已完成平台检索核心：只读 Retrieval Service 严格限定活动 generation，融合 FTS5 关键词与可选本地 Dense 候选，以 RRF 和父块去重返回可追溯证据；FTS 或语义索引异常时明确降级，绝不自动下载模型。固定质量/性能夹具与受控只读 API 已具备；模型问答和知识 Agent 路由仍未开放。
- K3 已完成 Evidence Gate、受约束回答与客户任务工作台：只接受 K2 受控证据，重新核验活动 generation、文档版本、父子块与来源锚点；普通问题至少一份资料、比较类问题至少两份独立资料。`POST /api/knowledge/answer` 只把有限活动版本证据交给 ModelGateway，回答的每条 claim 必须回指本轮 `source_id`，模型返回后再核验一次版本。`/answer/start` 使用 `202 -> 真实阶段事件 -> 终态补读` 写入统一任务历史；Qt 在独立可伸缩阅读窗口显示阶段、结论和按需来源侧栏。它不伪造 token 级流式输出；C4 仅对用户显式选择资料库的可信问答开放，`runtime_ready=true` 不代表深度任务已开放。
- K4.1 已冻结活动章节范围并拒绝索引变更后的旧范围；K4.2 已完成最多 24 章节的单章受控 Map、SQLite checkpoint、失败后显式恢复与完成后幂等读取；K4.3 已完成每批最多 6 章、最多 4 批的两级 Reduce、来源闭合和冲突保留；K4.4 已完成无正文 scope 的 SQLite 补读、后台 API 和真实阶段事件桥接；K4.5 已完成同一任务的协作式暂停、继续和取消，控制只在模型回合之间生效，已完成 checkpoint 不会重复执行；K4.6 已让结果补读明确返回章节/Reduce 覆盖、已完成 Map 小结和正式导出资格，部分结果只能预览且不能导出；K4.7 已实现客户确认后的 Markdown 正式报告，固定输出到 `output/knowledge_reports`、回读验证并登记统一历史；K4.8 已在知识库页提供独立深度分析工作台，显示真实阶段、冻结范围、部分/完整结果、暂停/继续/取消和确认导出；K4.9 会在资料库超过 24 章时按目标、文件名和章节标题透明聚焦最多 24 个代表章节，并在范围、结果和报告声明 N/M 覆盖边界，不把聚焦分析伪称整库审计。总指挥深度委派仍未开放。
- 状态修正：以上是 K4.1-K4.9 的历史阶段说明。当前 K4.15 已改为冻结全部活动章节、逐章 Map checkpoint 与每节点最多 6 个小结的递归 Reduce；C5.1-C5.3 已让总指挥在独立预算确认后创建深度子任务、镜像真实状态并深链接到同一工作台。
- K5.1 已完成同一活动 generation 下的本地检索短缓存；K5.2 已完成 Provider Context Cache 能力映射和响应 usage 归一化；K5.3 已累计 K4 深度任务的部分 Provider usage；K5.4 已记录索引阶段耗时及解析复用数；K5.5 会在资料完整 `ready`、版本快照/Profile 不变且无需补建向量时回用原 completed job。K5.6 已在独立目标 generation 写入前，只读复用当前活动 ready generation 中同资料库、同 Profile、同 child ID 与内容哈希的向量；更新/新增或旧目录不可读时重新嵌入。K5.7 已把 K3 有限证据与 K4 Map-Reduce 的实际字符预算、路由及已核验 Provider 长窗口状态写入无正文任务输出；即使能力已确认也不会直接发送整库正文，真实 usage/cache 仍只认 Provider 回执。K5.8 已通过 `GET /api/knowledge/performance` 给出基于真实索引、进程内检索/深度任务耗时、逻辑核数和数据目录可用空间的低/中/高性能建议；索引和深度任务同类 FIFO 串行，低配设备全局串行、中高配最多一条索引与一条深度链同时运行。K6 已通过 8 道合成/脱敏质量题完成收束：关键词与本地 Hybrid 均保持 required `5/5`，Hybrid 已覆盖语义改写缺口；在现有证据下不新增子查询、metadata 或 Rerank。K7.1-K7.3 已完成本地 OCR 选型、协议与受控解析接入：移动 OCR + 方向分类器、可选 requirements、延迟 `OcrAdapter`、受控缓存/ready marker、图片/无文本层 PDF 的区域来源分块，以及旧知识库 SQLite 迁移均已通过离线回归；默认后端仍不安装、不初始化或下载 OCR。下一步只实现客户可见的状态与显式准备入口。
- K7.4.1-K7.4.2 已完成客户可见的 OCR 准备与索引状态：只有客户确认后，后端才异步安装固定的 `requirements-ocr.txt`、执行 `pip check` 并准备约 29MB 本地模型权重；重复点击会去重，Qt 显示真实阶段和活动指示器。缺少可选依赖时，知识库卡片会提供 `安装并准备 OCR` 按钮并说明约 850MB 组件占用、仅安装/模型准备时联网、不会读取或上传资料；依赖已存在时只准备模型。实际扫描材料识别时索引任务显示“正在识别扫描材料”；临时引擎失败页只自动重试一次，空白页不循环，成功页仍按页/区域来源进入索引，材料表显示 `OCR 已完成页/总页`。导入、解析、索引与能力检测均不会自动安装或下载。真实验收须先在 Qt 确认本地准备，再显式运行 `backend/scripts/verify_live_ocr_acceptance.py --live-local --input <扫描材料>`；脚本只在临时副本上输出脱敏统计，且至少一份材料实际进入 OCR 才通过。下一步只做真实扫描件客户验收，不扩大 OCR 范围。
- 客户侧为“知识库”，内部为 `Knowledge Agent + Knowledge Retrieval Service`

阶段 6：Qt MVP 体验

- 把调度台、历史、权限、来源、产物和失败提示整理成无需控制台的用户闭环
- 保持 Designer/QSS 视觉体系和异步状态一致性

阶段 7：打包与运行形态

- PyInstaller 后端 + Qt Release + 便携目录/安装包
- `AgentFlow.exe` 作为唯一用户启动入口

阶段 8+：

- OCR、知识库 K3 之后的大规模/企业增强、`.afagent` 插件、Vision / Media Agent、MCP/A2A 和多平台发布

## 开发原则

- 先闭环，再扩展。
- 先模拟数据，再接真实 LLM。
- 先内置 Agent，再做插件市场。
- 先本地 SQLite，再考虑 PostgreSQL/Redis。
- 先可追踪日志和状态，再追求复杂 UI。
- 先把 ModelGateway 和 Harness 分层做稳，再新增模型供应商或复杂 Agent Runtime。
- 先完成一个 Agent 的纵向闭环，再补下一个 Agent；底层只按已确认 Agent 的真实需要扩展。
- 一个动作能由确定性 Tool 完成时不另造 Agent；多 Agent 拆分以工具、权限、模型、输出契约和所有权差异为准。
- 一级产品功能必须解决具体客户任务并交付可使用的文件、数据或状态变化；不能只把摘要、问答、改写、提取或提纲等通用 LLM 能力做成按钮。
- 功能立项前必须说明普通聊天不可替代的价值、技术可行性、权限、失败边界、质量验证和明确非目标；产品方向获认可后，具体 MVP 仍须逐项确认。
- RAG 算法仍属于平台 Retrieval 服务；已批准的知识库因具备资料生命周期、独立任务和权限边界，可由 Knowledge Agent 持有客户工作台，但不能为每个专业 Agent 复制索引。
- 先完成 `docs/DEVELOPMENT_ROADMAP.md` 当前门槛；知识库按已批准的阶段 5K 推进，插件市场、视觉、多平台和真正单文件 exe 仍不得偷跑。
- 真实 Agent 开发要看任务成功率、执行效率、工具调用准确性、失败收敛速度和成本，不只看模型单次回答是否流畅。
- 长任务优先拆分、摘要和数据库恢复上下文，不把所有历史对话都塞进模型窗口。
- 工具调用必须结构化、可验证、可审计；同类失败要有重试上限和超时，避免 Agent 死循环。
- 每个危险能力都要有权限声明和二次确认预留。
- 所有新增文本文件使用 UTF-8，包含中文的 C++ 源文件要确保 MSVC 使用 UTF-8 编译。

## 运行与验证

后端开发环境：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8765 --reload
```

如果 Qt 自动启动使用 `backend/.venv/Scripts/python.exe`，新增后端运行时依赖后也要同步安装到 `.venv`：

```powershell
cd D:\project\AgentFlow\AgentFlow
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
```

后端离线接口验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python scripts\verify_backend.py
```

总指挥后台 Runtime / 暂停恢复专项回归：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python scripts\verify_commander_runtime_jobs.py
```

总指挥项目范围与任务后记忆建议专项回归：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python scripts\verify_commander_memory_proposals.py
```

总指挥重启后 Runtime 安全停驻专项回归：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python scripts\verify_commander_runtime_recovery.py
```

PPT 制作 V2 离线回归：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_presentation_studio.py
```

PDF 整理 Tool 离线回归：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_pdf_processing.py
```

后端真实端口 + UTF-8 中文往返验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
$env:AGENTFLOW_CHAT_MODE='mock'
python -m uvicorn main:app --host 127.0.0.1 --port 8765
python scripts\verify_live_backend.py
```

真实端口烟测建议临时使用 `mock`，避免 `.env` 中的真实 LLM 配置让每次 smoke test 都产生模型调用；真实模型连通性用下面的脚本单独验证。

真实模型连通性验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python scripts\verify_llm.py
```

文档助手真实 Provider 验收（使用临时 workspace / SQLite，不保存验收材料；可选指定已配置的 provider）：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python scripts\verify_document_agent_llm.py
python scripts\verify_document_agent_llm.py --provider kimi
```

Agent Registry 状态接口：

```text
GET http://127.0.0.1:8765/api/agents/registry/status
```

Qt CLI 构建验证：

```powershell
cmd /c ""D:\IDE\VS2022\buildtools\VC\Auxiliary\Build\vcvars64.bat" && "D:\IDE\qtcreator\Tools\CMake_64\bin\cmake.exe" -S "D:\project\AgentFlow\AgentFlow" -B "D:\project\AgentFlow\AgentFlow\build\codex-debug" -G Ninja -DCMAKE_MAKE_PROGRAM="D:\IDE\qtcreator\Tools\Ninja\ninja.exe" -DCMAKE_PREFIX_PATH="D:\IDE\qtcreator\6.11.0\msvc2022_64" -DCMAKE_BUILD_TYPE=Debug && "D:\IDE\qtcreator\Tools\CMake_64\bin\cmake.exe" --build "D:\project\AgentFlow\AgentFlow\build\codex-debug" --target AgentFlow -j 4"
```

Qt 短跑验证需要 Qt bin 在 PATH 中：

```powershell
$env:PATH = 'D:\IDE\qtcreator\6.11.0\msvc2022_64\bin;' + $env:PATH
Start-Process D:\project\AgentFlow\AgentFlow\build\codex-debug\AgentFlow.exe
```

前端继续使用 Qt Creator 打开根目录 CMake 项目运行。当前开发版已通过 `QProcess` 自动启动源码形态后端；后续打包阶段会把启动入口替换为后端 exe。

当前开发版 Qt 已支持自动启动源码形态后端。启动顺序是先探测 `127.0.0.1:8765`，如果已有手动后端则复用；如果没有，则从 `backend/` 工作目录启动：

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8765
```

可选环境变量：

- `AGENTFLOW_BACKEND_DIR`：指定后端目录。
- `AGENTFLOW_PYTHON`：指定 Python 解释器。

真实模型本地配置位于 `backend/.env`，该文件已被忽略，不要提交。可参考 `backend/.env.example`：

```text
AGENTFLOW_CHAT_MODE=llm
AGENTFLOW_LLM_PROVIDER=deepseek
AGENTFLOW_LLM_BASE_URL=https://api.deepseek.com
AGENTFLOW_LLM_MODEL=deepseek-v4-flash
AGENTFLOW_LLM_API_KEY=replace_me
AGENTFLOW_LLM_THINKING=disabled
```

切换 Kimi、OpenAI、Claude、Qwen 或私有 OpenAI-compatible 网关时，优先改 `AGENTFLOW_LLM_PROVIDER`、`AGENTFLOW_LLM_BASE_URL`、`AGENTFLOW_LLM_MODEL` 和 `AGENTFLOW_LLM_API_KEY`；旧的 `DEEPSEEK_*` 变量仍兼容，但不再作为架构边界。Kimi 的示例在 `backend/.env.example` 中；实际 Key 只保存在本地加密配置或用户自己的 `.env`，不要写入仓库。

需要稳定离线验证时，把 `AGENTFLOW_CHAT_MODE` 设为 `mock`；`scripts/verify_backend.py` 会自动强制 mock，避免消耗真实模型额度。
