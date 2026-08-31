# AgentFlow 项目状态

最后更新：2026-08-31

## 当前仓库状态

- 工作区：`D:\project\AgentFlow\AgentFlow`
- 初版规划：`AgentFlow_初版规划.md`
- 分阶段开发路线：`docs/DEVELOPMENT_ROADMAP.md`
- Agent 工程方法：`docs/AGENT_ENGINEERING_GUIDE.md`
- 前端：Qt Widgets / CMake 工程，当前仍放在仓库根目录
- 后端：FastAPI 骨架位于 `backend/`
- 构建产物：`build/` 内包含 Qt Creator 产物和 `build/codex-debug` 命令行验证产物
- 图标资源：`icons/` 下已有 SVG 图标

## 文档分工

- `docs/PROJECT_STATUS.md`：只记录当前阶段、已具备能力、最近验证基线、下一步和重要注意事项。
- `docs/WORKLOG.md`：记录有复盘价值的历史开发流水，不把逐轮临时验证长期堆在状态文档里。
- `docs/DEVELOPMENT_ROADMAP.md`：记录阶段门槛、技术路线和长期目标。
- `docs/AGENT_ENGINEERING_GUIDE.md`：记录 Agent / Harness / 检索 / 评估方法论。
- `docs/AGENT_SPECIFICATIONS.md`：记录每个内置 Agent 的方案确认表；正式实现 Agent 前必须先讨论并确认。
- `docs/KNOWLEDGE_BASE_PRODUCT_SPEC.md`：记录已批准的本地知识库产品边界、Retrieval 架构、K0-K5 门槛和验收；知识库开发前必须阅读。
- `docs/KNOWLEDGE_BASE_K0_ADR.md`：记录 K0 的固定夹具、Windows 技术试验、依赖取舍和未决风险；进入 K1 前必须阅读。
- `docs/飞书文档.txt`：用户提供的原始参考资料，不作为每轮必读文档，也不在未确认前删除。

## 当前阶段

当前处于：**阶段 5：内置 Agent MVP。文档助手 V1 和数据工作台均已完成当前基础闭环，后续按已确认的客户价值扩展；总指挥已完成 C0-C4、C5.1 全库深度总结受控委派、C5.2 父子任务真实状态镜像与 C5.3 关联深度任务工作台入口。知识库已完成 K0-K4.15、K5.1-K5.7：全库任务冻结全部活动章节，以可恢复 Map/Reduce 执行；同 generation 的本地检索证据可短时复用，ModelGateway 只记录 Provider 实际返回的 cache usage，K4 任务累计部分可观测指标，索引任务记录阶段耗时与解析复用数，版本/Profile 不变时复用已验证 generation；增量索引仅可在同 Profile、同 child ID、同内容哈希下从活动向量代次受限复用向量。K5.7 已对 K3/K4 的实际模型输入写入无正文路由和字符预算，不把已确认的长窗口能力变成整库直灌。资料对照仍只在知识库工作台由客户明确选择材料后启动。**

> 当前阶段以本节为准：阶段 5 内置 Agent MVP；知识库已完成 K4.1-K4.15、K5.1 本地检索短缓存、K5.2 Provider usage 基础可观测、K5.3 K4 任务指标聚合、K5.4 索引性能事实、K5.5 无变化索引快路径、K5.6 受控增量向量复用与 K5.7 上下文路由/预算边界，Commander 已完成 C5.1 的全库深度总结受控委派、C5.2 的父子状态镜像、C5.3 的关联工作台入口和 R5.4A/B/R5.4C 首版数据交付。文档助手与数据工作台是可用的基础闭环，仍保留后续扩展空间；资料对照仍仅在知识库工作台启动。

> **2026-08-31 R5.4B 字段加工自然语言闭环：**AI 调度台现在能在已绑定单个 CSV/XLSX 后识别“新增字段、排名、累计、月份、环比、占比、四舍五入、分段、清理文本和有限四则计算”等目标，先生成不写文件的 `DataTransformIntent v1` 预览，再在客户确认后委派 `data_agent.export_field_transform`。Runtime 复用已有确定性字段加工服务，在 `output/data_transformations/` 中保持原文件类型创建副本，按原字段之后追加最多 12 个派生字段，回读新字段/行数/格式和源哈希后才登记 artifact，并把文件名和新增字段摘要写回同一会话。自然语言不能执行公式、脚本或 SQL，源文件不会被修改。`verify_commander_data_transformation_delivery.py` 与字段加工、数据交付、Commander 回归已通过；本轮未调用真实模型或网络。R5.4C 两份数据关联首版已完成，复杂多表关系仍不自动开放。

> **2026-08-25 补充校正：**知识库 K5.8 已完成。后端现以已保存的索引耗时、当前进程无正文的检索/深度耗时、逻辑核数和数据目录可用空间生成性能建议；同类索引或深度任务 FIFO 串行，低配全局串行、中高配最多一条索引与一条深度链并行。它不保存客户正文、问题、文件名、路径或设备身份；进程队列不会替代 SQLite checkpoint。

> **2026-08-25 K6：**已建立版本化的 8 道合成/脱敏检索质量题，并将 5 道 required 回归与 3 道 diagnostic 失败模式分开统计。关键词与本地 Hybrid 均保持 required `5/5`；Hybrid 已覆盖语义改写的唯一缺口，并用候选验收开关防止回归。Hybrid 的中位/P95 约 `172/396ms`，高于关键词约 `7/16ms`，但当前收益足以支持“本地语义索引准备后走 Hybrid、不可用时明确降级”的既有路线。没有证据支持继续引入子查询、metadata、Cross-Encoder 或 LLM Rerank。评测只输出夹具 ID、聚合指标与耗时，不读取客户材料、问题、文件名或正文。

> **2026-08-27 C6.5.4 调度台客户表达修正：**绑定单一已索引资料库的安全只读问答，会在 dry-run 后自动进入 Runtime，并在调度台直接显示 K3 已验证的正文与精简来源；计划、事件流、日志、Runtime ID、预算与完整审计不再写进聊天区。最终 K3 回答以关联子任务 ID 幂等追加到同一会话归档，重启恢复仍能阅读；完整结果、全部来源和失败诊断继续以任务历史为准。其余写入、联网、确认、多 Agent 或深度任务仍不会自动执行。`verify_commander_c6_planning.py`、`verify_knowledge_answer.py`、`verify_commander_c64_runtime.py`、`verify_backend.py` 与 Qt Debug 构建已通过。

> **2026-08-27 调度台可靠性整改 R0.1-R0.3/R1.2/R2.1/R3.1-R3.3/R4.1-R4.2：**修复会话 URL 把 query 错写入 `QUrl::setPath()` 导致的 `404`，并把工作区文档列表改为纯元数据扫描，避免每次列表都解析 PDF/DOCX/OCR。调度台附件现可按类型受控导入 CSV/XLSX，并通过“添加材料 -> 选择已导入材料”组合选择一份文档、一个资料库和一份数据集；该选择器只同步目录元数据，不会后台画像、解析或调用模型，目录同步时显示真实旋转状态并在终态停止。Markdown 能安全显示表格、引用和行内代码，客户消息在新发与会话恢复时统一右侧显示，Composer/材料栏不再挤占长结果。会话空间与 `@` 中文链路已完成首轮修正；调度台、文档、数据和知识库工作台均已有按真实作用域打开的“本次模型”入口，路由状态在客户点击后才读取，不给启动期增加额外请求。R0.3 追加了启动期健康检查单飞行、端口被非 AgentFlow 服务占用时的明确提示、可见的“重试后端”入口和启动耗时诊断；显式重试不会停止手动启动的服务，也不会在被占用端口拉起第二个后端。R3.3 让文档与数据工作台的主状态行复用真实活动标志：仅在已有列表、画像、分析、交付或审查运行态旋转，终态立即收束，且不会增加网络轮询或模型调用。R3 的其它页面异步状态与 R4 的模型枚举、主动连接测试仍在后续整改范围。专项后端回归、UI XML/UTF-8 检查与 Qt Debug 构建均通过，未调用真实模型。

> **2026-08-28 调度台 R5.1 自然会话与安全自动执行：**已修复数据工作台交接后遗留 `@文档助手` 抢占路由、导致已绑定数据被过滤的缺陷；交接现在清理互斥提示并写入 `@数据工作台`。普通 `direct_answer` 已直接显示真实模型回答，不再被 Qt 固定文案替换成“已生成可执行方案”，也不要求客户点击“开始执行”。对一份已绑定数据集的只读 `data_agent.analyze_dataset`，系统在已校验计划、无写入/联网/确认条件下自动从 dry-run 转入 Runtime，完成后将受控结论与图表建议幂等追加到同一会话；知识库单库问答保持同一原则。复杂编排、多材料、深度分析、联网、写入和命令仍需明确确认。`verify_commander_data_delegate.py`、`verify_commander_c6_conversation.py`、`compileall`、`pip check`、Qt `codex-debug` 构建与 `ctest` 均通过；未调用真实模型或读取客户数据。

> **2026-08-28 调度台 R5.2 交付时机与主题创作引导：**修复了数据只读任务在 dry-run 结束时被 Qt 过早判定为“没有结论”的问题；现在仅在真实 Runtime 完成后读取并显示子 Agent 的受控结论、趋势和图表建议。知识库最终回答增加幂等读取保护，后续状态刷新不再向聊天流重复追加同一份答案。同一会话中的“选择了/选好了/就用这份”等表达会复用此前明确绑定的文档或数据，不跨会话、不猜测材料。`@文档助手` 的一句 PPT 主题不再被当成“缺少文档”：总指挥只打开并预填既有智能制作 PPT 工作台，不自动消耗模型额度或创建文件。离线验证新增文档/数据续聊覆盖；`verify_commander_data_delegate.py`、`verify_commander_c6_conversation.py`、Qt `codex-debug` 构建与 `ctest` 均通过。

> **2026-08-28 调度台 R5.3 任务意图与数据图表交付：**总指挥已改为“任务意图优先、材料按需使用”：上轮残留的 CSV、文档或资料库不会劫持普通聊天；`@Agent` 只保留路由偏好，不过滤其它客户已选材料、更不扩大权限。明确 PPT 主题始终进入智能制作 PPT；明确“分析当前数据”自动委派只读数据分析；明确“制作/导出图表”则生成“分析 -> PNG 图表交付”的受控计划，客户回复“开始执行”后才写入 outputs。图表委派已接入 Action Admission、Node Contract、Runtime、源哈希、PNG 像素回读与父子 artifact 审计。AI 调度台取消固定“开始执行”按钮，改由自然语言确认；过程面板默认收起以让对话/结果占据主空间，应用启动默认最大化。`verify_commander_intent_routing.py`、`verify_commander_data_chart_delivery.py`、既有 C6.6/D5.4 回归、Python 编译和 Qt CMake/Ninja Debug 构建均通过；未调用真实模型、网络或客户材料。

## 2026-08-25 知识库 K5.4：索引性能事实与解析复用核验

- `KnowledgeIndexJobRecord` 与 SQLite migration 已新增解析/分块、向量、关键词、总耗时，以及本次复用的已解析版本数。计量不写正文、路径、向量、模型输入、缓存键或凭据，旧数据库只前向增列，不重建资料。
- 索引任务已核验：`ready/parsed` 的未变文档版本会复用既有受控分块，增量 generation 不重复解析它们；FTS 仍按 generation 隔离重建，本地向量启用时也仍重嵌当前 generation 的子块。没有复制旧 Chroma 目录或宣称不存在的向量缓存。
- `verify_knowledge_migrations.py` 与 `verify_knowledge_keyword_jobs.py` 已覆盖 migration 清单、阶段计量、增量重建中的一份解析复用、FTS 与重启恢复。下一步只在真实指标显示 embedding 成本值得优化时，再评估按内容哈希限定的向量复用。

## 2026-08-25 知识库 K5.5：无变化索引快路径

- 完整 `ready` 资料库再次请求索引时，服务会精确比较当前候选文档版本清单、活动 generation 的版本快照和 Index Profile JSON。三者一致才返回原完成 job，并记录 `knowledge_index_job_reused` 审计；不会创建多余 generation、重写 FTS 或重复嵌入。
- 部分失败、文件/版本或 Profile 改变一律不复用。活动 generation 仍是 `pending` 向量而本地模型已准备时同样强制完整构建，以便补齐语义索引；因此快路径不会掩盖新的可用能力。
- K1.4 索引任务回归已覆盖第二次无变化索引保持 generation 数不变、回用原 job 和审计事实；K2 Hybrid 检索回归仍通过。K4 stale 回归也改为索引同名导入后实际返回的新受控文件名，继续验证真实资料更新必定阻断旧 scope。

## 2026-08-25 知识库 K5.6：受控增量向量复用

- 新 generation 只会从当前活动、`ready` 且向量已就绪的旧 generation 读取向量；同资料库、完全相同 Profile、相同 `child_chunk_id` 和相同 `content_sha256` 是同时成立的硬条件。任何更新、新增、Profile 不同、旧 collection 缺失或单条回读失败都会转为本轮本地嵌入。
- 目标 generation 仍使用独立 Chroma 目录和完整回读验证。系统不会复制旧目录、把向量落入 SQLite、跨资料库/跨 Profile 共享向量，也不会把复用数写成成本节省或 Provider 缓存命中。
- 索引任务新增“本代写入向量数 / 复用向量数 / 新嵌入向量数”三项无正文计量。`verify_knowledge_vector_generation.py` 已覆盖新增资料仅嵌入新增块、更新资料不复用旧块，以及旧向量目录缺失时完整回退；migration、关键词任务、K2 缓存、K4 深度任务和全量后端回归均通过。

## 2026-08-25 知识库 K5.7：上下文路由与预算边界

- 新增无副作用的 `knowledge_context_router`：K3 固定走有限检索证据，K4 固定走单章 Map/分层 Reduce；每次实际构造的系统提示与受控用户消息只记录字符总数、内部字符预算和路由状态，不写 prompt、材料正文、路径或凭据。
- DeepSeek V4 Flash/Pro 的 1M 窗口仅在当前 Provider 与实际模型名均已核验时记为“已确认但未启用”；它不改变 K2/K3 Evidence Gate、K4 checkpoint 或“不得整库直灌”的产品规则。未知/自定义模型保持未确认，真实 token/cache 仍只以 Provider 响应 usage 为准。
- K3 回答任务和 K4 Map/Reduce checkpoint 已持久化该无正文决策；超预算会在请求前停止，不会静默切到长窗口。`verify_knowledge_context_routing.py`、K3/K4 回归、`compileall` 与 `pip check` 已通过。

## 2026-08-25 知识库 K5.8：性能分级与运行队列

- 新增 `GET /api/knowledge/performance`：只读取最近 completed 索引的总耗时、当前进程匿名检索/深度任务耗时、逻辑核数与数据目录所在卷的可用空间，返回低/中/高资源建议、慢阶段提示、磁盘提示和当前进程队列快照。它不读取或保存资料正文、问题、文件名、路径、向量或设备身份。
- 索引和深度任务接入受控后台队列：同类任务严格 FIFO 串行；低配只启用一个全局重任务通道，中高配最多并行一条索引与一条深度链。普通检索、K3 问答不排队；K4 重复继续会被进程内去重，避免第二条协程抢先结束原任务的实时事件流。进程重启后队列与短时观察自然清空，业务恢复仍只依赖 SQLite checkpoint。
- `verify_knowledge_performance_queue.py` 已覆盖 FIFO、异类通道、低层去重、资源提示与 API 队列快照；并复跑 K1.4 索引、K5.1 缓存、K5.7 路由、K4 Map、`compileall`、`pip check` 与 `verify_backend.py`。`TestClient` 仍输出既有 Starlette/httpx 弃用警告，未影响结果。

## 知识库后续开发顺序

1. **K5.6 增量向量复用（已完成）**：仅在同一资料库、完全相同 Profile、相同 `child_chunk_id` 和相同内容 SHA-256 下从活动向量 generation 只读回用；旧向量目录或单条记录不可读时完整回退为新嵌入。
2. **K5.7 长上下文路由（已完成）**：K3/K4 已记录实际受控输入的字符预算与路由；已确认的 Provider 长窗口只作为未来能力事实，当前不启用整库直读。
3. **K5.8 性能分级（已完成）**：无正文性能建议、资源分级、受控后台队列和过载提示已完成；队列不会替代 SQLite checkpoint。
4. **K6 质量与检索升级（已完成）**：8 道合成/脱敏题、关键词/Hybrid 对比和 Hybrid 语义缺口验收均已完成；当前不新增子查询、metadata 或 Rerank。
5. **K7 OCR（K7.1-K7.4.2 工程完成，真实扫描件验收待进行）**：只推进扫描件 / 图片型 PDF 的本地优先 OCR。隔离探针已选定移动 OCR + 方向分类候选；可选 requirements、延迟导入 Adapter、受控缓存/ready marker、图片/无文本层 PDF 的受控解析接入、页/区域来源分块、客户确认后的模型准备，以及索引任务中的真实 OCR 阶段/页级有限重试均已完成。默认后端保持不安装/不初始化 OCR；不做云端视觉、连接器、多人权限或行业包。

**2026-08-25 K7.4.1-K7.4.2 已完成：**知识库现已显示本地 OCR 可选能力与活动指示器；客户确认后才能从异步 `202` 准备入口安装固定 `requirements-ocr.txt`、执行依赖校验并初始化约 29MB 模型权重，重复点击不会重复启动，导入、解析、索引和能力诊断均不会暗中下载。缺少依赖时卡片提供明确的 `安装并准备 OCR` 操作，并说明约 850MB 可选组件磁盘占用、联网边界和资料不上传；已安装依赖时只准备模型。索引任务会在实际扫描材料识别时显示“正在识别扫描材料”；只有临时引擎失败页会自动重试一次，空白页不会循环，成功页仍按页/区域锚点进入检索，材料表会显示 `OCR 已完成页/总页`。新旧 SQLite 均以 `foreign_key_check` 验证前向迁移。**下一步仅为客户真实扫描件验收：**先由客户明确准备可选组件与本地模型，再验证实际 PDF/PNG/JPG 的处理范围、来源与检索；不扩展复杂 OCR 或云端视觉。

**2026-08-25 K7 真实验收准备已补齐：**新增 `verify_live_ocr_acceptance.py`。默认与未带
`--live-local` 时不会读取材料；客户显式传入本机扫描 PDF/PNG/JPG 后，脚本仅在系统临时副本上复用
实际受控解析链，输出不含正文/路径/文件名的路由、页级完成/失败/重试计数和来源锚点数量。它绝不
安装依赖、下载模型、写入 workspace/资料库/任务历史或修改原件。当前开发环境的 PaddleOCR 可选依赖
仍未安装，故真实识别仍需先在 Qt 知识库页明确确认准备，之后再执行 UI 与脚本双重验收。

**2026-08-25 K7 工作台体验修正：**本地 OCR 与语义索引模型的准备项在未就绪时以紧凑状态条显示，准备中
才展开真实阶段，完成后自动收起，不持续占用资料库主工作区。新建资料库、刷新、提交索引和删除均已加入
即时按钮状态与重复操作锁定；索引运行期间禁止再次建立索引或修改材料，删除会轮询到资料库实际移除后才
结束“删除中”。Qt Debug 构建、`verify_ocr_preparation_api.py`、`verify_ocr_index_progress.py`、
`verify_backend.py` 与 `pip check` 已通过；真实扫描件尚未读取或执行。

**2026-08-26 知识库状态与删除修复：**此前材料导入错误地把资料库状态写为 `indexing`，导致没有
Index Job 时 Qt 仍显示“索引中/正在读取资料库材料”并锁住建立索引；现导入 API 明确返回 `queued`，
候选材料只等待客户点击建立索引。新增 SQLite 前向 migration 会安全修复既有的无活动 generation、无
queued/running Job 的错误 `indexing` 记录为 `empty`，真实索引不受影响。软删除维持审计记录但会释放
原名称，历史 deleted 记录在下次同名新建时也会自动释放；deleting 状态仍明确提示等待清理完成。Qt Debug
构建、`verify_knowledge_library.py`、`verify_knowledge_deletion.py`、`verify_knowledge_api.py`、
`verify_knowledge_migrations.py`、`verify_knowledge_keyword_jobs.py`、`verify_ocr_index_progress.py`、
`verify_backend.py` 和 `pip check` 均已通过。

## 2026-08-25 知识库 K5.3：K4 真实 Provider 用量聚合

- 通用 `AgentRunner` 为每次模型回合保留无正文 usage trace；K4 Map/Reduce 对真实 `ModelRuntime` 将该摘要累加到同一可恢复任务的 `RuntimeExecutionMetrics`。一次瞬态失败后的受控重试同样计数，离线 mock 不会被写成 Provider 请求。
- 指标明确区分“Provider 请求总数 / 实际返回 usage 的请求数 / 实际返回缓存字段的请求数”。各 token 合计均为可选字段：Provider 没有返回时保持空值，已有缓存读数不会因后续响应未提供该字段而变成零。SQLite、日志与 UI 不写 prompt、模型正文、API Key、缓存键或账单金额。
- `verify_model_usage_metrics.py` 覆盖多回合聚合与未知 usage；`verify_knowledge_deep_task_map.py` 覆盖真实 Runtime 累加、部分 usage、零 cache hit 和 mock 隔离。任务级指标目前仅落在 K4 深度任务，不等于所有 Agent、长期成本账本或用户可见缓存命中率。

## 2026-08-25 知识库 K5.2：Provider 缓存边界与实际用量计量

- `ModelGateway` 现将不同供应商响应中的 token usage 收敛为无正文的 `ModelUsageMetrics`：输入、输出、总 token、cache read、cache creation 与 DeepSeek cache miss 均保留为可选计量。响应没有 `usage` 时明确标为 `not_reported`，不以字符数估算、不推断未命中。
- 模型 provider API 现同时返回受控缓存协议边界：DeepSeek 为“自动且可观测”，但只有 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 实际出现才算观测；Anthropic 为“需显式请求”，现有 Gateway 尚未发送 `cache_control`；OpenAI 为“响应提供时可观测”；Kimi、Qwen 和自定义兼容入口保持未知或未核验。不会把静态能力映射显示为成本下降、缓存已命中或 100% 保证。
- 新增 `backend/scripts/verify_model_usage_metrics.py`，覆盖 DeepSeek、OpenAI、Anthropic 和缺失 usage 四种夹具，以及 Tool Turn 透传与 profile 映射；`pip check`、`verify_backend.py` 均通过。任务级聚合、长期成本报表、长窗口路由、解析/Embedding/Map 缓存仍是后续 K5 工作，尚未立项为客户承诺。

## 2026-08-24 知识库 K5.1：本地检索短缓存

- Retrieval Service 已增加进程内 LRU 短缓存。唯一缓存对象是已经定位的受控证据包；模型回答、来源 Gate 结论、SQLite 任务历史、长期记忆和外部 Provider 响应一律不缓存。
- 缓存键同时绑定资料库 ID、当前活动 generation、检索 profile、`top_k` 和规范化问题的 SHA-256 摘要。资料重建后 generation 改变会自然 miss；检索过程若发现 generation 在读取时再次切换，会重新读取一次或明确要求稍后重试，绝不把旧来源写入新 generation 缓存。
- 临时 FTS/Dense 故障的 `keyword_fallback` 不进入缓存，避免短暂依赖问题被放大；命中结果按深复制返回，调用方不能污染缓存。Qt 问答阅读与任务 Tool 审计只显示实际 `hit/miss` 和可选年龄，不暴露正文、缓存键、摘要哈希或 TTL。
- 已通过 `.venv` 专项回归：`verify_knowledge_retrieval_cache.py` 覆盖首次 miss、重复 hit、结果副本隔离、`top_k` 隔离和重建索引后的 generation 失效；并复跑 K2 Hybrid、K3 回答、K2 API、全量后端回归与 Qt Debug 构建。系统 Python 缺少 Chroma/FastEmbed，不作为知识库验证环境；桌面端 `BackendManager` 已优先启动 `backend/.venv/Scripts/python.exe`。

## 2026-08-24 总指挥 C5.1：知识库全库深度总结受控委派

- AI 调度台只有在客户明确绑定一座资料库，并明确要求深度分析、全库/整库总结、逐章梳理或深度报告时，才会产生 `knowledge_agent/deep_summary`。普通资料问答仍走 C4，单一绑定不会自动推断资料对照。
- 深度 action 使用产品级 `knowledge_deep_analysis` 预算确认。它是长耗时、模型成本可观但只读的操作，因此所有权限模式都需要一次明确确认；历史页显示为“全库深度分析预算”，不暴露内部权限字符串。
- 确认后 Commander Runtime 只做“冻结 K4 scope -> 创建 `task_k4_*` -> 启动后台子任务 -> 登记关联 artifact”。父任务的完成语义是“委派已受理”，不等同于深度结论、Map/Reduce 或 Markdown 报告已经完成；客户可从关联任务的历史记录查看真实阶段、暂停、继续、取消和报告状态。
- 新增 `backend/scripts/verify_commander_knowledge_deep_route.py`，离线覆盖无绑定拒绝、全库深度 action 准入、四种权限模式都停在预算确认、子任务受理、父子 artifact 关联和“不得伪报完成”；该脚本与既有 C4 回归均通过，不消耗模型额度。

## 2026-08-24 总指挥 C5.2：父子任务真实状态镜像

- 父任务的历史复盘不再只展示 C5.1 受理时写入的静态 `queued` 回执。对于关联 `task_k4_*`，后端只查询 SQLite 中的任务状态与按 action/status 分组的步骤计数，并将真实 `Map 完成/总数`、`Reduce 完成/总数` 映射到现有 `/updates` 快照；不会读取或复制子任务摘要、章节正文、Map 小结、来源、模型输出或完整 `run_json`。
- Qt 历史页会在关联子任务仍处于排队、运行或等待状态时继续低频自动刷新，即使父 Runtime 只完成了“交接”步骤；状态条与复盘卡会明确显示“子任务进行中”和当前 Map/Reduce 进度。子任务暂停、失败、取消或完成时，父任务只给出下一步提示，控制、检查点、完整 trace 和报告导出仍保留在 K4 工作台。
- `verify_commander_knowledge_deep_route.py` 新增 C5.2 离线断言：模拟 K4 运行 checkpoint 后，父任务 `/updates` 必须显示真实 `running`、`Map 11/37`，且不触发模型调用或材料读取。`compileall`、K4 Map/Reduce 回归与 Qt Debug 构建均通过。

## 2026-08-24 总指挥 C5.3：关联深度任务工作台入口

- 当客户在总指挥父任务的历史产物栏点击关联 `task_k4_*` 时，Qt 直接打开既有“深度任务工作台”的只读关联模式，而不再只跳转一条通用历史记录。该模式隐藏新建任务输入，将空间留给冻结范围、真实阶段、部分结论、暂停/继续/结束和正式报告资格。
- 关联入口只传递已审计的稳定子任务 ID；工作台仍向 K4 API 补读真实 scope、Map/Reduce checkpoint 和状态。它不会创建新任务、重新冻结范围、读取原始章节、复制子任务正文，或让父任务取得子任务控制权。
- `compileall`、`verify_commander_knowledge_deep_route.py`、`verify_knowledge_deep_task_map.py` 与 Qt Debug 构建通过。下一次客户可见验收只需从一条 C5 父任务关联产物点击“打开”，确认运行中旋转标识、终态停止、范围显示和控制按钮与 K4 原工作台一致。

## 2026-08-24 知识库 K4.15：完整深度总结与资料对照表

- 新建的“全库深度总结”不再把 24 章作为执行或客户范围上限：所有已索引活动章节均被冻结为独立 Map checkpoint。Reduce 改为每个节点最多读取 6 个已验证小结的递归归并树；章节数量增长只增加可恢复的后台节点，不会让最终一次模型调用塞入全库内容。
- 新建的“资料对照表”必须由客户选择 2 至 12 份材料；选择的列表顺序就是报告列顺序。结果窗口和 `output/knowledge_reports` Markdown 会交付真实表格。若 Provider 只返回概述而漏掉表格行，Runtime 仅依据已完成 Map 小结补一行“资料摘要”，不捏造未出现的事实，也不会退化成普通跨文档摘要。
- K4.9 的 `goal_focused` 仅保留给历史 scope/checkpoint 读取，新任务不再按标题挑选 24 个代表章节。新界面不再提供泛化的“风险与一致性审查”；这类判断作为 Map/Reduce 的内部质量检查保留，旧审查任务仍可查看与导出。
- 离线回归新增两份实际资料的对照任务、Markdown 表格导出和 389 个 Map 单元的递归 Reduce 计划检查；未调用真实模型或联网。

## 2026-08-24 知识库 K4.14：限流自恢复与历史产物兼容

- 客户实际任务日志确认 Map/Reduce 的反复暂停来自 Kimi 返回的 `HTTP 429` 与 `max RPM: 3`，不是章节内容或结构化输出问题。K4 现在会从供应商的明确 RPM 回执学习同账户的滚动一分钟请求窗口；首次触发后，后续 Map/Reduce 请求自动排队到安全时间再发出，界面事件会说明预计等待，客户不必连续点击“继续并重试”。该保护不增加模型调用次数，也不会重跑已完成 checkpoint；HTTP 400、参数错误和输出契约失败仍不会自动重发。
- 修复历史任务“打开产物”404：报告 artifact 已正确写入 SQLite 和 `output/knowledge_reports`，问题来自旧 Qt 把冒号 ID 预编码后又被 `QUrl` 二次编码。后端现在兼容一次遗留 `%3A` 解码后再精确查找，Qt 新构造方式交由 `QUrl` 只编码一次；已有历史报告无需重新导出即可预览或打开。
- Provider 错误摘要新增 `ak-`/尖括号令牌脱敏，避免限流消息中意外回显的访问标识进入任务历史或 UI。
- `verify_knowledge_deep_task_map.py` 已覆盖 3 RPM 的虚拟安全窗口、遗留双编码 artifact 预览、Map/Reduce 恢复；`compileall` 与 `verify_backend.py` 均通过。Qt URL 改动仍需 Debug 构建验证。

## 2026-08-24 知识库 K4.13：可诊断的模型失败与一次受控重试

- 复盘客户实际任务确认：连续点击并非没有发送恢复请求；任务曾在同一 Map 节点重复触发 `model_request_failed`，但旧实现把真实请求错误误写成“未通过结构化输出”，同时丢失 `AgentRunner` 的安全错误摘要，客户无法判断是否应等待、检查配置还是缩小任务。
- Map 与 Reduce checkpoint 现保存有限 `stop_reason` 和脱敏 `failure_message`。结果补读会把失败章节或 Reduce 节点的短原因放入“需要留意”，恢复时会清除旧错误字段；原文、请求体、绝对路径、密钥和 Provider 原始错误正文均不入历史。
- 对连接失败、超时及 HTTP `408/409/425/429/5xx`，同一节点自动短退避 `0.8s` 后重试一次，并把 `knowledge_deep_model_transient_retry` 写入 SQLite 事件与实时状态。HTTP 400、参数问题、JSON/来源契约失败和第二次失败都立即停驻，不增加模型轮次或重跑已完成章节。
- `verify_knowledge_deep_task_map.py` 新增 Map 与 Reduce 的“首个 HTTP 503 失败 -> 自动重试成功 -> 事件可复盘”回归；同一脚本、`verify_backend.py`、UTF-8/编译检查通过后，客户只需重启后端并点击一次继续。当前旧任务的既有失败记录不会被篡改，下一次恢复才使用新策略。

## 2026-08-24 知识库 K4.12：Map/Reduce 输出契约收束

- Map 与 Reduce 不再把 `map_unit_id`、`source_id`、发现编号、冲突编号等 Runtime 内部字符串交给模型填写。模型只需返回章节小结/概述、发现文本、待确认项和提示；服务端根据同一次冻结 scope 单向补齐编号、已读章节范围与最终覆盖，并再次验证正式 checkpoint。
- `KnowledgeDeepMapDraft`、`KnowledgeDeepReduceDraft` 兼容 `summary/overview/conclusion`、`findings/points/key_findings` 等常见语义字段，以及字符串或旧对象列表。该兼容只提取模型已返回的业务文本、限制数量和长度；不会补造事实、扩大资料范围或让模型自行声明来源。
- 这直接修复了不同 Provider 因复制动态 ID、来源数组超长或新旧 JSON 形态轻微不同而反复停在同一 Map/Reduce 节点的问题。失败时仍保留已完成 checkpoint，显式继续只重试失败节点；不会为掩盖问题增加无界模型轮数。
- 离线回归 `verify_knowledge_deep_task_map.py`、全量 `verify_backend.py` 与 UTF-8 编译检查已通过；当前 Kimi K2.6 以两条临时章节小结完成一次真实 Reduce 契约探针，`completed`、零格式修复、未读取客户资料、未写入任务或文件。

## 2026-08-24 知识库 K4.11：Reduce 恢复状态机修复

- 修复了一个确定性恢复漏洞：当全部 Map 章节已完成、Reduce 因模型输出失败而 `blocked` 后，`resume` 会先把统一任务状态设为 `pending`。旧 Map 入口误将这个全局状态当作 Map 未完成并直接返回，外层 Runtime 因而永远不进入 Reduce，客户会看到 `24/24` 与 `0/5` 长时间停在“等待执行”。
- Map 现明确区分“Map 阶段完成”与“整体 Map/Reduce 工作流完成”。恢复时会保留全部章节 checkpoint、把任务切回 `running`，并以 Map 阶段完成回执继续进入未完成的 Reduce；不再读取章节正文或重复调用 Map 模型。
- `verify_knowledge_deep_task_map.py` 新增了完整客户路径回归：Map 完成、首个 Reduce 节点失败、`resume`、完整 Runtime 继续，最终确认 Map 不重跑而所有 Reduce 节点完成。`cancelled` 仍为终态，不会由普通继续按钮自动复活。

## 2026-08-24 知识库 K4.10：Map 输出契约兼容与操作层级修复

- 深度分析的章节 Map 不再要求模型输出 `map_unit_id`、来源数组或发现编号。模型只返回 `summary`、短 `findings` 和 `warnings`；Runtime 根据已冻结的章节单向补齐来源、编号并再次校验。这样不会扩大可引用资料范围，也避免真实 Provider 因复制动态 ID、旧对象结构或提示词细节而让整个长任务停在同一章节。
- `KnowledgeDeepMapDraft` 对旧式发现对象、单条提示和空白小结做兼容/校验；恢复任务只重试失败章节，完成章节仍从 SQLite checkpoint 读取，不重复调用模型。`verify_knowledge_deep_task_map.py` 已通过 Map/Reduce 隔离、暂停恢复和陈旧索引拒绝回归。
- 已对当前客户端实际选择的 Kimi K2.6 进行最小真实验证：JSON Map 探针和独立临时资料库的 Map checkpoint 均完成 `1/1`；测试不读取客户资料、不输出模型正文或密钥。临时目录中的 `.env` 默认 DeepSeek 配置曾返回请求失败，因此它不能作为客户端已保存 Kimi 配置的验收结论。
- 主窗口新增按钮视觉兜底：普通工作区 `QPushButton` 缺少语义时仍显示边框与状态；“新建资料库”为蓝色主按钮，项目范围图标按钮也已接入可见的次级样式。左侧导航保持局部无边框。所有窗口仍需在实际 Qt 首屏验收后才算视觉交付。

## 2026-08-24 知识库 K4.9：大型资料聚焦与可见任务状态

- K4 的 Map/Reduce 首期预算仍为最多 24 个章节，未盲目提高上限。活动索引多于 24 章时，服务端仅依据客户目标、文件名和章节标题进行确定性、轮转式聚焦；不读取正文、不调用额外模型，并优先让不同资料库文件都获得代表章节。
- `KnowledgeDeepTaskScope` 新增总资料/总章节数、`complete/goal_focused` 模式和覆盖说明。Qt 范围检查器、运行结果和导出的 Markdown 报告都会显示“聚焦 N/M 个章节”；未选章节不会被读取或写入结论。真正逐章整库审计仍属于后续分层 Map/Reduce，不以本次 MVP 冒充完成。
- 新增可复用 `TaskActivityIndicator`。知识库问答、深度分析和索引状态已按真实运行终态驱动转动/停止；后续所有后台任务页面须按 `SKILL.md` 的统一规则接入。
- 索引实体本来持久化在项目 `data/agentflow.db` 与受控资料目录中，重启后不会因为 Qt 客户端重新打开而重新解析。此前“建立索引”按钮和资料列表没有明显说明版本状态，容易让客户误以为必须重建；现改为展示 `已索引 vN`、`已切分 / 已索引`、`重新建立索引` 等明确标记。后续仍需客户在实际窗口确认显示符合使用习惯。

## 2026-08-21 知识库 K2 真实 Hybrid 校正与基线

- 已修复 FastEmbed 到 Chroma 的类型边界：FastEmbed 产出的 `numpy.float32` 现在在 Embedding Gateway 归一为 Python `float`，真实 512 维向量可写入、回读和按 generation 隔离；此前因 Chroma 拒绝嵌套 numpy 标量而出现的 `partial_failure` 不再被误认为“模型未准备”。
- `verify_knowledge_retrieval_baseline.py` 默认仍保持关键词、零模型下载；显式 `--with-local-dense` 才只读复用客户已确认的本地 BGE 缓存，SQLite/Chroma 测试索引始终创建并清理于临时目录。固定 7 资料/11 题的关键词为 `8/8` 必过、`0.850/0.850` Recall@5/MRR、无答案 `1/1`；真实 Hybrid 为 `8/8`、`1.000/0.950`、无答案 `1/1`。这是小型隔离集的质量证据，不是生产 SLA。
- 已新增 Dense L2 距离 `<= 0.95` 的拒答门槛与离线回归：向量数据库的最近邻若未达到相关度要求，不得产生伪来源；“已计算 Dense 但没有可信证据”统一返回 `no_result`。Rerank、查询改写、HyDE、OCR、多向量和 K4 深度任务仍未启用，不能写成完成。

## 2026-08-21 知识库 K4.4：可恢复后台 API 与结果补读

- 新增 `POST /api/knowledge/deep-tasks/start` 和 `GET /api/knowledge/deep-tasks/{task_id}/result`。启动接口只接受已批准的摘要、比较或审查任务，先冻结活动 generation 的无正文 scope，再返回 `202` 并通过既有任务事件流发布真实 Map/Reduce 阶段。
- 首个 Map step 保存一次无正文 scope；服务重启后结果接口能从 SQLite 恢复范围、状态和最终 `KnowledgeDeepReduceResult`，不依赖进程内请求对象，不暴露父块正文、绝对路径或凭据。
- `run_knowledge_deep_task()` 在任务开始时固定一次已解析 Model Runtime，Map/Reduce 共用同一 provider/profile；没有可用模型或资料更新时任务写为 `blocked`，不假装成功。离线 HTTP 回归继续注入假模型，不使用客户额度。
- 后台协程边界外若出现未预期异常，也会把同一任务收束为 SQLite 中可见的 `failed` 终态并追加脱敏审计事件；刷新历史不会留下永久“运行中”的深度任务，既有无正文 scope 和检查点仍可补读。

## 2026-08-21 知识库 K4.5：协作式暂停、继续与取消

- 新增深度任务专用的 `pause`、`resume`、`cancel` 控制接口，复用 SQLite 的 Runtime 控制信号而不依赖进程内标志。暂停、继续和取消均保持同一 `task_id`、冻结 scope 与既有 Map/Reduce checkpoint。
- 正在运行时只登记控制请求；服务只在模型回合开始前或结果已安全写入 SQLite 后消费控制信号。因此不会强杀 Provider 请求，也不会丢弃已经完成的章节或批次；取消后续节点会明确标为 `cancelled`。
- 离线回归覆盖启动前暂停零模型调用、运行中取消保留当前章节但不进入下一章节、同任务继续和三条 HTTP 控制接口；未调用真实模型、网络或客户资料。Qt 深度任务入口、部分完成报告和正式文件交付仍未开始。

## 2026-08-21 知识库 K4.6：部分结果与导出资格契约

- `GET /api/knowledge/deep-tasks/{task_id}/result` 现在会确定性返回 Map/Reduce 覆盖状态、已完成章节的受控小结、未完成/失败/取消章节，以及报告资格。结果补读不重新扫描资料库、不读取父块正文、不调用模型。
- 只有任务完成、所有冻结 Map 单元和预期 Reduce 节点都完成、且最终 `KnowledgeDeepReduceResult` 能通过输出契约时，`report_readiness.can_export=true`；任何部分完成只允许预览，不能伪装成正式整库报告。
- 历史任务若最终 Reduce checkpoint 损坏，结果接口会返回脱敏警告并拒绝正式导出，而不是直接以 500 中断客户阅读。离线回归已覆盖失败 Map 的部分预览、运行中取消后的覆盖范围、完整 Reduce 的导出资格与 HTTP 补读；未调用真实模型、网络、未创建文件或 Qt 页面。

## 2026-08-21 知识库 K4.8：Qt 深度任务客户入口

- 知识库页新增“深度分析”入口；只有完整 `ready` 索引可创建跨文档摘要、比较或风险/一致性审查，避免用 `partial_failure` 的资料库伪装成整库结论。
- 新增独立 `KnowledgeDeepTaskDialog` Designer 工作台：受理后以既有任务事件流展示真实阶段，850ms 低频补读无正文范围/覆盖；客户可查看冻结资料/章节/定位元数据、完整 Reduce 结论或已完成 Map 小结，并可在安全边界暂停、继续、取消。
- 正式 Markdown 报告按钮只在后端 `report_readiness.can_export=true` 时可用，且经一次明确确认；导出不重跑模型、检索或原文读取。Qt Client 仅使用稳定任务 ID 和受控 JSON，不保留正文、绝对路径或凭据。
- 本轮已通过 `backend/.venv` 的 `compileall`、`verify_knowledge_deep_task_map.py` 与 `build/codex-debug` Qt Debug 构建。尚需客户在可见窗口验收布局和实际模型任务；Commander 深度委派尚未开放。

## 2026-08-21 知识库 K4.7：确认后的 Markdown 正式报告

- 新增 `POST /api/knowledge/deep-tasks/{task_id}/report`。只有 `K4.6` 判为 `ready_for_export` 且客户提交 `confirmed=true` 的完整任务才能生成 Markdown；导出不重新检索、读取父块正文或调用模型。
- 报告固定写入 `output/knowledge_reports`，使用文件系统 `x` 模式防覆盖、UTF-8 回读验证，并以 `knowledge_reports` artifact 登记到同一任务历史。客户可通过现有历史预览和打开接口访问，列表不会暴露绝对输出路径。
- 离线回归覆盖部分任务拒绝导出、未确认拒绝、完整报告写入/回读、HTTP 导出与脱敏预览。未调用真实模型或网络，未改 Qt 页面；下一步才是深度任务的 Qt 客户入口。

## 2026-08-21 知识库 K4.3：两级 Reduce 检查点与冲突保留

- K4.3 只消费 K4.2 已完成的 Map checkpoint：每批最多 6 个章节、最多 4 批，最终再合并批次小结；不会重新读取父块正文、扫描资料库或扩展来源范围。
- `KnowledgeDeepReduceDraft` 只承载概述、发现文本、待确认项与提示；正式结果中的 `map_unit_id`、来源范围和稳定编号由服务端根据冻结批次单向写入。批次/最终节点均写入 SQLite；结构化输出失败后停驻，显式恢复仅重试失败 Reduce 节点。
- `verify_knowledge_deep_task_map.py` 已扩展为真实导入/index/SQLite/AgentRunner 的离线回归，覆盖 Map/Reduce 输入隔离、失败恢复、最终来源覆盖、冲突保留、完成后幂等读取及索引更新后的 stale 拒绝。本轮未调用真实模型、未联网、未创建报告或 Qt 页面。

## 2026-08-21 知识库 K4.2：有限 Map 检查点与恢复

- K4.2 在 K4.1 范围快照之上实现了最多 24 个章节的后台 Map 阶段：每轮模型只获取当前一个受控父章节，输出只能回指当前 `map_unit_id`；父块原文不会写入任务步骤、事件或统一历史。
- 每个章节完成后立即写入 SQLite checkpoint。一次结构化输出失败会停止后续调用并保留已完成章节；再次进入同一任务才重试失败节点，完成节点不重复调用模型。活动 generation 在读取前或模型返回后发生变化，旧任务会安全停驻，不能把旧结论写入新索引。
- `verify_knowledge_deep_task_map.py` 已以真实导入/index/SQLite/AgentRunner 和假模型验证章节隔离、无正文快照、失败后恢复、完成后幂等读取与 stale 拒绝；本轮未调用真实模型、未联网、未创建报告或 Qt 页面。下一步才是 Reduce 的分层合并和冲突保留。

## 2026-08-21 知识库 K4.1：深度任务范围快照

- 新增 `KnowledgeDeepTaskScope` 与 `knowledge_deep_task.py`。它只按当前活动 generation 读取章节 ID、版本、标题路径、来源锚点和字符范围，建立稳定 Map 单元；不读取父块正文到回执、不调用模型、不写报告、不扫描工作区，也不开放总指挥路由。
- 资料库索引切换后，`verify_knowledge_deep_task_scope()` 会拒绝旧范围恢复，防止已完成 Map 节点和新版资料被静默混合；活动文档若没有可处理父块也会明确停止，而不是生成漏章节的“整库”任务。
- `verify_knowledge_deep_task_scope.py` 已覆盖活动范围、无正文快照和 generation 更新后的 stale 拒绝。K4.2 的受预算限制 Map checkpoint 已完成；Reduce、部分完成报告、暂停/取消承接、Qt 深度任务页和 LangGraph/Harness 评估均未开始。

## 2026-08-21 本地知识库 K0.1-K0.3 与 K1.1-K1.6 完成

- 已完整复审 `docs/langchainANDlanggraphMD` 第四章，并结合现有 Retrieval、文档来源、SQLite、权限、任务历史、Commander 和多供应商边界形成正式方案。
- 客户侧保留“知识库”应用；内部拆为 `Knowledge Agent + Knowledge Retrieval Service`。前者拥有资料库生命周期、可信问答和深度任务，后者向所有已批准 Agent 提供关键词/向量/重排/来源能力。
- 已批准路线为 K0 技术试验 -> K1 本地资料库 -> K2 Hybrid Retrieval -> K3 可信问答 -> K4 Map-Reduce 深度任务 -> K5 长窗口与缓存优化。只有 K3 通过引用、更新/删除失效和客户 UI 验收后，才允许 `runtime_ready=true`。
- 已建立 4 份脱敏固定资料和 7 类问题夹具；`verify_knowledge_k0.py` 在正式 Python 3.13 环境验证 SQLite FTS5、中文影子字段和关键词召回，关键词必过用例为 Top 5 的 4/4，未调用模型或网络。
- 已实测 FastEmbed `bge-small-zh-v1.5`：512 维、约 90.81MB 本地缓存、冷启动约 31.6 秒、热启动约 119ms，固定中文语义改写题为 Top 1 的 1/1。Windows 未启用开发者模式会使模型缓存退化为复制文件，K1 需显示磁盘与下载状态。
- 同机对照后 K1 默认向量引擎改为 Chroma PersistentClient：10k x 512 索引约 5.9 秒、过滤查询 P95 约 67ms、索引目录约 29.30MB；100k 索引约 148.8 秒、查询 P95 约 453ms。Qdrant Local 通过功能验证但 10k 持久化写入约 117 秒，故不作为 K1 默认。两个候选均只在隔离环境安装，正式依赖未改变。
- Chroma 基准已补齐 Windows 清理纪律：`PersistentClient` 必须显式 `close()` 后再释放 collection 和删除临时索引目录；复跑确认不会新增残留目录。K1 的版本切换和删除任务必须复用这一顺序，不能把磁盘残留当成无害测试现象。
- Rerank 默认关闭：可商用本地候选约 1.04GB，多语候选存在非商业许可证限制。K0.3 的 Pydantic 契约与 K1.1-K1.2 的 SQLite migration/Profile/资料库/脱敏审计、受控副本及逻辑版本事实仓储已经完成；索引版本切换、删除清理和扩展质量题集仍在后续切片，当前不宣称知识库已可使用。
- MVP 默认本地解析与索引、不联网、不修改原文；OCR、网页爬虫、云连接器、多人 ACL、知识图谱、完整聊天历史向量化和全代码仓库向量化不在批准范围。
- K1.1 已新增 `schema_migrations` checksum 保护与三张基础事实表；K1.2 新增逻辑文档/文档版本表及知识库私有副本目录；K1.3 复用既有文本/PDF/DOCX 解析器，新增父块/子块、标题路径、来源定位与子块邻接事实；K1.4 新增 generation、文档快照、可恢复后台索引任务与按 generation 隔离的 SQLite FTS5/CJK 影子字段。隔离回归覆盖首启、重复启动、名称冲突原子性、脱敏审计、checksum 篡改拒绝、受控副本新建、重复去重、内容更新、失败清理、来源锚点、父子关系、空材料失败、FTS 回读、部分失败、旧 generation 保留和服务重启收束。未调用模型、未修改 Qt 或用户资料，也未导入任何客户文件。
- 已正式安装 Chroma/FastEmbed 依赖并完成零模型、零客户资料的 Chroma 本地持久化探针；下一步是实现模型下载诊断、Chroma Adapter 与索引双后端验证。
- K1.5/K1.6 已新增 generation 隔离的 `ChromaGenerationIndex`、无副作用能力诊断、显式本机模型下载守卫和真实 generation 接入：未经客户确认只建立 FTS 关键词 generation；确认后才初始化 FastEmbed 并写入 Chroma，失败时关键词 generation 仍可用并记录部分失败。API、后台 job 轮询、取消、删除和启动恢复均已完成；Qt 知识库页已从占位状态改为“资料库列表 + 材料 + 索引状态”工作台。所有 K1 验证与 Qt Debug 构建已通过，下一步为 K2 Hybrid Retrieval 的离线评估和只读检索链。

## 2026-08-21 本地知识库 K2 第一段：受控混合检索内核

- 已新增 `KnowledgeRetrievalRequest / Result / Evidence / Diagnostics` 契约与 `knowledge_retrieval.py`。检索只接受资料库稳定 ID、普通问题和有限 `top_k`，不会接收路径、向量、模型对象或任意 FTS 表达式。
- 每次检索严格绑定 `knowledge_bases.active_index_generation` 的 `ready` generation；更新过程仍可读旧活动 generation，删除中、无索引或候选 generation 一律拒绝，不会把旧 FTS 或跨库内容混入结果。
- 关键词路径使用 SQLite FTS5 与中文二元词影子字段；语义路径只在 generation 已是 `vector_index_mode=ready` 且本机模型可离线加载时调用 Chroma。两路候选以固定 RRF 合并，回读时按父块去重并保留稳定文件/版本/来源锚点。
- 本机 Embedding 缓存、Chroma 目录或依赖在检索时不可用，会明确返回 `keyword_fallback` 与客户可解释的降级提示；不会自动下载模型、联网或把底层异常暴露给客户。语义尚未准备时同样明确标注为关键词检索。
- FTS5 作为派生索引异常时，会退到当前活动 generation 内最多 500 个受控子块的确定性搜索，并标记该降级；不能把“索引不可用”伪装成“资料没有答案”。
- 新增 `backend/scripts/verify_knowledge_retrieval.py`、`verify_knowledge_retrieval_baseline.py` 与 `verify_knowledge_retrieval_api.py`。夹具以临时目录、假向量和真实 Chroma 验证当前 generation 隔离、编号/中文关键词、Hybrid RRF、父块去重、FTS 故障降级与语义故障降级；固定 7 题资料集当前关键词基线为必过召回 `4/4`、平均 Recall@5 `0.750`、MRR `0.750`、无答案 `1/1`、中位检索约 `8ms`，未联网或下载模型。HTTP 回归验证已索引资料返回只读证据、未索引资料返回 409、错误资料库返回 404。本轮 K1/K2 全部 12 个知识库离线脚本已通过。Rerank 继续关闭：K0 已确认候选体积/许可证不适合作为桌面 MVP 默认依赖，只有未来评估集证明 RRF 不足时才按独立 Adapter 重新立项。K2 当前仍未接入问答模型、任务历史、缓存或 Qt“问知识库”页面。

## 2026-08-21 本地知识库 K3 第一段：Evidence Gate 与回答契约

- 已新增 K3 最小来源卡、Evidence Gate 和可信回答契约。Gate 只消费 K2 返回的受控证据，不向模型、客户接口或后续回答层泄露父块全文、绝对路径、数据库对象或索引实现细节。
- Gate 会重新核验 child/parent chunk、文档版本、来源范围和当前活动 `generation` 的映射。资料更新切换后，即使旧内容仍保留在历史事实表中，也会清空本轮可回答来源并要求重新检索。
- 普通问题至少需要一份独立资料；包含对比/差异/冲突等标记的问题至少需要两份独立资料。零命中明确返回“资料不足”，比较覆盖不足只能标为 `partial`，而不是由模型补写结论。
- `verify_knowledge_evidence_gate.py` 已离线覆盖充分、部分、无证据和 generation 更新后的旧证据失效；同时复跑 K2 retrieval、固定基线、HTTP API 与后端总回归均通过。未调用真实模型、网络或客户资料。
- K3 已具备 Gate 与回答契约，但当时尚未开放客户问答入口；当前第二段已补齐受约束模型回答和引用 Verifier。流式事件、任务历史、Qt 阅读区或 Commander 仍未接入；`knowledge_agent.runtime_ready` 继续为 `false`。

## 2026-08-21 本地知识库 K3 第二段：受约束回答与引用 Verifier

- 已新增 `KnowledgeAnswerRequest / Response` 与 `KnowledgeTrustedAnswer` 契约。每个 `claim` 和顶层 `source_ids` 均要求去重且完全一致；服务端继续校验它们只能来自本轮实际发送给模型的来源集合。
- 新增 `POST /api/knowledge/answer` 与 `knowledge_answer.py`：先运行 K2 检索和 Evidence Gate；零证据不调用模型；模型上下文最多四条已核验父块；Runner 最多一次 JSON 格式修复且不开放 Tool；完成后再次执行 Gate，generation 切换或来源失效时丢弃回答。
- 离线假模型回归覆盖正常引用、一次格式修复、比较资料不足的 `partial` 回答、越权 `source_id` 拒绝、零证据不调用模型、回答期间 generation 更新后的旧答案拒绝，以及 HTTP 契约。未读取真实模型配置、未联网、未消耗模型额度。
- K3 的多资料夹具同时暴露并修复了 K1 增量 generation 的状态计数：上一代已 `ready` 的版本在新 generation 中应视为可复用的解析成功，不能误记为失败；向量 generation 也必须继续嵌入这些 ready 分块。`verify_knowledge_keyword_jobs.py` 与 `verify_knowledge_vector_generation.py` 已各自固定新增材料后的成功重建断言。
- K3 仍未接入流式阶段事件、任务历史、Qt 来源阅读区或 Commander 只读准入；`knowledge_agent.runtime_ready` 继续为 `false`。回答模型是否有能力完成客户问题仍需在上述观察面完成后按用户授权进行单次真实验收，不能把离线假模型通过等同于客户交付。

## 2026-08-21 本地知识库 K3 第三段：任务历史与阅读工作台

- 已将可信问答从同步 HTTP 调用扩展为 `POST /api/knowledge/answer/start -> 202 Accepted -> WebSocket 真实阶段事件 -> GET 终态补读`。受理、检索、Evidence Gate、模型、引用核验、阻塞、失败和完成都由同一任务 ID 进入既有工作流/任务日志；工具审计仅记录 `knowledge.retrieve` 与 `knowledge.evidence_gate` 的脱敏摘要，不把模型调用伪装成 Tool。
- 已保留原同步 `/api/knowledge/answer` 作为兼容入口；新的后台任务回读结果只包含受控回答、来源卡和检索诊断，不写入或回显父块全文、绝对路径、向量或数据库对象。
- Qt 的知识库主页新增“向资料提问”入口。它只有在选择已建立活动索引的资料库时才可用；独立窗口把问题、真实阶段、可伸缩结论阅读区和按需来源 Inspector 分开，避免把长回答挤进资料库的材料/索引管理页面。客户可从窗口直接跳到同一任务历史。
- 离线回归已通过：`verify_knowledge_k0.py`、资料库/分块/索引/删除、K2 检索与基线、K3 Evidence Gate、K3 问答任务生命周期及 `verify_backend.py`；`build/codex-debug` 的 Qt UIC/MOC/MSVC 链接也通过。所有本轮验证均使用临时资料和假模型，未读取 API Key、未联网、未消耗模型额度。
- `backend/scripts/verify_live_knowledge_answer.py --live` 已对当前已配置回答模型完成一次受控真实验收：合成双资料问题在 1 轮内完成，返回 2 条独立来源并走关键词检索路径；脚本不输出密钥、模型正文、来源片段或绝对路径。客户实际 Qt 体验验收仍待完成，之后再单独讨论 Commander 对知识库的只读 action 准入；此之前 `knowledge_agent.runtime_ready=false`。

## 2026-08-20 总指挥 C0-C3 当前闭环

- **C0 已落地**：新增统一 Agent Action 准入目录。每个动作都明确执行模式、材料类型、`runtime_ready` 前置、风险、确认、验证范围与恢复提示；`GET /api/agents/action-admissions` 供 Qt、审计与后续 Router 共用。旧 Code/Report 占位能力不再进入客户可执行路线。
- **材料边界已落地**：AI 调度台只会发送用户刚刚选择的工作区相对文档引用；后端拒绝绝对路径、目录跳转和未绑定材料。总指挥不再扫描 workspace 猜测“这个文件”指向什么，缺材料时会提出澄清问题。
- **C1 首条真实路线已接通**：明确绑定的 TXT/Markdown/PDF/DOCX 文档可进入已验收的文档助手步骤和现有父子任务链。父任务终态会汇总已委派专业 Agent 的状态、短结论与关联任务 ID，完整来源和 Tool trace 保留在子任务。数据任务会生成结构化“转入数据工作台”步骤，并把目标预填到数据工作台；在 D5.4 之前，该步骤在 Runtime 中以 `blocked/user_action_required` 留下审计，不读取 CSV、不创建伪造的 data_agent 子任务。
- **调度台承接已更新**：计划步骤会显示“可执行 / 仅规划 / 需转入专业工作台”的准入状态、验证范围和恢复建议；真实执行前的确认框会复述本次计划、步骤、读取范围、写入范围和外部服务。数据交接时主按钮变为“前往数据工作台”。文档导入后的默认任务也已改为提取目标、要求和待确认事项，不再遗留已取消代码工坊的 Python/README 表述。
- **C2 初版已落地**：SQLite 新增用户确认的长期记忆记录。仅允许 `user_preference`、`project_constraint`、`experience` 三类短事实；`/api/memories` 支持查看、新建、编辑、停用、删除和按范围明确清空。设置页默认关闭“长期记忆”，开启后 Commander 才按标题、标签、摘要的轻量关键词检索最多三条相关记录，并把实际使用的短摘要写入 `workflow_plan.memory_context_summary` 供审计。
- **C3 项目范围与显式建议已落地**：调度台输入框旁提供低频“项目范围”入口，范围只允许 `global` 或 `project:<稳定标识>`，不是文件路径、目录权限或完整项目管理系统。范围随计划保存，仅用于隔离长期记忆的检索与保存。已完成 Runtime 的总指挥任务在历史页提供“记住约束”：服务端只从“以后、始终、默认、固定”等明确长期表达生成至多一条确定性候选；客户可在独立确认窗口修改类型、范围、标题、摘要与标签后保存。普通一次性任务不会被强行写成记忆，建议查询本身也绝不落库。
- **隐私与控制边界**：模型不会自动写入记忆；创建必须带用户确认。服务端拒绝 API Key、令牌、私钥与绝对路径，且不保存原始文档、完整对话、原始表格行或模型臆测。Qt 的“系统设置 -> 管理记忆”使用独立可伸缩窗口承载列表与编辑，避免把低频治理操作塞进总指挥工作台；项目范围目前只作为轻量记忆命名空间，后续若引入真正项目实体、成员或工作区权限，必须单独立项。
- **C3 计划版本已落地**：`WorkflowPlan` 既有的 `plan_id / plan_version / parent_plan_id / change_summary` 已连接 SQLite 不可变快照表。`GET /api/tasks/{task_id}/plan-versions` 可列出版本，`GET .../plan-versions/{version}` 可只读回看，`POST .../plan-revisions` 只接受客户确认后的新任务目标与变更说明；服务端重新走 Commander 的材料绑定、动作准入、权限与 dry-run 校验，不接受客户端提交步骤、权限或路径 JSON。已派生 Runtime 的来源任务拒绝再修改，避免新计划与旧执行链混淆。Qt 调度台只新增一个“查看计划”入口，打开可伸缩 Inspector 后才显示版本表、各版详情和修订输入；真实执行后该窗口自动只读。
- **C3 权限恢复已校正**：同一 `runtime_task_id` 在权限批准或阻塞条件消除后继续时，会保留原有日志、已完成步骤、工具审计和产物，并从第一个未完成步骤恢复；不再用一次“从头再跑”覆盖时间线。回归明确检查 `task_resumed` 事件存在，且恢复前已完成步骤只出现一次 `step_started`。
- **C3 Runtime 治理已落地**：`POST /api/tasks/{task_id}/start` 会先持久化 Runtime 任务、步骤、权限和 `task_queued` 事件，再由同进程后台 worker 执行；Qt 不再因一次长任务请求而卡住。每个 Tool 边界都会将步骤、工具审计、产物、权限和 append-only 事件写入 SQLite，WebSocket 同时推送已发生的真实阶段事件。暂停与取消是协作式控制：运行中的调用在当前 Tool 返回后的安全边界停止，等待审批或已暂停任务可立即落库；继续复用同一 runtime task，不重复执行已完成步骤。Qt 历史页已经提供暂停、取消和继续入口。
- **C3 重启恢复语义已落地**：后端启动时会扫描上一进程遗留的 `pending/running/waiting_permission` Runtime，将其安全收束为 `blocked`，保留已完成步骤、已有产物与权限审计，并把正在执行的步骤/Tool 标注为“服务重启中断”。不会在不知道上次调用副作用的情况下自动重跑；历史页可解释地提示客户复核后 `retry` 生成新的干净执行记录。
- **本轮回归已通过**：`python -m compileall -q app scripts`、`python scripts/verify_commander_memory.py`、`python scripts/verify_commander_memory_proposals.py`、`python scripts/verify_commander_c0.py`、`python scripts/verify_commander_plan_revisions.py`、`python scripts/verify_commander_runtime_jobs.py`、`python scripts/verify_commander_runtime_recovery.py`、`python scripts/verify_backend.py` 与 Qt Creator MSVC Debug `AgentFlow` 构建均通过。离线脚本强制 mock，不消耗模型额度。
- **仍未完成**：当前后台 worker 只在当前后端进程中运行；服务异常退出后会安全停驻遗留任务，但不会自动续跑，自动续办前必须先完成跨调用幂等和副作用证据设计。暂停/取消不会强杀正在执行的模型或外部 HTTP 调用，模型 token 也尚未逐字推送。真正的项目实体/权限、跨项目记忆治理，以及数据工作台的写入型委派仍在后续范围。H2 Harness 仍未进入客户任务路径。

## 2026-08-24 数据工作台 D5.4 Commander 单数据集只读准入

- **历史基线**：`data_agent.analyze_dataset` 先通过工程准入；后续 R5.4B 已追加 `plan_field_transform` 与 `export_field_transform` 两个受控动作。当前总指挥必须显式绑定一份已经导入并完成画像的 CSV/XLSX；没有数据或绑定多份数据时澄清，不会伪造子任务。
- **受控子任务**：Runtime 创建 `task_data_preview_*` 子任务，复用 D2 的确定性数据计算和 L1/L2 结论层。父任务只保留子任务 ID、短结论、源 SHA-256、图表/表格数量和状态；原始行、预览行、绝对路径和完整目标不进入父任务、模型或长期记忆。
- **不扩大写入边界**：此动作只读、不联网、不生成 PNG/CSV/XLSX、不创建 `output/` 文件，也不修改源文件。现有图表看板、分析工作簿和字段副本仍由客户在数据工作台内预览并直接确认，不能借由总指挥绕过确认。
- **Qt 承接**：数据工作台在当前已完成画像的数据集旁提供“交给总指挥”入口；点击后只带入该数据集和建议目标，调度台仍要求客户确认计划后才执行。任务历史按现有父子 artifact 打开子任务，便于回看只读分析范围与结果。
- **离线验证**：`verify_commander_data_delegate.py` 已覆盖无绑定/多绑定拒绝、准确 action/Node Contract、真实父子任务、源哈希、原始行不泄露、只读产物边界和零 `output/` 写入；同时复跑数据预览、知识库 C5.3 与全量后端回归，并完成 Qt Creator MSVC Debug 构建。全部使用临时目录和 mock，不读取 API Key、不联网、不消耗模型额度；Qt 客户交互复核仍待进行。

## 2026-08-31 总指挥 R5.4B：自然语言字段加工

- 已新增字段加工计划与交付动作：客户在调度台绑定一份 CSV/XLSX 后，说出“根据金额新增排名、累计和月份字段”等目标，Commander 先从当前画像生成 `DataTransformIntent v1`，再由 Runtime 执行本地白名单操作。
- 预览与写入严格分离：预览只校验源哈希、字段存在、类型兼容和结果列名，不改源文件；客户确认后才创建 `output/data_transformations/` 中的新副本。CSV 仍为 CSV，XLSX 为无样式单表，多个派生字段统一追加到原字段之后。
- 交付前重新读取副本，核对新字段、行数、格式与源哈希，再登记 artifact 并把文件名和新增字段摘要写回同一会话。自然语言不接受公式、SQL、脚本或任意表达式，最多一次加工 12 个字段。
- 已通过 `verify_commander_data_transformation_delivery.py`、字段加工与数据交付回归、Python 编译和 `git diff --check`；本轮未调用真实模型或网络。下一步为 R5.4C 多数据集交付设计，不提前猜测关联键。

## 2026-08-19 总指挥路线复审

- 用户确认：文档助手与数据工作台都应纳入总指挥的能力版图。总指挥负责理解目标、显式绑定材料、生成计划、管理确认与父子任务、汇总结果；专业 Agent 继续拥有各自的专业 Tool、验证与交付责任。
- 数据工作台当前仍未完成 D5.4 Commander action 准入，故总指挥可以识别数据任务、生成计划并带着预填目标引导用户进入数据工作台，但不能声称已经自动委派或创建伪子任务。文档助手已验收 action 作为第一条真实委派链。
- 总指挥将采用高质量优先的模型策略，但模型选择保留在多供应商 `ModelGateway`：依据结构化输出、长上下文、工具调用、稳定性、成本与用户偏好选择，不能硬编码某一个供应商或模型为永久最优。
- 长期记忆列为总指挥 MVP 的平台能力，分为会话状态、用户偏好、项目约束和可复用经验。AgentFlow SQLite 是唯一产品事实源；客户可查看、编辑、删除、关闭和按项目清理。API Key、绝对敏感路径、原始表格行、完整私密材料与未经授权的推测不进入长期记忆。
- Node.js DeepSeek Harness 维持 H0/H1 的可选 Adapter 地位。总指挥 C1 默认走 Native Runtime 与标准任务事件，确保多模型和正常流式交互；H2 的首个候选仅是可开关、只读、非绑定的“计划审阅/长任务续办”试点，输出仍须经过 AgentFlow Validator 与用户确认。
- 下一实现顺序：C0 Agent action 准入 -> C1 调度台计划/确认/父子任务闭环 -> C2 长期记忆初版 -> C3 计划修改、暂停恢复、流式阶段与交付汇总 -> D5.4 数据工作台 action 准入 -> 按需评估 Harness H2。当前没有把 Harness、数据工作台或旧 Code/Report 占位能力伪装成已可自动执行。

## 2026-08-18 数据工作台第四轮客户反馈修正

- 字段加工已从“附带说明页的 Excel 导出”收束为真正的数据副本：CSV 仍输出 UTF-8 BOM CSV；XLSX 只输出无样式的“数据副本”单表。标准记录型表保持原有行列方向，选中的多个派生字段统一追加到原字段之后；操作、来源与验证摘要继续写进任务历史，不塞进客户表。真实的转置键值表不做静默转换，后续须按“向下追加派生行”的独立规格实现。
- 字段向导移除了无价值的“智能推荐”。创建副本改为严格的选项与字段驱动：最多可一次选择 12 个派生字段，统一在同一份副本中追加；不接受自然语言作为公式或模型执行指令。D2 的一句目标只用于数据分析问题与结论。
- 新增“数值保留位数”和“环比百分比”两项本地加工；界面中的数值范围、样例和分析指标默认显示到两位小数，但不会悄悄降低原始数据或计算精度。
- D2 结果详情现在总会显示“本次结论”。模型只可接收已验证指标、有限聚合表和图表合同，必须返回引用已有对象 ID 的严格 JSON；超时、HTTP/格式错误或越界引用都会自动切换到本地确定性结论，客户不会面对空白结果。
- 图表看板改用系统标题栏的最大化/还原按钮，图片会随当前窗口可用空间等比自适应并保持完整显示；不再保留不起作用的倍率下拉框。使用说明仍为独立的三步帮助窗口，主工作台保持聚焦当前文件和一句目标。
- 本轮已通过 `compileall`、`verify_data_analysis.py`、`verify_data_transformations.py`、Qt Designer XML/UTF-8 检查和 Qt Creator MSVC/JOM Debug 构建。**工程验证不替代客户用真实 CSV/XLSX 对字段副本、结论、图表最大化和帮助阅读的可视验收。**

## 2026-08-18 数据工作台第五轮客户反馈修正

- D2 结论不再把“生成了几份聚合表、几张图表”当成客户答案。后端在本地聚合完成后会先提炼时间首尾、峰值/低谷、类别排名与差异、连续数值变化、汇总和质量影响等有限事实；每条事实仍可回到本次表 ID。
- 可用模型只负责围绕事实底稿组织自然语言解读，不能获得原始行、外部事实或任意计算能力。存在数值事实时，模型结论或发现必须包含数值锚点；趋势与横向对比同时存在时优先各覆盖一项。未通过 JSON、引用或数据锚点检查时直接使用同一事实底稿的本地结论，不再留给客户泛泛的流程提示。
- Qt 数据工作台会在预览完成后直接显示“本次结论”卡，说明结论来源；“查看当前结果”继续承载完整发现、下一步下钻建议、聚合表和图表合同，避免把长内容塞进主工作台。
- `verify_data_analysis.py` 新增本地趋势/横向差异断言和假 Runtime 契约回归；另以 5 行合成数据完成一次真实 Provider 冒烟，返回 `mode=model`、非空标题、数值锚点与 3 条下钻建议。验证不记录 API Key、原始模型正文或客户数据。

## 2026-08-17 数据工作台第二轮客户反馈修正

- 已移除数据页默认的大型“本地数据准备”Hero；默认工作台只保留数据选择、可见的 `? 使用说明`、可展开的“字段详情”和底部的一句目标行动区。字段原始预览与建议卡不再挤压主任务区域；模型建议超时不会再显示“暂无法整理建议”。
- 对设备/实验类 CSV，新增“连续数值横轴 -> 测量指标”的本地曲线选择规则。像“焦点位置 / 有效清晰度”会优先生成真正相关的折线图；未提出比较需求时不再额外生成无意义类别柱图。该选择不向模型发送原始行，图表数值继续由确定性 Tool 计算。
- 字段加工现在支持一个最多 12 项的队列：多项新增列在内存中一起预览，确认后写入同一份同类型数据副本并回读全部结果列。图表、Excel 与字段副本的结论仍在应用内预览/历史阅读；文件交付成功后由后端受控 artifact 自动交给系统默认程序打开，新任务历史里的 runtime artifact 也不再因绝对路径隐藏而灰掉。
- 已运行 `verify_data_analysis.py`、`verify_data_transformations.py`、`verify_data_delivery.py`，并以 Qt Creator Debug 目标构建通过。**这只代表工程回归通过，仍需客户用真实 CSV 复验页面密度、连续曲线和多字段副本。**

## 2026-08-17 数据工作台产品规格 D5.0

- 用户已确认先完成数据工作台三条客户路径的正式规格，再逐项开发：数据发现与可回答问题、图表看板与 PNG 交付、安全字段加工与新副本。完整边界见 `docs/DATA_WORKSPACE_PRODUCT_SPEC.md`；该文明确模型只做规划/解释、确定性 Tool 只做计算/渲染、默认不向模型发送原始表格、源文件永不修改。
- D5.0 仅完成产品、隐私、界面、产物、失败恢复和验收规格，**不代表 D5 功能已实现**。D5.1、D5.2、D5.3 必须依次验收；D4.3B Commander 路由准入后置到三条直接工作台路径稳定后评估。

## 2026-08-17 数据工作台 D5.1：可回答问题与下一步建议

- 已新增 `POST /api/agents/data_agent/recommendations` 与稳定 Pydantic 合同。后端根据 D1 已完成的 L1 结构画像生成最多四张趋势、对比、构成、分布、数值概览或质量检查建议；每张卡都列出问题、字段、理由、预计交付和有限图表候选，字段或类型不满足时不会伪造卡片。
- Qt 默认工作台不再自动展示固定四格“下一步建议”：候选接口保留给后续 Inspector/命令入口，主页面改为可见的使用说明、按需字段详情和底部自然语言目标区。用户无需等待或刷新建议，切换数据文件时也不会把 A 文件的迟到候选显示给 B 文件。
- 当前模型增强已真实接入但严格受限：有可用 Provider 时，模型只接收字段名、类型、行列规模、缺失/唯一值计数和本地候选 ID，用于排序与引导语；不能新增列、数值、图表或执行动作。无模型、HTTP 错误、输出无效或候选越界时返回完整 `local_profile/local_fallback`，不阻塞 D2。临时四行数据的真实 Provider 探针返回 `model_assisted`、3 张卡片、0 条警告，未记录原始行或模型正文。
- 已通过 `.venv` 的 `compileall`、`verify_data_recommendations.py`、`verify_data_workspace.py`、`verify_data_analysis.py`、`verify_backend.py`、UTF-8 检查，以及 Qt Creator MSVC/JOM Debug 构建。D5.1 不创建 artifact、不联网补数、不写入源文件；其后的 D5.2 已在下一节完成。

## 2026-08-17 数据工作台 D5.2：图表看板与 PNG 交付

- 已新增确认后的 PNG 交付任务：`POST /api/agents/data_agent/charts/export/start` 立即受理，后台严格复用 D2 已验证的聚合表和 `DataChartContract`，不调用模型、不联网、不读取模型可见原始行。当前最多生成 4 张 `bar/line/pie/doughnut` 图，固定写入 `output/data_charts/<task_id>/`，拒绝覆盖与任意输出路径。
- 每张 PNG 先用 Matplotlib 的非交互后端绘制，再用 Pillow 回读格式、尺寸和最小字节大小；只有通过验证的图片才登记 Runtime artifact。任务状态、阶段事件、Tool 审计、取消和历史恢复复用现有 Native Runtime。取消会清理尚未登记的任务目录，失败不保留半成品，历史任务也禁止一键重放旧数据版本。
- Qt 在“数据分析预览”中新增“确认生成 PNG 看板”：客户明确确认后才写文件，主工作台显示真实受理/绘制/验证状态；完成后打开独立图表看板，左侧切换图表、右侧异步读取受控 PNG，完整审计可跳转历史页。绝对路径在 artifact 列表中继续脱敏。
- 已通过 `verify_data_charts.py`：异步终态、至少两张真实 PNG、图像字节/尺寸验证、受控图像接口、artifact 路径脱敏、协作式取消和源文件哈希不变。并复跑 `verify_data_recommendations.py`、`verify_data_workspace.py`、`verify_data_analysis.py`、`verify_backend.py`、`compileall`、`pip check` 与 Qt Creator MSVC/JOM Debug 构建。FastAPI/Starlette 仅出现既有 TestClient 弃用提示，不影响本轮结果。

## 2026-08-17 数据工作台 D5.3：安全字段加工与新副本

- 已完成十类确定性字段加工：四则计算、日期拆分、数值保留位数、排名、占比、数值分段、累计、环比、环比百分比和文本首尾空格清理。Qt 入口复用“生成分析预览 -> 查看预览 -> 字段加工”，在独立向导中选择有限字段与操作；客户端不发送文件路径、公式、脚本、原始行或自然语言加工指令。
- 字段变更先在内存中计算，再显示新字段、完整表范围、影响行数、空结果、最多 12 条样例及除零/日期/分段风险。只有客户点击确认后，后台任务才以 `202 -> 事件流 -> 终态补读` 写到 `output/data_transformations/`；当前实现按源类型创建一份干净副本：CSV 保持 CSV，XLSX 为无样式单“数据副本”工作表。回读类型、新字段和行数成功后才登记 artifact。
- Tool 审计名为 `data.transform_fields`。加工不调用模型、不联网、不执行 Excel 公式、宏、Python 或 SQL，源 CSV/XLSX 永不改写。取消会清理尚未登记的副本，历史隐藏绝对输出路径且拒绝对旧数据版本一键重试。
- 已通过 `verify_data_transformations.py`：十类操作、除零局部空值/警告、未知字段与无操作拒绝、异步交付、真实文件回读、artifact 脱敏、协作式取消和源文件哈希不变；并复跑 `verify_data_workspace.py`、`verify_data_analysis.py`、`verify_data_delivery.py`、`verify_data_recommendations.py`、`verify_data_charts.py`、`verify_backend.py`、`pip check` 与 Qt Creator MSVC/JOM Debug 构建。当前尚待客户本机 Qt 交互验收，数据 Agent 继续保持 `runtime_ready=false`。

## 2026-08-17 数据工作台 CSV 兼容性修正

- 已复现并修复办公软件常见的 UTF-16 CSV 画像失败：编码识别现在优先检查 BOM 和稳定的双字节 NUL 分布，避免把 UTF-16 误当 UTF-8；随后按 UTF-8/GB18030 回退。分隔符覆盖逗号、制表符、分号和竖线。
- 已修复 UTF-8/GB18030 探测窗口末尾切开多字节字符时的误判：只在 64KB 采样结尾回退最多 3 个字节重新嗅探，完整文件仍严格解码；样本内部非法字节不会被静默忽略。真实导入的 `dev.csv` 属于该情况。
- `verify_data_workspace.py` 新增 UTF-16 BOM、竖线分隔与 UTF-8 中文字符跨 64KB 边界的端到端导入/画像回归；画像成功后现有 Qt 状态机会重新启用“生成分析预览”等依赖画像的操作。若画像本身仍失败，导入、刷新和文件切换保持可用，并在页面中显示可行动原因。
- 2026-08-17 运行环境修正：桌面端实际优先启动 `backend/.venv/Scripts/python.exe`，此前该虚拟环境遗漏了已在 `requirements.txt` 声明的 `pandas` 与 `openpyxl`，导致任何 CSV/XLSX 都在画像前报依赖缺失。现已在该运行环境安装并用专项回归验证。`/health` 同时返回数据工作台就绪状态；若发行或本机环境再次漏装依赖，Qt 会在启动状态中提前说明，而不把客户文件误判为坏数据。

## 2026-08-17 数据工作台 D4.3A：协作式取消与安全恢复

- 数据工作簿任务现复用统一 `POST /api/tasks/{task_id}/cancel`，但不会粗暴终止正在执行的 pandas/openpyxl 线程。取消请求先原子写入 SQLite 的 `cancelled` 终态和 `task_cancelled` 审计；后台线程返回到安全提交点后会再次核对取消标记，删除尚未登记的 `.xlsx`，且绝不写入 artifact。
- Qt 将 `cancelled` 与 `failed` 分开呈现：从任务历史取消当前导出后，数据工作台立即恢复材料、目标和预览操作，明确说明“可重新确认导出”；当前任务的历史入口仍保留，便于查看取消轨迹。历史页对 `task_data_*` 隐藏一键重试，并解释数据任务必须回工作台复核当前预览后再创建新交付，避免因不保存客户目标全文而错误重放旧任务。
- 离线回归真实暂停 Excel 渲染线程后发起取消，并检查 `cancelled` 状态、`skipped` Tool 审计、零 artifact、`task_cancelled` 事件、后台返回后无新增 `.xlsx`，以及重试保护响应。`compileall`、`verify_data_delivery.py` 与 Qt Creator Debug 构建已通过。
- D4.3B 仍待 Commander 路由准入评估：只有明确输入映射、隐私边界、恢复语义和客户验收都通过，数据 Agent 才可能被标记为 `runtime_ready`；当前不会被 Commander 对外调度。

## 2026-08-17 数据工作台 D4.2：Qt 异步受理与受控交付入口

- 数据工作台已从旧的同步 Excel 导出切换到 D4.1 的 `202 Accepted -> WebSocket 阶段事件 -> 终态结果` 协议。小文件在实时通道握手前完成时，Qt 会以 450ms 轮询补读终态；实时流中断时同样改为结果查询，不把链路波动误报为导出失败。
- 导出中的状态只显示后端实际发布的受理、版本复核、写入与回读阶段，不显示虚假百分比。确认导出后会出现紧凑的任务历史图标；用户可跳到同一任务的审计与受控 artifact 入口，但主页面不会保存或显示输出绝对路径。
- 本轮新增的 Qt UI 结构在 `mainwindow.ui` 中维护，准备态不占额外空间；开始一轮新预览会隐藏上一轮的任务跳转，避免交付记录错绑到新的分析意图。
- 验证已通过：Qt Designer XML/UTF-8、Qt Creator Debug 构建、`verify_data_delivery.py` 和全量 `verify_backend.py`。另以临时目录下载的公开天气趋势 CSV（1,461 行 × 6 列）与州级分类排名 CSV（52 行 × 4 列）完成“导入 -> 画像 -> 预览 -> 异步导出 -> 原生图表回读”，两例均生成 3 张分析表和 2 个原生图表；下载文件和产物均已清理，不进入用户工作区或离线回归。
- D4.3A 的取消/失败恢复已在上方完成；当前下一步为 D4.3B Commander 路由准入评估。数据 Agent 仍未标记为 `runtime_ready`，不会被总指挥对外正式调度。

## 2026-08-17 数据工作台 D4.1：导出任务历史与 artifact

- 新增数据工作簿异步任务入口：确认导出后立即产生 `task_data_*` Runtime 任务，先写入 queued/running 状态，再通过现有 WebSocket 事件通道发布版本复核、Excel 写入回读、artifact 登记和终态；不会阻塞后续 Qt 的主线程接入。
- 成功导出仍复用 D3 的确定性计算、临时文件和 Excel 原生对象回读；只有回读通过后才向任务历史登记 `data.render_workbook` 工具调用和 `.xlsx` artifact。失败任务保留原因、阶段事件与工具失败审计，不登记半成品文件。
- artifact 的本机输出路径仅保留给后端受控 resolver；`/api/tasks/{task_id}/artifacts` 对 Qt 返回脱敏占位，不再泄露绝对目录。新增 `verify_data_delivery.py` 覆盖任务受理、结果恢复、日志、工具审计、artifact 脱敏、二进制预览拒绝和版本哈希失败分支。
- D4.1 离线回归已通过；D4.2 的 Qt 异步入口、真实阶段与受控历史跳转已在上方完成。后续才讨论取消/恢复与 Commander 路由。

## 2026-08-17 数据工作台 D3：可编辑 Excel 交付

- D1/D2 已完成能力之上，新增 `POST /api/agents/data_agent/analysis/export`：用户先生成并查看受控分析预览，再明确确认导出；后端只接收受控数据引用、预览哈希、目标和固定安全策略，绝不接收客户端输出路径。
- 导出使用同一次确定性聚合的精确数据写入新的 `.xlsx`，包含分析说明、数据概览、质量问题、原始数据、清洗数据、分析工作表和适用的图表页；数据表与图表均是 Excel 原生对象，不使用截图伪装。
- 文件先写为临时副本并重新打开，核对工作表、原生 Table/Chart、关键指标单元格和图表数量后才移动到 `output/data_analysis/`。哈希不匹配、未确认、回读失败或命名冲突均不会生成正式交付物，源文件保持不变。
- 新增 `verify_data_workbook.py`，覆盖未确认拒绝、版本哈希保护、同名新建、artifact URI 脱敏、原生对象回读与源文件不变；D1/D2/D3 回归、全量后端回归和 Qt Creator Debug 构建均已通过。本轮自动化会话无法获取 Qt 窗口句柄，已清理测试进程；“预览详情 -> 确认导出”仍待客户本机可视复核。下一步为 D4：将交付登记到任务历史、接入 Commander 路由，并完成真正的端到端客户验收。

## 2026-08-03 PPT 制作 V3 视觉资产阶段

- 主题与版式层已完成第一轮升级：`executive_blue`、`technology_emerald`、`narrative_warm`、`impact_contrast` 分别使用独立构图规则；内容页只允许对比、流程、时间线、关键点、观点、图文陈述和信息卡等受控版式。没有可靠数据输入时不会伪造数字图表。
- 内置主题与版式始终生效，配图来源才由计划快照固定为 `none`、`pexels` 或 `seedream`。规划阶段零联网、零图片生成；导出确认框会明确说明即将调用的 Provider、数量上限、无可见水印的 AI 生成标识或摄影来源与审计范围。
- 已收到首次真实 PPTX 成功嵌入 Seedream 图片的客户反馈；此前 Qt 端 120 秒总等待窗口短于“四张串行生成 + 文件回读”的最坏路径，可能在文件成功写入后误报 `Operation timed out`。当前已统一为单图 75 秒、客户端总等待 390 秒，并继续做真实稳定性验收。
- 智能制作 PPT 工作台现在明确区分“内置设计”和“配图来源”：内置主题/构图始终生效，客户只选择不加额外配图、Pexels 或 Seedream。配图来源被持久化进计划快照；修改下拉框会使旧计划失效并要求重新生成，避免 UI 选择和实际导出来源不一致。
- Seedream 已作为**图像生成 Provider**接入安全密钥仓储与 PPT 导出链路，不加入聊天 `ModelGateway`，避免图像模型和 Tool Calling 模型混用。导出时最多生成 4 张横向 JPEG，并请求无可见水印；图片仅在内存中校验和嵌入 PPTX，artifact 只保留模型、页面意图、提示词摘要、来源类型和失败说明。
- 首次方舟探测曾返回 `ModelNotOpen`，之后客户已成功导出含 Seedream 图片的 PPTX，说明账号现已能完成至少一次真实生成。`ModelNotOpen` 仍保留为可行动的降级提示：若模型权限变化，系统只回退无图版式，不把整个导出任务误判为成功或失败。
- 本轮已通过 `verify_presentation_studio.py`（含 Seedream 无 Key 离线降级）、`verify_presentation_delivery.py`、`verify_backend.py` 和 Qt Creator Debug 构建。真实生成验收待模型开通后再做，不能提前标记为已验证成功。

## 2026-08-14 PPT 制作 V3：智能研究与数据图表（数量合同已完成离线验收）

- 第三步从最初的公开资料来源扩展为“演示型数据表 + 同口径图表”分级交付：计划阶段仍然零联网；确认导出后才会访问已批准的 Wikimedia、World Bank 或 ResearchGateway Provider。Wikimedia 仍只提供最多 3 条公开参考，不能替代数值证据。
- 公开资料的边界明确为“来源页与任务历史的可追溯参考”：只保存标题、页面链接、短摘要和抓取时间，不把网页正文交给模型改写，不自动填入 PPT 正文，更不会伪装为已核验的统计数据、案例或图表。
- 联网开关、计划快照、确认弹窗、实际请求和 artifact 元数据已保持一致；未带 `network_confirmed` 的资料请求会被后端拒绝。导出后的来源页会替换计划中的“未联网”说明，避免出现自相矛盾的事实边界。
- `verify_presentation_studio.py` 新增 MockTransport 覆盖：固定域名/接口、HTML 摘要清理、去重、确认拦截、来源页回读和 artifact 审计。该回归不联网、不读取真实密钥或写入项目 `output/`。
- 第二段已完成首个受限的结构化数据闭环：当主题匹配受支持的国家/地区以及 GDP、人均 GDP 或人口时，确认导出后才读取固定 World Bank Indicators API；多国采用共同年度，单国最多六个年度。数据表使用 PowerPoint 原生 Table，柱状图与折线图使用原生 Chart，来源 URL、数据点与降级原因写入来源页、artifact 和历史。
- 第三段已完成“主创作规划 + 专用研究规划”：主模型保持稳定的内容/版式最小契约；客户启用智能数据且明确要求数据对比、统计或趋势时，再调用一次无工具研究规划器，输出研究问题、对象、指标、时间与比较口径、目标页面、图表类型、数据点数量、3 至 6 条二次查询和来源偏好。该阶段零联网、零事实数值，不再为每个主题增加专用 MCP；研究规划失败不会拖垮已合法的主 PPT 计划。
- 研究蓝图入口会先本地归一化字段别名、嵌套结构和整数文本，再最多进行 1 次零联网 JSON 修复。2026-08-14 起，Harness 先从客户原句提取原生表格/柱图/折线图的最小数量；研究规划器再根据数量合同选择最多 6 个横向指标、独立趋势指标、检索别名和 3 至 6 条查询。多个表格会按不同指标组拆分，不能再用同一总览表重复凑数。未由客户明确提出的年份仍收敛为“同一来源明确期间/同页读取快照”。
- 固定 World Bank 由 `provider_planned` 表示，通用主题由 `research_planned` 表示；旧快照的 `planned` 继续兼容。ResearchGateway 已接入 Tavily/原生搜索候选、查询相关原文片段、受限正文读取、二次抽取、来源/对象/单位 Verifier 和最多一次补查。检索按共同查询、各对象聚焦查询有限并发执行；同来源同期间数据可画图，不同来源或期间但逐项有证据的数据会降级为带来源/期间的演示对比表，不再因审计级同源门槛整表丢弃。
- `verify_presentation_studio.py` 已覆盖研究蓝图恢复、查询分流、来源快照、逐点证据过滤、重复数量解析、每页指标分组、原生 Table/Chart 回读和 artifact 数量合同。用户示例“至少三个表格、一个柱状图、一个折线图”离线生成 5 个独立数据页：两张不同指标组的横向表、一张趋势明细表、一张柱图和一张折线图；删掉两张图时会明确记录“柱状图 0/1、折线图 0/1”，不再报告完整成功。8 月 13 日的真实足球文件后来发现趋势数据存在联赛/所有赛事口径混合，已撤回其最终验收资格；本轮没有继续消耗真实模型额度，待额度恢复后只做一次固定提示词真实验收。
- 2026-08-14 PPT 制作 V3 第四步状态校正：所有页面的手动淡入转场已通过客户实际放映；正文 `p:animEffect` 虽完成 XML/shape 回读和离线回归，但客户实际放映只看到转场，未看到按点击出现，因此正文动画不算完成并已暂停。后续恢复必须先用真实 PowerPoint 最小样本验证 Timing 语义，不能把 `python-pptx` 或 ZIP 回读当作视觉验收。详细边界见 `docs/PPT_NATIVE_MOTION_DESIGN.md`。

## 2026-07-31 产品方向校准

- 后续功能必须解决具体客户任务并交付可使用的文件、数据或状态变化；不能再把通用 LLM 的摘要、问答、改写、提取和大纲能力简单包装成一级产品功能。
- 文档助手的三个认可方向为：文件转换与处理、智能文档/PPT 制作、文档审查。PDF 整理基础版、项目方案 PPT v1、项目文档审查 v1、论文审查 v1 已完成；其余具体格式、专业范围和增强审查能力仍须按产品价值逐项细化，不代表全部功能已获准实现。
- 原报告助手并入文档助手；RAG 算法和索引作为平台 Retrieval Service；Evaluator/Verifier 仍是所有 Agent 共用的质量验证层。后续获批的 Knowledge Agent 因持有资料生命周期和深度任务而复用现有客户页面，不改变共享检索底座的归属。
- 现有问答、摘要、关键信息卡、结构化大纲、版本链、来源追踪和审计不会盲目删除，其中通用模型能力逐步降为内部步骤或兼容入口，已经验证的工程底座继续复用。
- 后续正式功能开发前，仍应先给用户提供下一项小闭环的选择、推荐方案和取舍，并确认输入、真实产物、支持范围、权限、技术依赖、验证方式与明确非目标。

## 2026-07-31 首个真实文件交付闭环

- 已完成：文档助手的“PDF 整理基础版”，提供合并、提取页面、旋转页面、删除页面四项确定性操作，不调用 LLM。
- 用户从文档助手页的“PDF 整理”打开专用工作区，选择 workspace PDF、填写范围并二次确认。结果只写入 `output/document_processing/`，不会修改、覆盖或删除原文件。
- 后端在生成后重新打开 PDF、核对页数；任务状态、WebSocket 阶段、artifact、Tool 审计和失败原因都复用统一任务历史。
- 当前边界：单文件导入 10 MB、一次输入总量 50 MB、总页数 1000、输出 100 MB；不处理加密/损坏 PDF、OCR、压缩、水印、格式转换或批量队列。

## 2026-07-31 后续交付路线已确认

- 用户确认继续建设三条文档助手交付路线：项目方案 PPT 制作、项目文档审查、论文审查。
- 实现顺序已完成：项目方案 PPT 工作台 v1 的“已核验草稿 -> 逐页计划 -> 用户确认 -> 可编辑 PPTX -> 验证/历史”，随后项目文档审查 v1 与论文审查 v1 均已交付；当前进入真实 Qt 验收与反馈修正。
- 三条路线共用文档助手和通用 Harness，不拆成多个营销式 Agent；不会承诺 OCR、查重、法律结论、企业模板、联网找图或任意复杂版式。
- 2026-07-31 修正：DeepSeek V4 思考模式的 Tool Calling 回合必须回传 `reasoning_content`、工具调用消息的 `content` 不能为空，且不能显式发送 `tool_choice`。该兼容差异已集中修复到 `ModelGateway`，离线回归与隔离真实 `deepseek-v4-flash` 文档助手验收已通过。模型名称并非本次 HTTP 400 根因。
- 2026-07-31 补充修正：当模型首次返回未通过结构化契约的最终回答时，Runner 的格式修复请求会移除那条无 `content`、无 `tool_calls` 的无效 assistant 消息；DeepSeek 不再因该协议形状返回 HTTP 400。最终 JSON 请求会在单次调用内关闭 DeepSeek 思考模式，且未知的需求分类会保守归为 `unknown`、未知简报字段会丢弃而非猜测映射。隔离真实 `deepseek-v4-flash` 的 `requirements` 与 `draft` 验收均已通过。
- 2026-08-03 输出契约修正：文档助手曾把统一 `DocumentModelOutput` 的所有可选字段同时写进模型提示，草稿/PPT 前置任务会在全局 `2048` 输出预算下生成无关数组并截断 JSON，导致一次格式修复仍无法通过。现在按 `output_mode` 向模型声明最小 JSON 契约；草稿、章节创作和审校使用请求级受控输出预算，未改变用户全局设置、Tool 权限或重试次数。OpenAI-compatible 的 `finish_reason=length` 也会被转成脱敏的可行动任务说明。`verify_document_agent_llm.py --fixture planning_document --output-mode draft` 已在隔离 workspace 对真实 `deepseek-v4-flash` 通过：一次读取、4 个带来源草稿章节、无格式修复。
- 2026-07-31 交付体验修正：项目方案 PPT 计划现在会自动重读草稿实际引用的受控材料，复用项目审查规则完成范围、验收、责任、节点、风险依赖与术语预检，并将结构化结果和来源一起展示在计划中。材料缺口作为透明提示而非额外按钮；来源缺失、未核验草稿、超过来源范围等交付前提仍会硬拦截。完整“项目审查报告”改为按需生成，不再被误解为 PPT 制作的客户前置操作。
- PPT v1 仍是“材料到可编辑 PPTX”的受控交付；对话式创作与材料创作双入口、内置模板体系和受控模板库已记录为下一版方案，未在本轮悄然扩张实现范围。
- 2026-08-03 前端工作区校正：左侧导航支持在完整分组导航与 76px 图标轨之间切换；折叠仅降低信息密度，保留 Tooltip、当前页和全部入口，并将折叠与分组展开偏好保存在本机 Qt 设置中。文档交付页保留原有标题图标与渐变 Hero，并将材料选择、交付任务和状态区顶部对齐、限制说明卡高度，避免大窗口把实际任务区拉散成空白。Qt Creator Debug 可见窗口已验证折叠、跨重启复原、展开恢复与正常退出。
- 2026-08-03 审查交互校正：项目文档审查与论文审查均采用“立即受理 -> WebSocket 真实阶段 -> 读取已校验报告”的统一链路，审查期间冻结本次材料选择，避免界面显示与后台实际审查对象不一致。论文审查补齐同形异步 API 与事件回放回归；同步 `/run` 入口仍保留给兼容集成和离线验收。报告窗口可直接清空历史筛选并定位到对应任务，完整证据、工具审计与产物不再要求客户手工复制任务 ID 查找。主工作台会为当前材料保留最近一份已校验审查报告的重新打开入口；审查中禁用该入口，换材料后不展示旧报告。文档交付页补齐底部伸缩项，较高窗口的额外空间稳定落在工作台之后，不再将 Hero、材料选择和交付操作人为拉开。`backend/.venv` 的全量后端回归、项目/论文专项回归与 Qt Creator Debug 构建均已通过；可见窗口在继承 VS Debug 运行时和 Qt 运行库的环境下已验证启动、正常退出及 `8765` 端口回收。
- 2026-08-03 PPT 交付完成态校正：PPTX 回读验证成功后，交付弹窗提供“查看任务历史”并定位到本次任务；文件仍只从受控 artifact 入口打开，Qt 不拼接或暴露本地输出绝对路径。`verify_presentation_delivery.py` 已复跑通过计划确认、同名保护、回读验证与历史 artifact 写入。
- 2026-08-03 审查阅读体验校正：项目文档审查与论文审查窗口会分别记住本机上次调整的尺寸；首次仍使用现有 `980×720`，并保留最小阅读尺寸约束。该偏好只保存窗口几何，不保存报告、材料或后端状态；Qt Creator Debug 构建及两条审查专项回归已通过。
- 2026-08-03 审查来源阅读校正：两类审查窗口新增可调节的双栏阅读，左侧保留完整报告，右侧只汇总待处理问题、优先级、规则检查数和去重后的来源范围；概览不生成新结论、不改变原报告，也不触发模型/文件读取。Qt Creator Debug 构建及项目/论文审查专项回归已通过。
- 2026-08-03 文档助手 V1 工程验收：通用后端回归、PDF 整理、项目方案 PPT 交付、项目文档审查和论文审查五组隔离回归均已通过。该结论只覆盖受控后端链路、产物与审计，不替代客户在 Qt 中对计划阅读、确认、导出、历史产物、长报告、来源栏和窗口缩放的实际验收。
- 2026-08-03 文档助手 V1 客户验收：用户反馈本轮实际使用暂时无问题，PPT 交付与两类审查从“等待 Qt 验收”转入“基于后续真实反馈修正”。这不扩大既定能力边界，也不将 V1 误记为最终完成品。
- 2026-08-03 PPT 制作 V2.2：在原有“一句需求 -> 受控逐页计划 -> 确认交付”闭环上，导出层已增加封面、议程、图文陈述、信息卡、交付表格、总结与来源页等多版式，避免把所有内容压成同一种文本页。Pexels 已作为唯一批准的授权图库 Provider 接入：计划阶段仍零文件写入、零联网；仅在导出时用户勾选外图并于确认框明确批准，才按封面和具体正文页的最多六个语义槽位进行受限 HTTPS 素材读取。图片只会嵌入其绑定页面；单次查询失败不会造成后续图片顺序错位。Provider 会按检索词与图片元数据轻量重排，图片、作者、素材页、许可证说明、槽位映射与失败降级会写入 artifact 和来源页；这不是多模态语义验图，不能承诺每张图库图都完全符合叙事。V2.1 的已持久化未导出计划仍可按原有检索顺序兼容导出。隔离真实 Pexels -> PPTX 验证已确认 3 个来源、3 张嵌入图和 6 页可打开的 PPTX；`verify_presentation_studio.py`、`verify_presentation_delivery.py`、全量后端回归及 Qt Creator Debug 构建均通过。等待客户在实际 Qt 窗口验收更高工作台、长计划阅读与版式效果；不将没有可靠输入数据的图表或 PowerPoint 原生动画误记为已完成。

阶段 0-4B 已达到出口。首个正式 Agent 已按纵向闭环落地：`Agent Definition + 通用 AgentRunner + 受控 Tool`，跨 provider 的 Tool Calls 适配，模型/Runtime 上下文分离，来源 ID 映射，单文档最多 4 个模型 turn / 8 次 Tool 调用，四文档分页跨文档任务最多 10 个模型 turn / 8 次 Tool 调用（最后一轮只允许格式修复），以及 SQLite 任务、日志和工具审计。单文档读取后会关闭 Tool 获取阶段以避免无收益循环；其中关键信息卡、结构化大纲和 Markdown 草稿预览只开放一次连续读取，前者按主题、目的、范围、角色、交付物、节点和风险输出可追溯字段，大纲按章节、写作意图和关键要点输出审阅蓝图，草稿预览按标题和章节正文输出可追溯内容。预览不自动写盘；用户可在结果详情页命名并二次确认后保存，后端只会写到项目根 `output/document_drafts/`、默认拒绝覆盖同名文件，并将 Markdown 产物与 `artifact_saved` 审计事件追加回原任务。已核验草稿还可在独立“模板与交付”工作区选择项目方案、PRD 或会议纪要：系统只按固定 schema 重组已有章节和来源，未匹配的内容保留为补充材料，缺失结构明确列出；全过程零模型、零 Tool、零 workspace 读取、零文件写入，仍须由用户另行确认保存。跨文档问答、整合和对比则必须连续逐份读取用户勾选的 2 至 4 份材料。问答和整合的最终 answer 必须直接关联至少两份不同文件；整合只归并可兼容内容、把冲突列入待确认，不会写回或生成文件；对比的每项结论也必须关联至少两份不同文件的来源。Qt 文档助手页已提供导入、单选或多选材料、常用任务、异步状态和分层来源结果；PDF 以页码、DOCX 以段落/表格回溯来源，解析缓存以 `mtime + size` 自动失效且解析移出事件循环。Commander 对明确 TXT/Markdown/PDF/DOCX 文件名的文档任务会委派同一正式入口：父任务保留计划与最终状态，子任务保留完整 Tool trace 和来源，父产物只存关联 task_id 与脱敏摘要。通用 Code Agent 已取消自研立项；遗留 Code/Report 入口仍只是未就绪占位，其中 Report 产品能力已并入文档助手。

### 本轮校正：文档助手与模型配置

- 模型供应商状态接口现已返回当前 `thinking`，模型页保存 DeepSeek/Kimi 思考模式后刷新或切换供应商不会再显示成未保存。离线回归已覆盖分别保存 Kimi/DeepSeek 的 `enabled`、切回原 provider、Key 隔离与状态回读；API Key 仍不返回明文。
- 历史 PPT 网页证据抽取曾按任务能力自动切到 DeepSeek，现已废止：从 C6.5.2 起，PPT 主计划、数据草稿与联网抽取都严格使用 `document_presentation` 的显式 Route。模型遇到 `content_filter`、HTTP 错误或 JSON 契约失败时会如实报告或降级为本地确定性草案，不能暗中读取另一 Provider 的 Key。

- 文档工作台 v1 的第二个小闭环已完成：新增 `cross_qa`，与多文档对比共用 2 至 4 份材料的连续读取、分页、分块压缩、动态 Tool 收束和 10 turn / 8 Tool 预算；但不强迫用户生成比较报告。Runtime 会直接校验 `answer_source_ids` 覆盖至少两份不同文件，避免“附带条目有多来源、实际回答却只基于一份材料”的伪跨文档结果。Qt 复用已有多选材料区，只新增“跨文档问答”常用任务和对应状态说明，不增加拥挤面板。
- 文档工作台 v1 的第三个小闭环已完成：新增 `synthesis`（跨文档整合），复用 2 至 4 份材料的连续读取、分页、分块压缩、动态 Tool 收束和 10 turn / 8 Tool 预算。它只将可兼容条目归并进结构化需求，并允许同一条目保留多份来源；冲突和证据不足必须保留在待确认问题，禁止模型自行裁定或写回材料。Qt 复用已有多选材料区，只新增“跨文档整合”选项和任务说明。离线回归与隔离 DeepSeek Flash 验收均已通过。
- 文档工作台 v1 的第四个小闭环已完成：新增 `brief`（关键信息卡），基于单份明确选择的材料提取主题、目的、范围、相关角色、交付物、时间节点和风险。字段采用稳定 key，所有内容都必须回溯来源；没有证据的字段不补写。为避免模型把固定字段逐一精确搜索而耗尽预算，该模式仅暴露 `document.read_text`，一次读取后立即关闭 Tool 面再校验 JSON。若模型在一次修复后仍无法输出合法字段 JSON，Runtime 只依据原文明确标题和字段标记生成可追溯保守结果，并明确提示用户复核。Qt 仅增加一个常用任务，完整字段表在既有结果详情页展示，不挤压工作台。离线回归与隔离真实 Provider 验收均已通过。
- 文档工作台 v1 的第五个小闭环已完成：新增 `outline`（结构化大纲），基于单份明确选择的材料输出可追溯章节、写作意图和关键要点，供用户审阅后再讨论分章节创作。该模式与关键信息卡一样只暴露一次 `document.read_text`，避免无收益搜索；它不生成正文、不创建、覆盖或导出文件。模型在一次无工具 JSON 修复后仍失效时，Runtime 仅根据标题以及明确的范围、交付、计划和风险线索生成保守只读蓝图，并附复核提醒。Qt 复用“常用任务”和既有结果详情页的章节导航，不增加拥挤面板。2026-07-24 已通过离线回归、独立 C++ 构建和隔离真实 Provider 冒烟：一次受控读取后返回 3 个带来源章节，未污染日常 workspace/SQLite，也未输出密钥。
- 文档工作台 v1 的第六个小闭环已完成：新增 `draft`（Markdown 草稿预览），基于单份明确选择的材料输出带来源的草稿标题和章节正文。它复用一次 `document.read_text`、既有任务历史和结果详情导航，不新增拥挤面板；用户可在较大阅读区审阅正文与章节依据。与字段卡和大纲不同，草稿在模型 JSON 连续失效时不生成规则兜底正文，避免平台替用户擅自创作。2026-07-27 已通过离线回归、独立 C++ 构建和隔离真实 Provider 冒烟：一次受控读取后返回标题及 3 个带来源草稿章节，未污染日常 workspace/SQLite，也未输出密钥。
- 文档工作台 v1 的第七个小闭环已完成：Markdown 草稿详情页新增命名与二次确认保存。`POST /api/agents/document_agent/{task_id}/save-draft` 不重新调用模型，只接受同一已完成任务的已验证 `draft_title/draft_sections`；它拒绝任意路径、要求 `.md`、仅写入 `output/document_drafts/`、使用文件系统级独占创建避免覆盖同名文件。首次保存后详情页会改为“另存为 Markdown”，给出副本文件名建议；每一份另存结果都保留章节来源脚注、追加独立 Markdown artifact 与 `artifact_saved` 日志。历史页继续通过后端受控预览接口读取该目录，Qt 不自行拼接文件路径。离线回归已覆盖未确认拒绝、同名冲突、多份另存、UTF-8 落盘、来源脚注和历史预览，独立 C++ 构建通过。
- 文档工作台 v1 的第八个小闭环已完成：Markdown 草稿详情页新增“撰写本章”。用户先从已验证草稿中选择一个章节，再输入本章指令并确认；`POST /api/agents/document_agent/{task_id}/draft-sections/start` 只从源任务恢复受控文档引用和章节身份，派生独立子任务重新读取同一材料，结果强制仅含一个保留原章节 ID/标题且带来源的预览。它不改写原草稿、不修改 `output/document_drafts/` 中的文件、不自动保存；失败时 Qt 保留原草稿详情。离线回归覆盖不存在章节拒绝、WebSocket 受理/终态、一次只读读取、章节身份保持和来源，`compileall`、`verify_backend.py` 与独立 Qt CMake 构建均已通过。
- 文档工作台详情体验补齐：已验证 Markdown 草稿新增“复制 Markdown”。Qt 仅在结果完成且每章带来源时启用该操作，复制标题、正文和章节来源脚注到系统剪贴板；不发起后端请求、不写入文件、不改变原草稿或任务状态。操作入口与“撰写本章 / 保存 Markdown”同处详情头部，避免为了一个常用出口增加额外面板、压缩正文阅读区。
- 文档工作台 v1 的第九个小闭环已完成：草稿详情页新增“核验事实”。`POST /api/agents/document_agent/{task_id}/draft-review/start` 仅从已完成草稿任务恢复受控材料和已验证章节，重新读取材料后输出“材料可支持的表述”和待确认问题；结果强制保留原草稿，不改写、保存、覆盖或合并章节，失败时也保留原详情。任务历史会明确记录 `review_draft_facts`，不会把核验伪装成普通分析。
- 文档工作台 v1 的第十个小闭环已完成：草稿详情页的“撰写本章”升级为主操作加下拉菜单，新增“审校本章”。`POST /api/agents/document_agent/{task_id}/draft-sections/review/start` 只从原任务恢复稳定章节 ID、完整草稿快照和受控材料范围；它重新读取材料，返回带来源的问题级别、原文片段、候选建议和理由。Runtime 会拒绝不属于当前章节的原文片段；建议不会自动应用、不修改草稿、不创建文件，历史任务记录为 `review_draft_section`。

- 多文档长材料缺陷已校正：旧版每份仅向模型提供 12,000 字符，四份材料需要“四次读取 + 一次最终 JSON”却仍套用通用四轮上限，导致约第 1063 行后未读、或在最终收束前触及 `max_turns_exceeded`。现在 `document.read_text` 每页最多 48,000 字符并返回连续分页的 `next_start_char`、真实行号和完整读取状态；跨文档问答、整合和对比均强制从文件开头连续读取，常规材料一页读全，接近边界时最多两页/份，选满四份时最多 10 轮，其中额外一轮只允许修复最终 JSON、不能再读取或搜索。超过直接两页范围时已进入“连续分块压缩 -> 最终归并”：每块最多 32,000 字符、最多 12 块、每块至多两次结构化重试；所有块成功才视为全文覆盖，最终只接收带原始行号来源的短摘要。超过预算会明确结束，不会静默截断或无限消耗模型额度。
- Qt 多文档选择已对齐后端契约：一次仅允许 2 至 4 份；超过四份会在提交前说明原因并禁用运行，不再让用户面对泛化的 HTTP 422。网络错误解析也会从 FastAPI 校验响应中提取可读字段原因。
- 用户截图中的 `document.read_text` 连续失败并非 DeepSeek 缺少多模态能力，而是长来源片段超过 `DocumentSourceRef` 的 360 字符协议上限。现在模型片段、审计摘要和最终展示来源分别限长；真实 DeepSeek 长 Markdown 验收已完成读取、两条需求和来源映射。
- 长文档上下文压缩已完成离线和真实模型双重验收：真实 UTF-8 材料超过直接读取阈值后生成 2 个连续分块，最终以 3 个步骤、2 条受控读取审计和 2 个原文来源完成结构化需求结果。PowerShell 发起中文测试时必须显式设置 UTF-8 管道编码，否则材料会退化为单字节字符、错误地绕过压缩阈值；该规则已在 `SKILL.md` 中保留。
- 真实多文档长材料验收已完成：两份分别超过直接范围的 UTF-8 Markdown 共生成 4 个连续分块审计，DeepSeek Flash 的最终归并返回 3 项跨文件比较、4 个来源且未触发格式修复。比较项的来源已断言必须同时覆盖两份材料；离线回归也覆盖相同组合，避免该路径只在真实 API 的偶然回复下成立。
- 当前文档助手是**以只读理解和受控草稿演进为核心的 MVP**：明确选取 1MB UTF-8 `txt/md/markdown` 或 10MB `pdf/docx` 后，执行搜索、读取、摘要、需求/约束提取、问答、多文档共识/差异初步对比与来源追溯。PDF 仅提取可读取文本并显示页码，DOCX 提取段落和表格并显示定位；对比模式至少选择两份材料，逐份读取且每项比较必须关联不同文件的来源。已具备草稿审校、受控章节修订、重新核验、版本链、恢复/差异、固定模板预览及项目方案 PPT v1 导出；仍不是支持 OCR、Excel、自由 PPT 编辑、完整自由编辑版本链、自由自定义模板或正式 DOCX/PDF 写入的完整文档工作台。
- 模型本地配置已升级为 provider 级 DPAPI 密钥映射。切换默认模型不会覆盖其他已配置供应商的 Key；Kimi / Moonshot 已作为 `kimi` profile 接入，供后续图像、视频等多模态 Agent 按已确认的业务场景使用。当前文档助手仍以已验证的 DeepSeek 文本链路为默认，不会因为“支持多模态”就绕过输出与证据验收。
- DeepSeek `deepseek-v4-flash` 与 Kimi `kimi-k2.6` 均已在隔离临时 workspace 通过文档助手真实验收：一次受控读取后返回带来源的结构化需求，未触发格式修复或保守降级。此前 Kimi 的失败根因是误将模型页连通性 Runtime 的 `max_tokens=64` 用于 Agent 验收，导致 JSON 在 requirements 中途截断；正式验收已恢复 Runtime 的完整输出预算。Kimi 已通过文本文档闭环，但其图像/视频能力仍待相关多模态 Agent 规格确认后再单独验收。
- 输出协议已加固：Runner 会扫描回复中所有完整 JSON object，避免被前置 schema 示例误导；首次输出无法解析或未通过 Pydantic 契约时，最多追加一次不开放 Tool 的格式修复。修复次数写入任务最终步骤和注意事项，仍失败才以 `model_output_invalid` 终止。离线故障注入与真实 DeepSeek Flash 的 Runtime/API 隔离复测均已通过；一次上游 HTTP 400 未被误判为 JSON 成功，后续同路径复测正常完成。
- 文档助手已从“等待长 HTTP 响应”改为“立即受理 -> WebSocket 实际阶段事件 -> 读取已校验终态”。流中只展示材料确认、模型分析、Tool 执行、来源校验和终态等事实，不展示未经验证的模型 token；结构化文档任务的最终结论仍必须通过 JSON 和来源 Guardrail。
- 文档页的“常用任务”包括智能分析、需求与验收、项目摘要、关键信息卡、结构化大纲、Markdown 草稿预览、基于文档问答、跨文档问答、跨文档整合和多文档对比；跨文档模式显式勾选 2 至 4 份材料。结果区按结论、摘要、字段卡、大纲章节、草稿正文、跨文档对比、需求/优先级、待确认问题、来源和注意事项分层，避免把结构化结果压成一大段调试式文本。
- 文档助手 UI 采用“工作台预览 + 聚焦结果详情”两层：工作台保留材料选择、任务输入、运行状态与 420px 的即时预览；结果有效后才可进入独立详情页，在更大的阅读区查看同一份结论、来源和注意事项，并可一键返回继续分析。图标、渐变识别区和全局标题层级均保留。该模式落实渐进披露，不再为塞下完整结果牺牲主任务区。
- 详情页已使用 `QTextBrowser` 的富文本锚点实现阅读导航：只为本次真实存在的摘要、对比、需求、待确认项、来源和注意事项生成可跳转分区，同时展示条目概览；运行中导航禁用，失败时仅显示失败说明。导航与预览始终共用同一份 Guardrail 已验证 HTML，避免模型正文碰巧含同名标题时跳到错误位置。
- 前端交互继续采用“状态驱动、动效解释因果”的原则：当前先展示真实的材料确认、分析、工具执行、来源校验与终态，不用伪造百分比或无意义的循环动画；页面转场、面板展开等动效只在可见运行验收和降低动态效果策略具备后再逐步加入。
- `docs/前端设计可借鉴文档.md` 已完整审阅，并已写入 `SKILL.md` 的前端必读规则和 `docs/DEVELOPMENT_ROADMAP.md` 的“前端体验验收基线”。后续 Qt 页面至少按任务工作台、渐进披露、显式状态、可追溯证据、性能与无障碍六项 P0 检查交付；Web 专属 API 只作为设计思想，不机械迁入 Qt Widgets。
- 真实模型曾在两份材料均已读取后继续请求搜索，最终触及 Tool 预算。现已把“材料范围已读全”接入通用 `AgentRunner` 的动态 Tool 收束条件：下一模型轮不再暴露 Tool，只允许输出最终 JSON；隔离真实模型复测已完成，两份材料各读取一次并返回跨文件来源结论。

## 后端已具备能力

- `GET /health`
- `GET /api/agents`
- `GET /api/agents/{agent_id}`
- `GET /api/agents/registry/status`
- `POST /api/chat`
- `WS /ws/tasks/{task_id}`
- `AgentRegistry` 可扫描内置 Agent manifest。
- Agent 生命周期字段已落地：`enabled`、`runtime_ready`、`health`、`maturity` 分离；Commander 只把 `enabled && runtime_ready` 的 Agent 放进客户可执行计划。
- `AgentRegistry` 已加入 manifest `mtime/size` 轻量缓存，常规请求只做低成本签名检查，不重复解析 YAML。
- 内置 Agent manifest 仍包含总指挥、文档助手、代码工坊、报告助手；后两项是历史兼容占位，不代表正式立项或 Runtime 就绪。
- `BaseAgent` 最小抽象已定义，但当前不动态实例化；阶段 5 将优先使用“Agent Definition + 通用 AgentRunner + 受控 Tool”，第三方 entrypoint 动态执行延后。
- 已新增通用 `backend/app/agents/runner.py`：参数 schema 校验、最大 turn / Tool Call 预算、同类 Tool 失败上限、结构化 trace 和停止原因统一由 Runner 管理。
- `ModelGateway` 已提供 OpenAI-compatible 与 Anthropic Messages 的结构化 Tool Calls 适配；文档 Agent 不直接持有供应商协议或 API Key。
- `POST /api/agents/document_agent/run` 已成为文档助手直接入口：只读本次明确选择的 workspace txt/md/markdown、PDF 或 DOCX，返回 `agentflow.document_context.v1`、可读结论和完整任务记录。
- `POST /api/agents/document_agent/{task_id}/save-draft` 已支持在用户确认后把同一任务的已验证草稿保存为受控 UTF-8 Markdown；固定输出到 `output/document_drafts/`，不接收客户端路径、不覆盖同名文件。用户可用新的安全文件名另存多份，每一份都保留独立 artifact / 日志审计。
- 文档助手在无模型 Key 时走确定性 mock，但仍经过同一 Runner / Tool / 来源映射 / 审计链；有可用模型时会切换到真实 provider Runtime。
- 用户 Agent 目录预留在项目根 `agents/`；当前只扫描 `agents/*/manifest.yaml`，不会导入或执行插件代码。
- `Commander` 初版规划服务已独立到 `backend/app/services/commander.py`。
- mock 模式和真实 LLM 模式已共用同一套 Commander `workflow_plan` 生成逻辑。
- `workflow_plan.steps[]` 已预留 `required_permissions`、`risk_level`、`requires_confirmation`。
- `workflow_plan.steps[]` 已包含 `reason` 和 `expected_output`，用于解释每一步为什么被安排、预计产出什么。
- `workflow_plan` 已预留 `version` 和 `validation_errors`，后续 LLM JSON 规划或 Workflow Engine 可复用。
- `workflow_plan` 已包含 `summary`、`max_risk_level`、`requires_confirmation`，用于前端展示整体计划摘要和风险提示。
- `workflow_plan` 已开始按总指挥 MVP 方案扩展协议字段：`schema_version`、`plan_id`、`plan_version`、`intent`、`user_goal`、`clarifying_questions`、`definition_of_done`、`preference_applied`、`budget_estimate`、`workspace_scope`、`next_action`，以及 step 级 `tool_name`、`command_policy`、`success_criteria`、`timeout_ms`、`retry_policy`。
- Commander 规则规划已能对明显含糊的任务生成少量 `clarifying_questions`，并用 `next_action=ask_clarifying_questions` 阻止用户误以为已经可以安全执行。
- Commander 规则规划能区分“定位”和“理解”：纯“搜索/查找/包含”仍生成低成本 `document.search_text`；用户给出 `.txt/.md/.markdown` 文件名并要求读取、归纳或分析时，生成正式 `document_agent/analyze_document`，由同一 `AgentRunner` 完成受控读取、来源映射和结束条件。没有明确文件名时，Commander 先请求选择材料，不会在多个 workspace 文档间擅自猜测。
- `POST /api/tasks/{task_id}/execute` 已把可能等待模型/工具的 Runtime 转移到工作线程，避免阻塞 FastAPI 事件循环；Document Agent 的 async Tool loop 在该受控执行线程内运行。
- 新增 `GET /api/workspace/documents`、`POST /api/workspace/documents` 和 `GET /api/workspace/documents/{document_name}`，可列出、导入和预览受控 workspace 文档；支持 1MB UTF-8 txt/markdown 以及 10MB PDF/DOCX。文本使用 UTF-8 内容，二进制文件使用受限 Base64 传输；后端只接收文件名和内容，不读取任意客户端本机路径，预览接口也不会暴露后端绝对路径。
- 新增 `POST /api/workspace/search`，可在受控 workspace 文档中做精确文本搜索，返回命中文档、行号、短上下文、命中文档列表和唯一命中时的建议读取路径；这是 agentic search / grep 优先路线的第一步，先做“定位”，后续再把命中片段交给 Document Agent 或 RAG 做“理解”。
- 新增 `backend/app/workflow/validator.py`，可校验 step id、依赖引用、DAG 成环、Agent 是否存在、权限字段是否合法、敏感权限是否要求确认。
- 新增 `backend/app/schemas/workflow.py`，定义 dry-run 的 `WorkflowRun` 和 `WorkflowStepRun`。
- 新增 `backend/app/workflow/dry_run.py`，可对计划做只读 dry-run，生成 `workflow_run` 和 task_id 对应的 WebSocket 日志。
- dry-run 日志已支持 `info`、`warning`、`error` 级别；涉及敏感权限的步骤会在执行前插入 `confirmation_required` 警告事件。
- dry-run 步骤输出会包含 `permission_summary` 和 `confirmation_required`，方便前端展示权限确认摘要。
- 已开始补 `RuntimePermissionRequest` / `RuntimePermissionDecisionRecord` 协议壳，为真实 Agent Runtime 预留权限请求与决策边界。
- `/api/chat` 在 Commander 任务下会返回 `workflow_run`，其中 `mode=dry_run`，明确表示未真实执行文件/工具/插件。
- 新增 `GET /api/tasks`，可分页查询工作流历史任务摘要，并支持按 `status`、`mode`、`max_risk_level`、`requires_confirmation` 筛选，返回 `task_id/status/summary/risk/step_count/created_at/updated_at`。
- 新增 `GET /api/tasks/{task_id}`，可按 task_id 查询工作流任务状态。
- 新增 `GET /api/tasks/{task_id}/plan`，可按需读取任务对应的 Commander `workflow_plan`，用于历史详情复盘计划意图、完成标准、预算预估、工作区边界和计划步骤；历史列表仍保持轻量。
- 新增 `GET /api/tasks/{task_id}/steps`，可按 task_id 查询 step 级结果，后续历史详情和真实 Runtime 局部刷新可复用。
- 新增 `GET /api/tasks/{task_id}/metrics`，可查询执行预算和运行指标，包括步骤数、工具调用数、权限请求数、估算 token、耗时和预算是否超限。
- 新增 `GET /api/tasks/{task_id}/evaluation`，可基于现有 run/metrics/tool-calls/permissions 给出任务效果评估摘要，包括步骤成功率、工具成功率、效率分、阻塞/失败信号和下一步建议。
- 新增 `GET /api/tasks/{task_id}/runtime-state`，可查询 Runtime 状态机快照，包括当前状态、是否终态、允许的用户控制动作和下一步可达状态。
- 新增 `GET /api/tasks/{task_id}/artifacts`，可查询任务产物目录；dry-run 使用虚拟产物，runtime 使用受控 outputs 产物，文档草稿保存使用受控 `output/document_drafts` 产物。
- 新增 `GET /api/tasks/{task_id}/artifacts/{artifact_id}/preview`，可按受控规则读取 runtime 文本产物或用户确认的 Markdown 草稿预览；接口只允许各自固定根目录下的文本类产物，限制最大读取字节数，dry-run 虚拟产物会返回不可预览原因，并隐藏后端绝对路径。
- 新增 `GET /api/tasks/{task_id}/tool-calls`，可查询工具调用审计记录；dry-run status 为 `simulated`，runtime 会记录真实工具状态。
- 新增 `GET /api/tasks/{task_id}/logs`，可按 task_id 查询任务日志，作为 WebSocket 的只读兜底。
- 新增 `GET /api/tasks/{task_id}/updates`，会把 logs、steps、tool-calls、permissions、artifacts 和任务状态快照聚合成结构化时间线，供 Qt 后续事件流面板直接消费，避免前端重复拼多个接口。
- updates 的 `artifact_created/artifact_planned` 事件已携带同 step 的输出和工具调用审计，前端可以直接展示产物验证和 `document.context`，不用再额外猜产物来自哪个步骤。
- updates 的 `task_state_snapshot` 已携带确定性 `evaluation` 和 `task_retrospective`：包含结果摘要、综合分、步骤/工具/产物/权限/重试事实、警告和下一步建议，让 UI 不必额外请求 evaluation 也能解释任务成败。
- 新增 `POST /api/tasks/{task_id}/cancel`，当前 dry-run 已完成任务会返回 `accepted=false`，为真实执行器取消协议预留形态。
- 新增 `POST /api/tasks/{task_id}/retry`，可基于缓存 `workflow_plan` 生成新的 dry-run task_id，不重新调用 Commander 或 LLM。
- 新增 `GET /api/tasks/{task_id}/permissions`，可查询任务的权限请求，并支持按 `pending/approved/denied` 决策状态筛选。
- 新增 `POST /api/tasks/{task_id}/permissions/{request_id}/decision`，可写入权限批准/拒绝审计记录；当前只记录决策，不触发真实工具执行。
- 新增 `POST /api/tasks/{task_id}/execute`，可从 dry-run 派生新的 runtime task，也可在 runtime task 等待权限后继续执行。
- 阶段 4B 最小 Runtime 已接入安全内置工具：`document.search_text` 精确搜索受控 workspace 文档，并可在唯一命中文档且计划允许时自动读取受控预览；`document.read_text` 只读受控工作区文本，并返回 workspace `relative_path` 方便前端和产物展示；`document.extract_requirements` 生成结构化摘要并能读取前置搜索/读取步骤的短上下文，若前置文档工具无命中/无预览则以 `missing_document_context` 结构化失败，避免假装已分析文档；`code.generate_code` 写入受控 `code_draft.py`，`report.compose_markdown` 写入受控 `README.md`；当前不执行 Shell、用户代码、联网或插件。
- Code / Report 安全工具已能消费当前任务内的 `document.context`：代码草稿会嵌入前置文档上下文 JSON，报告草稿会生成“文档上下文”章节，展示来源步骤、搜索命中和受控读取预览，避免后续产物只复述 workflow summary；传给 Code/Report 的文档上下文优先使用 workspace 相对路径，不携带本地 data 目录绝对路径。
- Code / Report 产物已加入轻量回读验证：写入受控 outputs 后会按 UTF-8 读回文件并检查关键片段，成功时在 step 输出和 tool_call 结果中记录 `verification`，失败时使用 `artifact_verification_failed` 结构化错误中止。
- 新增 `backend/app/workflow/node_contracts.py`，集中定义内置节点的 Agent/action、稳定 tool name、输入输出 schema、状态写入、权限字段、失败码和评估信号；dry-run 与 runtime 已共用同一套工具名映射。当前已覆盖 `document.read_text`、`document.search_text`、`document.extract_requirements`、`code.generate_code`、`report.compose_markdown` 等阶段 5 MVP 主链路，其中 `document.extract_requirements` 已明确声明会消费前置搜索/读取结果并写入 `document.context`，Code/Report 节点契约也已声明会消费 `document_context`。
- 新增 `GET /api/workflow/node-contracts`，可查询内置 Agent 的 Node Contract，并支持按 `agent_id` / `action` 精确筛选；后续 Qt 调试面板、LangGraph 适配层和验证脚本都可以复用同一份契约。
- 新增 `POST /api/workflow/command-policy/check`，可对命令字符串做静态风险分类，不执行命令；当前会区分只读、诊断、修改、联网和高危命令，返回是否允许、是否需要确认、是否可并发、默认超时、输出截断建议、识别到的命令、风险原因、命中规则 ID、破坏性提示和更安全替代做法，并会结合当前运行偏好返回 `effective_permission_policy/effective_action/effective_reason`，让用户能提前看到该命令在当前权限模式下会被放行、确认还是阻止。接口还会返回 `execution_scope/execution_route/cwd_policy/sandbox_hint/audit_fields/execution_notes`，以及 `runtime_request_status/runtime_ready/permission_required/approval_prompt/block_reason_code/audit_record_preview`，为未来真实 Shell Runtime 预留工作目录边界、用户批准提示和审计记录骨架。这是未来专业 Agent、外部代码工具或其它 Shell 能力接入前的安全前置层。
- 新增 `GET /api/workflow/command-policy/rules`，可查询命令治理规则目录，返回规则 ID、风险级别、默认动作、分类、原因、破坏性提示和更安全替代做法；该接口不暴露内部正则、不执行命令，只给 UI、审计导出和未来 Runtime 壳复用说明文案。
- 新增 `GET /api/settings/runtime-preferences` 和 `PUT /api/settings/runtime-preferences`，可读取/保存平台运行偏好：权限确认策略和 Agent 语言风格。偏好写入 `data/runtime_preferences.json`，只影响默认确认策略、计划偏好快照和表达风格，不保存任务正文或密钥，也不能绕过 Runtime 权限边界和审计。
- Commander 计划的 `preference_applied` 已开始使用运行偏好兜底；真实 LLM 会把人格偏好映射为受控系统提示词，且明确不得改变事实、权限、安全和验证标准。mock 与真实 LLM 两条聊天链路都会读取同一份平台偏好，避免 UI 设置只停留在展示层。
- Runtime 已接入确定性的 Permission Policy：`always_ask` 和 `smart_confirm` 对当前敏感写入继续等待确认；`auto_approve` 可自动批准受控工作区/outputs 文件读写；`full_access` 可减少已注册中风险工具的确认。联网、数据库、Shell、插件、未知权限和高风险操作仍按模式确认或由平台硬拦截，自动裁决会写入 `platform_policy:*` 审计记录和 `permission_auto_approved` 事件。
- Runtime 权限确认已能真实阻塞高风险步骤：未批准时任务停在 `waiting_permission`，批准后继续，取消会落库为 `cancelled`。
- 新增 `ModelGateway` 和 `GET /api/models/providers`，模型供应商不再硬编码为单一 DeepSeek；当前 profile 覆盖 DeepSeek、OpenAI、Anthropic、Qwen 和自定义 OpenAI-compatible 入口。
- 新增 `GET /api/models/config` 和 `PUT /api/models/config`，可读取/写入本地模型配置；非敏感字段保存到 `data/model_config.json`，API Key 使用 Windows DPAPI 加密后保存，响应只返回 `api_key_configured` / `api_key_source` 等脱敏状态。
- 新增 `POST /api/models/test`，可用当前表单内容测试模型连通性；测试使用短超时和小输出上限，不会保存配置，也不会在响应中回显 API Key。
- `ModelGateway` 现在优先读取本地模型配置，其次回退 provider 专属环境变量、通用 `AGENTFLOW_LLM_*` 和旧 DeepSeek 兼容变量；Agent manifest 明确指定 provider/model 时仍优先尊重 Agent。
- `WS /ws/tasks/{task_id}` 会优先推送该 task_id 对应的 dry-run 日志；没有 dry-run 时仍保留 fallback 日志用于通道验证。
- 新增 `backend/app/database/sqlite.py` 和 `backend/app/database/task_repository.py`，用 SQLite 保存 dry-run/runtime 任务状态、计划、step 级结果、执行预算/运行指标、Runtime 状态、产物、工具调用、日志和权限确认审计记录。
- 默认数据库路径：`data/agentflow.db`；可通过 `AGENTFLOW_DATA_DIR` 或 `AGENTFLOW_DATABASE_PATH` 覆盖。
- `GET /api/tasks/{task_id}` 和 `/logs` 会优先读内存缓存，缓存未命中时从 SQLite 恢复。
- `.gitignore` 已忽略 `data/`，避免本地 SQLite 运行数据被提交。
- 可选真实 LLM 聊天调用已抽象到 `ModelGateway`，默认示例配置仍使用 `deepseek-v4-flash`。
- `AGENTFLOW_LLM_THINKING=disabled` 已作为开发默认值，并兼容旧的 `DEEPSEEK_THINKING`；DeepSeek thinking 模式下正文 `content` 偶发为空时可先关闭。
- 模型调用已从业务层抽离到 `ModelGateway`；DeepSeek 只是默认示例配置，不再是架构边界。
- 后端离线验证脚本：`backend/scripts/verify_backend.py`
- 后端真实端口验证脚本：`backend/scripts/verify_live_backend.py`
- 真实模型连通性验证脚本：`backend/scripts/verify_llm.py`

## Qt 前端已具备能力

- `BackendClient` 使用 `QNetworkAccessManager` 请求 HTTP 接口。
- 启动时访问 `http://127.0.0.1:8765/health`。
- 后端在线后访问 `http://127.0.0.1:8765/api/agents`。
- 首页展示后端在线/离线状态。
- 首页和应用中心的 Agent 卡片可由后端 Agent 数据填充。
- 调度台 `sendTaskButton` / `dispatchInputEdit` 可提交 `POST /api/chat`。
- 后端模拟回复和 `workflow_plan` 可写入 `conversationTextEdit`。
- Qt 调度台已轻量展示总指挥新计划字段：计划意图、版本、下一步动作、澄清问题、完成标准、计划偏好快照、预算预估、工作区边界，并在步骤详情里展示成功标准和命令策略；如果后端返回 `next_action=ask_clarifying_questions`，右侧进度会停在“待补充信息”而不是误显示预演中。
- Qt 调度台顶部已增加“开始执行”和“查看历史”承接入口：生成计划后可直接把 dry-run 转入真实 Runtime，执行前会再次确认；请求成功后自动跳到历史任务并聚焦 runtime task。需要澄清的问题不会启用执行入口。
- Qt 调度台已能根据 updates 的当前状态调整承接按钮：等待权限时显示“处理权限”，有产物时显示“查看产物”，并在事件流里提示下一步去历史页确认或预览，避免用户只看到运行日志却不知道该点哪里。
- Qt 调度台与历史页已统一任务阶段表达：dry-run 显示为“预演”，runtime 显示为“真实执行”；调度台执行按钮会按“提交中 / 执行中 / 等待权限 / 执行完成”切换，关键状态事件会提示审查计划、处理权限、查看产物或排查失败的下一步。
- Qt 调度台已为当前非终态任务增加低频 updates 轮询兜底：正常时每 3.5 秒刷新，短暂网络错误后 6 秒重试；终态、阻塞、需要澄清或切换任务后停止。即使 WebSocket 日志暂时断开，调度台仍可通过 HTTP 聚合状态继续更新。
- `taskUpdatesFailed` 现在携带 task_id，调度台与历史页并发请求 updates 时，网络错误不会再被误显示到另一个任务详情。
- Qt 已接入 `QWebSocket`，可连接 `WS /ws/tasks/{task_id}`。
- WebSocket 任务日志可写入 `conversationTextEdit`，并更新 `progress1` 到 `progress5`。
- Qt 历史页已接入 `/api/tasks`、`/api/tasks/{task_id}/logs`、`/api/tasks/{task_id}/steps`、`/api/tasks/{task_id}/metrics`、`/api/tasks/{task_id}/evaluation`、`/api/tasks/{task_id}/runtime-state`、`/api/tasks/{task_id}/artifacts` 和 `/api/tasks/{task_id}/tool-calls`，支持状态、模式、风险级别和确认需求筛选，也支持当前页关键词搜索与上一页/下一页分页。
- Qt 历史页已按需接入 `/api/tasks/{task_id}/plan`，在任务详情中展示总指挥计划摘要、澄清问题、完成标准、计划生成时采用的权限/人格偏好、预算预估、工作区边界、计划步骤、成功标准和命令策略，便于用户复盘“为什么这样安排”。
- Qt 已在后端就绪后拉取 `/api/workflow/node-contracts` 并缓存 Node Contract；历史任务详情的步骤卡和工具调用卡会展示对应工具、权限、失败码、状态写入和评估信号，减少用户只能看原始 JSON 的困惑。
- Qt 历史页详情面板会在原有视觉框架内展示 step 概览、运行态快照、运行指标、工具调用、产物和执行日志，不额外叠加新面板。
- Qt 历史页已能在步骤卡、工具调用卡、产物卡和产物预览弹窗中展示 `document.context` 摘要，包括来源步骤、搜索命中和受控读取预览，方便用户判断代码草稿/报告是否真的使用了前置文档上下文。
- Qt 历史页事件流和调度台事件流也已接入 `document.context` 展示；当 artifact update 携带上下文时，用户在时间线里就能看到产物使用了哪些文档片段。
- Qt 历史页事件流和调度台关键事件已能展示 `task_retrospective` 复盘卡片：包括预演/真实执行状态、综合评分、步骤/工具/产物/权限/重试事实，以及警告和建议；历史页会在事件较多时把最新复盘固定在事件流顶部，并优先显示最近事件，减少用户只看到早期启动流水、原始 JSON 或翻不到最终结论的困惑。
- Qt 历史页已增加当前选中任务的轻量自动刷新：当任务处于 `pending/running/waiting_permission/blocked` 时，会定时刷新 steps、logs、runtime-state、metrics、artifacts 和 tool-calls；进入终态或切换为空态后自动停止。
- Qt 历史页已提供显式“开始执行/继续执行”按钮，调用 `POST /api/tasks/{task_id}/execute`；dry-run 转真实执行前会先给出确认框，执行完成后会聚焦 runtime task 并刷新列表。
- Qt 历史页右侧已增加受控产物工具条：仅在当前任务确实有产物时显示，可选择产物、弹窗预览、复制路径/URI，并只允许打开后端声明的 `agentflow-output://` runtime outputs 文件；其中预览按钮已切换到后端受控预览接口，由后端统一解释 dry-run 虚拟产物、runtime outputs 文本产物、不可预览原因和截断状态。
- Qt 历史页顶部“权限确认”警示条已接入 `/api/tasks/{task_id}/permissions` 和 decision 接口，确认已阅会把 pending 审计写回后端。
- Qt 历史页权限折叠区已能解释 Permission Policy：展示本次策略、裁决动作、策略理由、决策来源和审计备注；全部由平台策略自动批准时，徽章显示“策略已批准”而不是误写成用户“已确认”。开始真实执行弹窗也会展示计划快照中的权限模式，并说明敏感步骤可能自动批准、等待确认或被平台阻止。
- Qt 调度台“＋”按钮与文档助手共用 workspace 文档导入：支持 UTF-8 txt/md/markdown、PDF、DOCX，导入成功会显示安全短预览并自动填入任务输入框；Commander 可识别明确的 PDF/DOCX 文件名并委派正式文档助手。
- Qt 模型密钥页已接入 `/api/models/providers`、`/api/models/config` 和 `/api/models/test`，展示当前运行时、provider profile、transport、默认模型和 API Key 是否已配置；支持本地搜索、手动刷新、保存全局模型配置、写入新 Key、清空本地 Key 和保存前测试连接，不会显示 Key 明文。
- Qt 模型密钥页右侧布局已收紧：Provider Profile 改为可滚动短摘要，当前配置表单使用独立控件高度和 padding，避免输入框文字裁切以及运行时详情和配置区文字重叠。
- Qt 遗留代码工坊页已有“命令安全检查”卡片，接入 `POST /api/workflow/command-policy/check`，可在不执行命令的前提下展示风险级别、当前权限模式下的处理预期、运行请求状态、批准提示、阻止原因码、执行路线、cwd 规则、沙箱提示、后续审计字段、默认超时、输出截断、识别到的命令、命中规则、破坏性提示、更安全的下一步、判断原因、警告和建议；该卡片现在只代表可复用 Governance 能力，不能作为通用代码 Agent 已立项的依据。
- Qt 系统设置页已新增“运行偏好”卡片，可读取/保存权限确认模式和 Agent 语言风格，并以状态徽章展示加载、未保存、保存中、已保存和失败状态；页面保留原有设置图标、hero 渐变和标题层级。
- 历史页静态布局已改回 `mainwindow.ui` / Qt Designer 维护，`mainwindow.cpp` 只负责从 `ui` 取控件、连接信号、填充数据和处理状态。
- 历史页保留了原有 `heroCard` 背景、图标和标题层级，只去掉了重复的临时占位结构，没有破坏整体视觉语言。
- 历史页会将 Commander 的 `agentflow-task://<child_task_id>` 关联产物识别为专业 Agent 子任务：预览显示委派说明，打开动作直接聚焦子任务；子任务不在当前页时自动回到第一页刷新后再定位，不把它误当作普通文件。
- 历史页右侧权限确认区已压缩为紧凑折叠条，“确认已阅”按钮移入同一行，`取消/重试` 挪到任务详情之后，避免遮挡任务摘要。
- 选中历史任务后会自动拉取对应日志，并把 `confirmation_required` / `warning` 日志做更醒目的高亮展示。
- 历史任务详情顶部“权限确认”警示条会优先读取后端权限审计记录；日志事件只作为权限列表尚未返回时的兜底提示。
- 新增 `BackendManager`，使用 `QProcess` 自动启动本地 FastAPI 后端。
- 启动时会先探测 `127.0.0.1:8765`，如果用户已手动启动后端，则直接复用。
- 自动启动时会设置工作目录为 `backend/`，并启动：

```text
python -m uvicorn main:app --host 127.0.0.1 --port 8765
```

- 自动启动优先使用 `backend/.venv/Scripts/python.exe`，也支持通过 `AGENTFLOW_PYTHON` 指定 Python。
- 支持通过 `AGENTFLOW_BACKEND_DIR` 指定后端目录。
- Qt 退出时只关闭自己启动的后端进程，不关闭用户手动启动的后端。
- 后端 stdout/stderr 已被捕获，并临时显示到总览状态区；后续可接入独立日志面板。
- CMake 已链接 `Qt::Network` 和 `Qt::WebSockets`。
- Qt 端无需改 UI，即可通过原 `/api/agents` 接口显示 manifest 扫描出的 Agent。

## 尚未完成

- 二级功能页已开始收敛为紧凑上下文条：全局页标题承担名称，渐变卡片保留图标和必要状态，不再重复放大标题或以大块空白填充。文档助手已升级为“工作台预览 + 结果详情”子页流；插件管理、模型密钥、系统设置及未来已批准的专业入口后续复用这一信息架构。遗留代码工坊入口需在后续导航清理中隐藏或改造成明确的平台治理入口，不能继续暗示未立项能力。

- PPT V3 第三步的 `ResearchGateway` 核心 MVP 已接入并完成真实双对象主题验收：计划阶段只生成对象、指标、口径和 3 至 6 条查询蓝图，确认联网后按共同查询与对象聚焦查询有限并发搜索。Tavily 的 Advanced Search 查询相关原文 chunks 会与受限清洗正文一起进入证据上下文，但服务端生成的 `answer` 仍禁止作为事实。每个数值必须有来源 ID 与原文证据；同口径可画图，不同来源/期间自动降级为逐项标注的表格。整项最多一次补查、两次抽取，不再用重复搜索满足形式审查。搜索、抽取、降级、渲染与回读阶段均进入 Qt 实时反馈和 SQLite 历史。

- Agent Registry 还没有数据库持久化启用/禁用状态。
- Agent Registry 还没有插件签名校验、依赖检查、权限确认 UI。
- 文档助手已完成隔离的真实模型验收：UTF-8 材料经受控读取后，真实模型通过 Tool Calling 生成了带来源的结构化需求、两份超长材料的分块归并/跨文件对比，以及单文档精确搜索零命中后的 search -> read 受控回读；常规离线回归仍强制 mock。后续主要扩大不同 provider 的收束稳定性、真实超时和异常恢复验收。
- ModelGateway 的 HTTP 超时和网络不可达现在分别映射为稳定的 `model_timeout`、`model_connection_failed` 停止原因；AgentRunner 会保留已完成的 Tool trace，文档助手向用户分别提示稍后重试或检查网络、Base URL 与密钥配置，而不是展示 `ConnectError` 等底层类名。离线回归已覆盖两个分支；不为制造真实超时而等待供应商请求。
- 用户明确选择 `requirements` 时，若模型在一次无工具 JSON 修复后仍失败，文档助手会只从已读取原文中带“必须/需要/不得/验收”或 `must/shall/required` 等明确标记的行生成保守需求条目，并保留来源和降级警告；单文档问答、跨文档问答、摘要与多文档对比仍要求模型的结构化结果，不能用这条规则伪装完成。
- Commander 当前只对明确文件名的文档理解任务进入正式文档助手；跨多个候选文档的自动选择和真实超时策略仍需继续扩大验收覆盖。
- Code 与遗留 Report manifest 已明确为 `runtime_ready=false`、`maturity=placeholder`；其遗留 Runtime action 仅保留底层回归覆盖，不能视为正式产品能力。Report 不再作为独立 Agent 继续规划。
- 还没有 PyInstaller 后端 exe，也还没有发布形态的后端启动入口。
- Workflow Engine 仍未执行任意用户代码、Shell、联网或插件；真实工具目前只限受控 outputs 目录内的安全文本产物。
- SQLite 目前已持久化 dry-run/runtime 任务状态、计划、step 级结果、执行预算/运行指标、Runtime 状态、产物、工具调用、日志和权限审计；但真实 Runtime 的失败恢复、真实耗时/token/成本回填和更细粒度工具结果落盘还没有完全接入。
- 阶段 4B 已覆盖工具失败结构化错误、失败计数、重试边界字段、自动重试和超时中断；更复杂的网络/IO 重试只按正式 Agent 的真实需要补充。
- Qt 历史页已经有受控产物打开/复制路径/后端受控预览首版；更完整的运行中状态、二进制/大文件和异常提示按阶段 5 用户闭环需要细化，仍在 `mainwindow.ui` 的现有视觉框架内维护。
- 真实 LLM 已抽象到 ModelGateway，后端已具备 provider 配置持久化和 Windows DPAPI Key 加密存储；Qt 模型页已有基础保存/清空 Key 表单，但还没有用量统计、供应商自定义增删和 per-agent 独立模型覆盖 UI。
- 还没有插件安装及插件级权限确认，也没有打包发布。

## Harness 映射

- Context：受控文档片段、任务目标、必要历史摘要和 Agent Definition；Runtime 本地对象、凭据、绝对路径与模型可见上下文分离。
- Planner：Commander 初版规划器；阶段 5 默认采用 manager 模式，由 Commander 保持任务与最终回复所有权。
- Tool：通用 Runner trace、`document.search_text`、`document.read_text` 和 `agent.document_agent.analyze`；Code / Report 的遗留 action 仅用于底层回归，不属于已上线 Agent。
- Runtime：Workflow dry-run、执行预算、运行指标、Runtime 状态机壳和最小安全执行器。
- Memory：SQLite 任务状态、计划、步骤、执行预算/运行指标、Runtime 状态、产物、工具调用、日志和权限审计。
- Verifier：计划校验器、`verify_backend.py`、`verify_llm.py`、`compileall`。
- Governance：权限请求、用户批准/拒绝、Permission Policy 自动裁决及审计、运行偏好和沙箱边界；审批应作为原 run 的可恢复 interruption，而不是新建割裂任务。
- Command Governance：命令安全策略检查已开始落地，借鉴 Claude Code / Codex 的“只读自动低摩擦、高危默认拒绝、解析不清则提高风险”思路；当前只做静态分类，不执行 Shell，遗留 Qt 代码工坊页只是现有可视承载。命令检查结果会结合平台运行偏好展示预计放行、确认或阻止，也会给出运行请求状态、执行路线、cwd 规则、沙箱提示、审计字段、命中规则 ID、破坏性提示和更安全替代做法；真正执行命令仍必须由 Runtime 再次校验工作目录、参数、超时、输出截断和审计记录。
- Evaluation：后续按 `docs/AGENT_ENGINEERING_GUIDE.md` 建离线用例；当前已开始记录步骤数、工具调用数、权限请求数、估算 token 和耗时，并新增只读任务效果评估摘要接口。
- 外部方法论吸收：State / Node / Graph、Human In The Loop、checkpoint、updates、Guardrails、manager/handoff 和 trace/eval 已纳入 `docs/AGENT_ENGINEERING_GUIDE.md`；LangGraph / LangChain 仍是后续候选，但阶段 5A 先验证单 Agent Runner。
- 检索路线校准：未来知识库、代码检索和长文档问答采用“agentic search first，RAG as needed”的策略；代码和项目文件优先用 grep/ripgrep/glob/read 即时检索，RAG 只作为语义理解、长文档问答和跨文档归纳的可选增强，不把全仓库默认全量向量化。

## 当前工具链记录

- `python.exe` 3.11.9 可用，路径：`D:\environment\python\python-3.11.9\python.exe`
- `py.exe` 可用。
- 后端开发依赖已安装到当前 Python 3.11.9 环境：`python -m pip install -r backend/requirements-dev.txt`
- 普通 PowerShell PATH 里未直接找到 `git`、`cmake`、`nmake`、`cl`、`msbuild`。
- Qt Creator CMake 路径：`D:\IDE\qtcreator\Tools\CMake_64\bin\cmake.exe`
- Ninja 路径：`D:\IDE\qtcreator\Tools\Ninja\ninja.exe`
- Qt Creator JOM 路径：`D:\IDE\qtcreator\Tools\QtCreator\bin\jom\jom.exe`
- VS2022 Build Tools 环境脚本：`D:\IDE\VS2022\buildtools\VC\Auxiliary\Build\vcvars64.bat`
- 旧 Qt Creator Debug 构建目录使用 `CMAKE_MAKE_PROGRAM=jom`；普通命令行构建时改用独立 Ninja 目录 `build/codex-debug` 验证。

## 最近验证基线

本节只保留当前仍有参考价值的验证基线；逐轮流水记录已压缩，后续有复盘价值的历史放到 `docs/WORKLOG.md`。

- PPT V3 数据编排基线：2026-08-14 已通过 `backend/.venv/Scripts/python.exe scripts/verify_presentation_studio.py`、`scripts/verify_presentation_delivery.py` 与全量 `scripts/verify_backend.py`。离线新增覆盖“单对象模型误扩展为双对象”的回归、简短“生涯数据”自动展开为四个互补视图、AI 草稿的混合单位指标画像，以及原生表格/图表回读。真实 UTF-8 回归以 Kimi 规划的“帮我生成梅西生涯数据 PPT，要包含多种数据”为输入：计划实体仅为梅西，导出数据为 1 张原生表格和 3 张原生图表，已回读成功。Seedream 已按 DPAPI 配置新 Key、官方单图协议与三次有界重试实际请求；仍返回 HTTP 429，客户端会安全降级为内置版式，不把它误报为 Key、模型名或 PPT 嵌入错误。
- 2026-08-13 稳定性修正：PPT 回读告警超过 `PresentationVerification.warnings` 的 6 项上限时，过去会在成功写出文件后触发 Pydantic `too_long` 并返回 HTTP 400。现在回执保留前五条并把其余压成任务历史摘要，完整 artifact 告警仍保留；成功产物不再因展示层告警数量被撤回。
- 2026-08-13 模型设置修正：`GET /api/models/providers` 的当前运行时状态补回 `thinking` 字段。DeepSeek/Kimi 保存“开启思考”后，Qt 刷新不再错误回填为关闭；后端回归覆盖保存、切换 provider、再切回和供应商列表读取。
- 同日对 DeepSeek 原生搜索的同会话 JSON 抽取做了脱敏探针：即使关闭思考，响应仍包含搜索块和文本块，但没有稳定返回可解析的来源/数据 JSON。该能力没有接入正式交付；系统不会把供应商的自然语言搜索总结误当成可验证数据 API。

- UI 工作台/详情基线：2026-07-20 文档助手新增结果详情子页和条件式阅读导航，预览与详情复用同一份已校验 HTML，运行中不会把旧结果伪装为本次结论；`查看详情` 仅在有可复核结果时可用。已通过 `build/codex-debug` 的 Ninja 构建，以及 Qt Creator Debug/JOM 构建的 UIC、MOC 与链接验证。可见窗口烟测在窗口句柄可用后关闭通过，退出后无 `8765` 残留监听。命令行独立启动需在 PATH 提供 Qt `bin` 中的 Debug DLL，Qt Creator 运行配置仍是日常可视验收入口。

- 后端离线基线：2026-07-20 已通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`。覆盖 `runtime_ready` 路由边界、选定文档的 Runner/Tool/来源/历史闭环、两份材料逐份读取和跨文件来源的多文档对比、四份接近真实规划文档大小材料的完整读取、异步 `/start` 的四材料受理、长文档的连续分页协议、Commander 到正式文档助手的父子任务委派、纯搜索不触发模型、多文档未选择、缺失文档，以及带自然语言前缀/代码围栏/简写清单的模型输出 Guardrail；旧 Code/Report Runtime 仅作为显式构造的底层回归计划，不由客户入口生成。
- 后端离线基线：2026-07-22 已再次通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`；新增覆盖模型超时的稳定 `model_timeout` 停止原因，并保留零命中回读、一次无工具 JSON 修复与已有长文档/多文档回归。
- 后端离线基线：2026-07-22 已再次通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`；新增覆盖“读取成功、模型两次 JSON 失败后 requirements 保守降级”的来源、警告和完成状态，以及 OpenAI-compatible / Anthropic 两类 Tool Call 的消息序列化、函数别名、Tool Call ID、Kimi 思考回传和 Tool Result 解析契约；保留模型超时、网络不可达、零命中回读、一次无工具 JSON 修复与已有长文档/多文档回归。`scripts/verify_document_agent_llm.py` 可在不切换默认模型或污染日常数据的前提下，对任一已配置 provider 验证真实 Document Agent 的 Tool Calling、JSON Guardrail 和来源闭环。
- 后端离线基线：2026-07-22 已再次通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`；新增 `cross_qa` 的两份材料逐份读取、最终来源覆盖和“仅选一份材料则澄清”回归。该模式复用多文档预算，不强迫输出比较项。同日新增 `synthesis` 回归：两份材料连续读取，兼容的重复要求归并为保留两份来源的结构化条目，不产生比较项或文件写入。随后新增 `brief` 回归：固定字段完整、每项有同一份材料来源、不混入 requirements，且只发生一次 `document.read_text`；模型连续返回无效 JSON 时只读保守字段降级也有固定覆盖。
- 后端离线基线：2026-07-24 已通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`；新增 `outline` 回归：结构化大纲只读取一次当前材料、每个章节均有来源且不混入 requirements；模型连续返回无效 JSON 时会生成仅依赖标题/范围/交付/计划/风险显式线索的保守只读蓝图。Qt `build/codex-debug` CMake 构建也已通过，结果详情可按“大纲章节”定位阅读。
- 后端离线基线：2026-07-27 已通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`；新增 `draft` 回归：Markdown 草稿预览只读取一次当前材料，草稿标题与每个章节正文都有来源且不混入 requirements；模型连续返回无效 JSON 时任务保持 `model_output_invalid`，不会自动拼接正文。随后新增确认保存回归：未确认返回 409、同名不覆盖、同任务可用另一文件名另存独立产物、Markdown 以 UTF-8 写入临时受控目录且保留来源脚注，任务产物预览可读回同一内容。Qt `build/codex-debug` CMake 构建也已通过，结果详情可按“Markdown 草稿”定位阅读并显示保存入口。
- 后端离线基线：2026-07-27 已再次通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`；新增覆盖分章节创作派生任务的不存在章节拒绝、`task_queued -> scope_ready -> task_completed` 实时事件、单次 `document.read_text`、章节 ID/标题保持和单章来源结果。独立 `build/codex-debug` Qt CMake 构建也已通过。
- 后端离线基线：2026-07-27 已通过 `python -m compileall -q app scripts` 与 `python scripts/verify_backend.py`；新增覆盖草稿事实核验派生任务的 `task_queued -> scope_ready -> task_completed` 实时事件、原草稿快照保持、一次只读读取、带来源的支持表述、零产物写入以及 `review_draft_facts` 审计动作。本轮未改动 Qt 代码或 `.ui`，不重复构建桌面端。
- 后端与 Qt 基线：2026-07-27 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增本章审校的错误章节拒绝、异步事件、原草稿保持、原文片段归属、一次 `document.read_text`、零产物写入和 `review_draft_section` 审计覆盖。Qt 详情页以分裂按钮承载“撰写本章 / 审校本章”，不额外压缩结果阅读区。
- 后端与 Qt 基线：2026-07-27 已再次通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增“审校建议 -> 修订预览”闭环：错误 suggestion 拒绝、`task_queued -> scope_ready -> task_completed`、唯一原文片段精确替换、修改前/后差异、完整新草稿、原审校/原草稿保持、零模型/Tool/产物写入与 `create_section_revision_preview` 审计动作。Qt 将入口放在“撰写本章”下拉菜单，详情页新增充足阅读空间的差异区，确认后仍只允许另存新 Markdown 文件。
- 后端与 Qt 基线：2026-07-27 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增“多建议合并修订预览”：同一审校任务选择 2 至 6 条 suggestion ID，重复 ID 在协议层返回 422，每条原文须唯一且区间不得重叠，Runtime 按原文位置倒序合并并生成独立完整草稿。全过程零模型/Tool/产物写入，原草稿、审校任务和已有文件保持不变；Qt 将入口置于既有“撰写本章”分裂菜单，并以一次性勾选对话框避免挤压详情页。
- 后端与 Qt 基线：2026-07-27 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增版本链 v1：每个可保存草稿快照都持有版本 ID、根草稿、直接父任务、类型与变更摘要，派生任务会继承根草稿而不复制正文；详情页新增独立“版本链”阅读区，保存 Markdown 注释与 artifact 元数据同步保留版本身份。旧快照只支持回看或另存，未开放覆盖式回滚或自由编辑。
- 后端与 Qt 基线：2026-07-27 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增历史快照恢复预览：详情页的版本菜单和任务历史中的“恢复草稿”都只能从已完成文档分析任务恢复已验证快照。Runtime 会校验版本身份后创建独立子任务，继承根草稿并把源任务记为直接父版本；恢复全过程不调用模型、不读取工作区、不调用 Tool、不写文件，也不会覆盖旧任务或旧 Markdown。恢复后的草稿仍须经用户明确另存才会写入 `output/document_drafts/`；协议拒绝额外正文、路径和覆盖字段，离线回归已覆盖未知任务、事件流、版本关系、零副作用与保存后的 artifact 元数据。
- 后端与 Qt 基线：2026-07-27 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增父版本双栏差异：`GET /api/agents/document_agent/{task_id}/version-diff` 只读取当前草稿与直接父快照，按稳定章节 ID 标记未修改、修改、新增或删除。初版无父版本时明确返回 400；查询不创建任务、不调用模型、Tool 或 workspace，也不写文件。Qt 复用详情页的版本操作菜单，并在独立、可伸缩的双栏阅读窗口展示两份正文，避免压缩结果详情。离线回归已覆盖初版拒绝、修订差异和恢复快照的零差异。
- 后端与 Qt 基线：2026-07-29 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增受控手动章节修订：详情菜单在独立大编辑窗中按章节建立 `manual_revision_pending_review` 子版本，后端重新绑定任务、版本、原章节正文，拒绝额外字段、无变化正文与越界身份。预览零模型、零 Tool、零文件写入，详情展示修改前后差异；`draft_verification_state=requires_review` 会同时在 Qt 和 `save-draft` 后端硬性禁用保存，只有后续“核验事实”重新读取材料且没有待确认项才恢复可保存状态。离线回归已覆盖事件流、父子版本、旧稿不变、保存拦截和差异查询。
- 后端与 Qt 基线：2026-07-29 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake 构建；新增固定内置模板交付预览。`POST /api/agents/document_agent/{task_id}/template-preview/start` 只接受项目方案、PRD、会议纪要的稳定模板 ID，并且只允许已完成、`draft_verification_state=verified` 的 Markdown 草稿。Runtime 从 SQLite 恢复快照后按保守标题线索重排已有章节与来源，未匹配内容保留为“补充材料”，缺失模板结构单独列出；全过程零模型、零 Tool、零 workspace 读取、零文件写入，建立 `template_preview` 子版本。Qt 通过草稿操作菜单打开独立“模板与交付”工作区，预览后仍须走现有 Markdown 命名与二次确认保存。
- 后端与 Qt 基线：2026-07-29 已通过 `python -m compileall -q app scripts`、`python scripts/verify_backend.py` 与独立 `build/codex-debug` CMake + Ninja 构建；新增同根草稿三方章节合并预览。候选接口只返回同根、已完成、已核验的完整版本；计划接口从两端版本链找共同祖先，自动保留未同时修改章节，并将标题、正文、新增或删除冲突交由用户逐项选择。创建接口拒绝漏选、未知冲突、额外正文和跨根版本，成功后建立 `merge_preview`，当前版本为直接父版本，候选与共同祖先写入审计。全过程零模型、零 Tool、零 workspace 读取、零文件写入；离线回归已覆盖冲突拦截、WebSocket 终态、版本血缘、旧稿保持和零副作用，Qt 在独立版本选择/冲突确认窗口处理复杂操作，不挤压结果阅读区。
- PDF 整理验证：2026-07-31 `backend/.venv/Scripts/python.exe scripts/verify_pdf_processing.py` 已覆盖合并、提取、旋转、删除与错误路径；`build/codex-debug` 的 Ninja 构建已通过 UIC、MOC、C++ 编译和链接。该专用工作区承载长文件列表与结果，不继续挤压文档分析主页面。
- Qt Creator / GUI 烟测基线：2026-07-20 文档助手结果详情和多材料选择修正后，可见窗口启动/关闭烟测通过，关闭后 `127.0.0.1:8765` 无残留监听。
- 真实端口基线：`python backend\scripts\verify_live_backend.py` 曾在真实 Uvicorn 端口下通过；常规后端改动优先跑离线验证，避免无意义启动/清理。
- 真实模型基线：2026-07-16 已用隔离临时工作区完成文档助手真实模型验收：单文档受控读取 1 次、带来源的结构化需求 2 条、无重复搜索；多文档对比复测中两份材料各读取 1 次，返回 1 项跨文件来源结论。2026-07-22 又完成两份超长材料的 4 个连续分块、跨文件来源归并，以及单文档精确零命中的 `document.search_text -> document.read_text` 回读验收；随后 DeepSeek `deepseek-v4-flash` 与 Kimi `kimi-k2.6` 的单文档需求提取均以完整 Runtime 输出预算返回 `completed`、一次读取和带来源结果。同日新增的 `cross_qa` 也在隔离 DeepSeek Flash 环境完成两份材料读取与跨文件来源覆盖验收；随后 `synthesis` 在同一隔离 DeepSeek Flash 环境读取两份材料、执行 4 次受控 Tool 调用并返回 3 个可追溯整合条目。关键信息卡也在隔离真实 Provider 环境完成一次读取、7 项固定字段 JSON 和来源映射验收；该模式已关闭逐字段搜索工具面。所有验收均未显示或持久化 Key/测试材料。`backend/scripts/verify_llm.py` 继续用于通用聊天连通性，`backend/scripts/verify_document_agent_llm.py` 用于真实文档 Agent provider 验收，常规回归默认 mock。
- 真实模型基线：2026-07-16 已用隔离临时工作区完成文档助手真实模型验收：单文档受控读取 1 次、带来源的结构化需求 2 条、无重复搜索；多文档对比复测中两份材料各读取 1 次，返回 1 项跨文件来源结论。2026-07-22 又完成两份超长材料的 4 个连续分块、跨文件来源归并，以及单文档精确零命中的 `document.search_text -> document.read_text` 回读验收；随后 DeepSeek `deepseek-v4-flash` 与 Kimi `kimi-k2.6` 的单文档需求提取均以完整 Runtime 输出预算返回 `completed`、一次读取和带来源结果。同日新增的 `cross_qa` 也在隔离 DeepSeek Flash 环境完成两份材料读取与跨文件来源覆盖验收；随后 `synthesis` 在同一隔离 DeepSeek Flash 环境读取两份材料、执行 4 次受控 Tool 调用并返回 3 个可追溯整合条目。关键信息卡也在隔离真实 Provider 环境完成一次读取、7 项固定字段 JSON 和来源映射验收；该模式已关闭逐字段搜索工具面。2026-07-24，结构化大纲在隔离临时 workspace/SQLite 中完成真实 Provider 冒烟：一次读取后返回 3 个带来源章节，`status=completed`。所有验收均未显示或持久化 Key/测试材料。`backend/scripts/verify_llm.py` 继续用于通用聊天连通性，`backend/scripts/verify_document_agent_llm.py` 用于真实文档 Agent provider 验收，常规回归默认 mock。

**2026-08-14 当前校正：**普通数据型 PPT 的默认路径已由“先联网检索与核验，再补 AI 草稿”切换为“专用规划器建立合同 -> 确认导出后模型直接生成数据 -> 原生 Table/Chart 回读”。客户点明的表格、柱图、折线、饼/环图和总数量均为硬合同；单对象展示型饼图或指标画像不会再因联网口径规则被误删。客户原句可识别出的对象范围已成为本地 Harness 约束，防止“梅西生涯数据”被模型错误扩展为“梅西与 C 罗对比”。真实回归已通过该原句的 1 表 3 图回读；Seedream 图片真实请求仍返回 Provider HTTP 429，现已采用官方单图参数与三次有限重试，继续安全降级而不伪造有图成功。

**2026-08-14 DeepSeek Harness 启动：**已依据官方仓库、Node CLI、TypeScript SDK、Python SDK、平台包及 Session/Persistence/Compaction/Sandbox/MCP 文档形成集成方案。结论是将官方 Node CLI `@deepseek-ai/dsh@0.1.0-rc.6` 作为 Windows 首期可选 `ExecutionBackend`，不替换 AgentFlow 的总指挥、ModelGateway、权限审计、任务历史、产物和 Verifier。Python SDK 没有 Windows Runtime wheel，不作为首期入口；正式产品采用目录式安装与桌面快捷方式，不要求物理单文件。详细门槛与阶段见 `docs/DEEPSEEK_HARNESS_INTEGRATION_PLAN.md`。

**2026-08-14 DeepSeek Harness H0：**已将官方 Node CLI 精确安装到 `backend/runtime/deepseek_harness_node/`，`package-lock.json` 锁定 `@deepseek-ai/dsh@0.1.0-rc.6`，本机 Node 为 `v22.22.3`。`dsh --version`、`dsh --profile headless --dump-default-config`、`GET /api/harness/runtime` 和 `backend/scripts/verify_node_harness_runtime.py` 均已在无 API Key、关闭遥测、不开启模型/Agent/Tool 的条件下通过。后端新增只读 Runtime 状态接口与 `AGENTFLOW_NODE_HARNESS_ENABLED=false` 开关。

**2026-08-14 DeepSeek Harness H1 准入复核：**项目内官方 README 与组合配置确认：`headless` 只支持“一次任务 -> 最终文本 -> 退出”，不能直接提供 Qt token 级流式；默认 base profile 装配 PowerShell、文件、网页搜索、子 Agent、Workflow，并会按继承环境、`$DSH_HOME` 和启动目录 `.env` 解析凭据。H1 已新增外部执行后端的非敏感 `execute_task` 契约、四类规范化事件定义与零副作用 fake backend 回归；同时以项目自带 `agentflow-readonly` profile 在临时数据目录执行官方 `--dump-config`，确认 `read-only` / `ask` 和 28 项禁用能力，且没有创建 `.credentials.yaml`。受控 Node Bridge 仅在功能开关、DeepSeek Provider 与 `read-only` 三项都通过时，才临时向子进程注入本次运行配置；当前没有 Router 或客户任务引用该类。恢复和关闭协议尚未实现。Node Runtime 仍保持未启用，不能用默认 profile 在客户 workspace 中直接启动。

**2026-08-14 Agent 与 Harness 路线决策：**不再自研通用代码工坊 Agent，也不以复刻成熟 coding agent 为阶段目标；旧 manifest、Node Contract、命令检查卡片和导航入口暂按兼容资产记录，后续 UI 清理另行安排。专业 Agent 从现在起一次讨论一个，只有用户确认客户价值、交付物、范围和验收后才实现。Harness H2 可继续做隔离只读工程试点，但 Router 只为已确认场景开放；H3 写入/批准、H4 MCP 按实际需要进入，H5 仅在发行版确定携带 Harness 时启动。

**2026-08-17 数据工作台 D1：**已完成单个 `.xlsx/.csv` 的受控 Base64 导入与独立 `data_workspace`，20MB/100,000 行/100 列/10 可见工作表硬边界、自动主表与候选表头识别、字段类型/缺失/唯一值/数值或日期范围/重复行画像、最多 20 行 × 20 列预览、文件版本 LRU 缓存，以及 Qt 的准备态与文件就绪态。D1 不调用模型、不联网、不修改源文件、不创建分析交付物；`data_agent` manifest 保持 `runtime_ready: false`。离线回归覆盖中文 Excel 的第二行表头、数值/日期/缺失/重复行、GB18030 分号 CSV、路径拒绝与源文件不变性；`build/codex-debug` 与 Qt Creator Debug/JOM 构建均通过，最新 Qt Creator Debug 可见窗口启动/关闭烟测退出码为 0。其后的 D2 已在下一条记录完成。

**2026-08-17 数据工作台 D2：**已完成受控本地分析预览：D1 结构画像驱动有限标准计划，Validator 拒绝未知列、非白名单操作与不适用字段组合；执行器只在受控副本上做安全文本首尾空白规范化、数值概览、类别聚合和月度趋势，返回有限聚合表、原生 Excel 图表所需合同、质量提示、单项跳过和脱敏阶段 trace。Qt 主页面只增加一句目标、一个主操作和状态，完整预览放入可伸缩独立窗口，避免压缩文件/字段阅读区。D2 不调用模型、不联网、不写 Excel、不持久化正式任务历史；`runtime_ready` 仍为 false。`python -X utf8 scripts/verify_data_workspace.py`、`python -X utf8 scripts/verify_data_analysis.py`、`python -X utf8 scripts/verify_backend.py`、Qt Creator Debug/JOM 构建和可见窗口启动/关闭烟测均通过；窗口退出码为 0 且无 `8765` 遗留监听。下一步才是 D3 的临时工作簿、原生 Table/Chart 回读和原子交付。

**2026-08-17 数据工作台 D5.3 客户反馈修正：**用户反馈部分 CSV 画像出现“未找到指定的数据文件”的 HTTP 404，同时字段加工入口过于密集。根因是 Qt 为数据集路径先做百分号编码、再以 `QUrl::setPath()` 处理，中文、空格和括号等文件名被二次转义；现改为一次自然路径编码，并增加“工作区副本缺失 -> 自动刷新一次 -> 明确提示重新导入”的恢复链，避免重复请求和内部错误暴露。`verify_data_workspace.py` 已覆盖 GB18030、UTF-16、UTF-8 边界以及中文/空格/括号文件名的正确 HTTP 路径。字段加工已迁为 `datatransformationdialog.ui` 的独立渐进披露工作台：操作、字段与参数按需出现，主页面不再堆叠设置；技术验证包含 Designer XML 与 Qt Creator Debug 构建。**这不是客户验收通过声明，仍待用真实 CSV 完整复验画像、预览、确认导出和历史。**

## 推荐下一步

> **当前唯一开发动作（2026-08-28）：调度台可靠性整改的 R3/R4 收束。**优先统一仍未覆盖页面的异步状态、失败恢复和紧凑布局，再实现客户主动触发的 Provider 模型检测/枚举；总指挥自然语言能力只能沿已验证 action 目录逐项扩展，不以“模型似乎能理解”替代 Tool、权限、产物和回归。

**2026-08-26 总指挥 C6.1-C6.2.5 已完成：**资料库已绑定且计划已准入 `knowledge_agent.answer_question` 时，表达模型不再能把它错误说成“无法访问”；Commander 先生成已校验计划、再约束自然语言回复，明确冲突时使用确定性计划说明。Qt 在后端未 ready 时暂存一条冻结材料范围的请求，ready 后自动发送，失败则还原输入。AI 调度台现在自动维护 `conversation_id`：SQLite 保存完整的脱敏会话归档、受控摘要、材料引用和最近 task/plan 指针；模型上下文始终只取摘要与最近 8 条消息，避免长会话无界烧上下文。客户可从对话标题右侧的会话历史图标切换当前范围内的最近会话，或打开完整记录按页回看；Qt 设置仍只保存不透明会话 ID。客户在同一会话出现“刚才/上一步/这份资料”等明确指代时才复用此前确认材料，新会话或项目范围切换不继承旧范围。计划阅读和计划修改已拆进独立标签，聊天区只保留计划摘要，完整 Node Contract、工具参数与日志留在计划/历史 Inspector。文档助手的 workspace 列表改为真实准备、加载、失败重试状态，不再永久停留在初始化占位。长期记忆仍保持客户可查看、关闭、编辑、删除和确认，绝不把自动会话升级成永久画像。`verify_commander_c6_conversation.py`、Python 编译检查与 Qt Creator Debug 构建已通过；后续先做 C6.3 的显式组合材料、依赖图和有限并发计划，再做 C6.4 真实组合 Runtime/结果汇总。C6.5 显式模型 Profile 与 C6.6 `@Agent` 路由约束已写入计划，尚未实现。详细范围与验收见 `docs/COMMANDER_C6_PRODUCT_PLAN.md`。

**2026-08-26 总指挥 C6.3 已完成：**AI 调度台现在把本轮选中的文档、资料库、数据集显示为可移除材料标签；从各工作台带入一项材料不再静默清空其它已显示选择。总指挥针对同时绑定的只读专业材料生成计划级 DAG：各专业步骤共同依赖任务分析、带同一并行组，最后追加汇总节点。`verify_commander_c63_composition.py` 覆盖组合材料、依赖、并行组、汇总节点与 dry-run 协议；不读取客户文件、不调用模型或网络。

**2026-08-26 总指挥 C6.4 已完成核心闭环：**Native Runtime 已实际执行白名单组合：文档分析/精确搜索、数据工作台只读预览、知识库可信问答。每轮最多 2 个并发槽位、最多 3 条专业分支；父 Runtime 在创建子任务前预留 12 次父级 Tool 预算与 240 秒任务预算，并持续落 SQLite checkpoint 与 append-only 事件。分支失败不会取消其它独立只读分支，父汇总只记录已完成子任务的脱敏短结论与关联 task ID；存在失败/阻塞时明确显示“部分完成”，不会把未完成分支写进最终结论。深度分析、导出、OCR、联网、写入和未知动作仍被 `requires_composition_runtime` 拒绝，避免越权并发。`verify_commander_c64_runtime.py` 已覆盖两个并发槽位、单分支失败隔离、部分结果范围、预算和事件；`verify_commander_c63_composition.py`、`verify_backend.py`、Python 编译与 UTF-8 检查均通过。下一步是 C6.5 的显式模型 Profile，随后才是 C6.6 的 `@Agent` 路由约束；Qt 真实多材料组合仍需客户现场验收，不能把 fixture 验证误写成全场景验收。

**2026-08-26 总指挥 C6.5.1-C6.5.2 模型路由：**新增按作用域保存的 Model Route Profile，密钥仍只在既有 Provider 级 DPAPI 安全存储中保存。总指挥规划、文档分析/PPT、数据洞察、知识库问答和深度任务已通过同一 `ModelGateway` 按作用域解析；显式 Profile 在保存前会校验 Provider Key、思考模式与所需 JSON/Tool Calls 能力，不能失败后静默换模型。模型密钥页现有独立“任务模型路由”检查器：列表定位作用域，右侧只编辑当前一项，且不复制 Key、不调用模型、不挤压 Provider 表单。PPT 主规划、数据草稿及联网抽取的旧跨 Provider DeepSeek 回退已删除，客户选择失败时只会得到明确原因或本地确定性草案。总指挥聊天响应和 dry-run 任务已登记脱敏的 Profile/provider/model/thinking 快照，视觉生成保持预留态。专业子任务历史页的逐轮模型审计仍未补齐，不能写成“所有任务都已有完整模型轨迹”。`verify_model_routes_c65.py` 已在 fake 模型下通过继承/显式 Profile、能力拒绝、保留态和任务快照回归，不联网、不读取客户资料或真实密钥；Qt Creator Debug CMake 构建通过。

**2026-08-26 总指挥 C6.6 `@Agent` 路由约束已完成：**AI 调度台可通过输入框旁的受控 `@` 菜单或直接输入 `@文档助手`、`@数据工作台`、`@知识库` 选择本轮路由偏好，并以可移除标签显示。标签不是权限提升、MCP 自动加载或新的 Agent 发现入口；后端会独立规范化有限别名并把结果写入 `WorkflowPlan.agent_hints`。客户点名一项时，计划、工作区范围和会话材料快照只保留对应类型的已选材料，避免其它同时挂着的私有文档、数据集或资料库被意外继承；若点名但缺材料，只返回明确澄清。多个已绑定的只读能力继续由 C6.4 Native Runtime 并行和汇总，深度分析、OCR、导出、联网、写入和未知能力仍不能借 `@` 绕过准入。`verify_commander_c66_agent_hints.py`、C6.3/C6.4 离线回归、Python 编译、UTF-8/XML 检查和 Qt Debug 构建均通过；未调用真实模型、未读取客户资料。

**2026-08-26 总指挥 C6.5.3 专业子任务模型审计已完成：**文档分析、数据洞察、知识库问答及知识库深度 Map/Reduce 会将实际 Route Runtime 的脱敏 `stage/route/profile/provider/model/thinking` 快照写入既有 `WorkflowRun.model_routes`。`GET /api/tasks/{task_id}/model-routes` 只按需返回这份白名单数据；历史页标题行只有任务存在快照时才显示模型明细入口，正文保持一行紧凑摘要，旧任务明确显示“历史版本未记录实际模型路由”，不猜测或回填。`verify_model_route_audit_c653.py` 已在临时 SQLite 覆盖单阶段、多阶段多模型、历史缺失与 Key/Base URL 脱敏；`verify_model_routes_c65.py`、C6.4 Runtime、全量后端回归、UTF-8/XML 检查与 Qt Creator Debug 构建均通过。未调用真实模型、未读取客户材料或真实密钥。**C6 当前已批准范围已收尾；下一项必须先讨论面向客户的真实交付价值，不能自行新增 Agent 或扩展范围。**

**2026-08-21 C4 已完成：**知识库页在当前资料库索引可用时提供“交给总指挥”入口；它只带入用户明确选择的稳定资料库 ID，跳转到 AI 调度台后仍由用户输入并主动发送问题。Commander 只委派 `knowledge_agent.answer_question`，不扫描、猜测或合并资料库，不读取文件路径，也不申请文件、联网、Shell 或数据库权限；父任务仅记录脱敏状态、来源数量和关联子任务 artifact。`verify_commander_knowledge_route.py`、K3 可信问答回归、C0/C1 回归和 Qt Debug 构建均已通过。**

**2026-08-21 K3 质量扩展：**`verify_knowledge_retrieval_baseline.py` 已将固定资料集扩展为 7 份/11 题，运行时生成一份可提取文本 PDF、一份 DOCX 与跨父块长 Markdown，并走真实受控导入、分块、generation、FTS 与证据回读；关键词主路径必过召回 `8/8`、PDF 页码/DOCX 段落来源 `2/2`、平均 Recall@5/MRR `0.850`、无答案 `1/1`、中位/P95 约 `8/9ms`。跨文档冲突题保留为非必过诊断，当前单次关键词只命中直接包含“冲突”的材料，不会伪装成多文档覆盖已完成。专项混合 RRF/Chroma 假向量回归、K3 回答回归、C4 委派回归与 `.venv` 的 `pip check` 均已通过。系统 Python 缺少 `chromadb/fastembed`，不再用于后端验证；桌面端实际运行的 `backend/.venv` 依赖完整。**

**当前下一步为 K3 检索决策：**评测已覆盖短编号、长材料后段、PDF、DOCX 与跨文档诊断。下一轮先比较用户明确准备本机语义模型后的 Hybrid 召回，若跨文档覆盖仍持续不足，再评估最多一次确定性子查询；不把查询改写、HyDE、Cross-Encoder、OCR、多向量或 LLM 重排直接塞进默认链路。K4 深度任务仍在独立产品范围、评测和恢复契约确认后启动。

1. 已完成：DeepSeek、Kimi 的单文档真实 Tool Calling / JSON / 来源收束验收；模型输出无效恢复、超时、网络不可达和跨传输协议继续由离线故障注入固定覆盖。后续新增或切换 Provider 时运行 `scripts/verify_document_agent_llm.py`，不为制造真实超时而消耗模型额度。
2. 已完成方向确认与四项实现：文件转换与处理中的 PDF 整理基础版，智能文档/PPT 制作中的项目方案 PPT v1，以及文档审查中的项目文档审查 v1、论文审查 v1。PPT 仅从已核验草稿生成逐页计划，经确认导出可编辑 PPTX，回读验证并写入原任务历史；两类审查均只读受控材料，输出规则化报告与来源/范围锚点。
3. 已完成：文档助手 V1 已通过工程回归与本轮客户 Qt 验收；PPT 导出、历史产物、项目/论文审查入口、长报告、来源栏和窗口缩放的后续问题按真实反馈修正。旧摘要、问答、提取、大纲、多文档整合和 PDF 整理已降为内部兼容/历史回放能力，不应再出现在客户主选择区。
4. 数据工作台 D5.3 的画像路径与字段向导客户反馈已完成技术修正，仍等待客户在本机复验；D5.4 已完成单数据集只读 Commander 准入，不阻挡已批准的知识库 K0，也不会因 K0 自动扩大到写入型委派。
5. PPT V3 第三步已完成；第四步仅“原生转场”通过客户实际验收，正文按点击出现已暂停。Seedream 的 Provider 429 作为可降级外部依赖继续观察，不阻断文件交付；用户明确恢复前不继续消耗开发周期。
6. 外部 Runtime 当前步骤：Node Harness 的项目内锁定安装、无密钥探针、官方 profile 安全边界复核、fake Adapter 契约、最小权限 profile 回读和受控单任务 Bridge 已完成。H2 可另做隔离、无客户副作用的 Windows 只读真实试点；数据工作台 MVP 使用 Native Runtime，不为它提前实现 Router、H3 或 H4。

2026-07-31 UI 交付校正：文档助手主工作台已改为“选择当前材料 -> 制作项目方案 PPT / 项目文档审查 / 论文审查”；旧通用模式和 PDF 整理入口从客户页面隐藏，详细结果继续在独立详情或审查窗口阅读。`build/codex-debug` 已完成 UIC 与 C++ 构建，相关界面和文档均通过 UTF-8 检查。

总指挥已确认且协议已部分落地，后续只随真实专业 Agent 的能力扩大路由；通用代码工坊不再开发，命令治理作为平台能力保留。数据工作台已统一使用“数据工作台”导航、渐变背景与渐进披露的独立结果阅读：D1 受控导入/画像，D2 本地预览，D3-D4.3A 可恢复异步交付，D5.1 建议，D5.2 PNG 看板，D5.3 字段加工新副本、D5.4 总指挥单数据集只读分析，以及 R5.3 总指挥受控 PNG 图表交付均已实现。

**2026-08-31 R5.4A/B 数据交付闭环完成：**AI 调度台不再把“图表、Excel 或字段副本交付”推回数据工作台确认。客户已绑定 CSV/XLSX 后可依次说“分析当前数据”“生成图表/分析 Excel/新增字段”“开始执行”；Runtime 完成 PNG、原生 Excel 或字段加工副本回读验证和 artifact 审计后，会向同一会话持久化一次客户可读交付结论，列出已验证摘要与“源 CSV/XLSX 没有被修改”。任务历史仍保留完整产物与审计入口，但不再作为普通交付的强制跳转。`verify_commander_data_chart_delivery.py`、`verify_commander_data_workbook_delivery.py`、`verify_commander_data_transformation_delivery.py`、`verify_commander_data_delegate.py`、意图路由回归、Python 编译、Qt Debug 构建和 `ctest` 均通过，未调用真实模型、网络或客户文件。

## 2026-08-31 总指挥 R5.4C：两份数据关联首版

- 调度台现在能识别“按客户ID合并两份数据”等自然语言目标，但只接受客户明确绑定的两份 CSV/XLSX；唯一同名字段可自动作为关联键，多个候选键、无共同字段、类型不兼容和重复非空键会停止并给出可读原因。
- Runtime 先做只读关联预览，再在确认后由受控子任务生成 `output/data_joins/` 下的新同类型副本，支持 `left`/`inner`，CSV 使用 UTF-8 BOM，XLSX 使用无样式单表。输出列、行数、关联统计和两份源文件哈希均需回读验证，源文件不会被修改。
- 合并结果会以脱敏 artifact 和一次性会话交付写回调度台，任务历史保留子任务、关联键、源版本和打开入口；不会把绝对路径、原始行或完整数据发送给模型。首版不代表 2 至 4 份数据或任意多表 Join 已完成。
- 已通过 `verify_commander_data_join_delivery.py`、既有数据交付/字段加工/图表/路由回归、Python 编译和 `git diff --check`；本轮使用临时数据与 mock，不调用真实模型或网络。下一步转入 R5.4D 通用会话结果卡。

## 2026-08-31 总指挥 R5.4D：统一结果卡后端协议

- 已新增 `agentflow.delivery.v1` 结果卡和 `GET /api/tasks/{task_id}/delivery`。接口把任务状态、醒目结论、有限事实、警告、可打开/可预览产物和下一步动作聚合为一个稳定对象，调度台不必再自行拼接步骤、日志和工具调用。
- 结果卡只消费已落库事实，不重新读取源文件、不调用模型、不返回绝对路径、原始行、完整日志或模型内部信息；正文与产物摘要还会过滤内部任务标识和本机路径。
- 已通过 `verify_delivery_card.py`，验证结果卡版本、终态、产物能力标记和路径脱敏。Qt 结果卡渲染、图片缩略图、表格摘要以及知识库/文档专用事实仍是下一步，当前不宣称已完成。

## 常用验证命令

## 2026-08-27 调度台可靠性与体验整改计划（进行中）

- 已新增 [调度台可靠性、会话体验与模型控制整改计划](COMMANDER_RELIABILITY_CHAT_AND_MODEL_PLAN.md)。它汇总真实客户反馈中的文档列表超时、启动期提交、会话列表 `404`、聊天布局/Markdown 表格、`@` 中文乱码、材料绑定、会话标题与 Provider/模型显式选择问题。
- R0.1/R0.2/R0.3 已完成：会话列表 `404` 的根因是 Qt 把 query 拼入 URL path，现已改为 `QUrlQuery`；workspace 文档列表也已从“逐份解析 PDF/DOCX/OCR”改为纯元数据扫描，避免页面打开因大材料超时。启动完成、首次进入文档页、PDF 工作区和导入成功后的并发清单 GET 现会合并为一条，避免慢响应覆盖新状态或失败后停在加载中。后端健康检查另加入单飞行收敛、端口被其他 HTTP 服务占用时的脱敏解释，以及离线时可见的“重试后端”入口；重试只复查 app-owned 慢进程或回到安全探测链，不会停止手动后端，也不会占用同端口重复启动。新增 `verify_workspace_document_listing.py` 与回环 `BackendManagerTests`，并通过会话归档、全量后端和 Qt Debug 回归。
- R1 已补齐调度台 CSV/XLSX 的受控直达导入与 `@数据工作台` 材料绑定；R2 首轮已收束聊天区伸缩布局、右侧用户气泡与受限 Markdown 表格渲染。R3.1 已把“项目范围”改为不扩权的“会话空间”，并修复 `@` 中文 UTF-8 解码、首条消息短会话标题；R3.3 已为文档交付与数据工作台主状态行落入统一活动标志，基于既有后台状态旋转或停止，不增加请求。R4.1 已在 AI 调度台 Composer 增加“模型”入口并自动定位总指挥规划路由，R4.2 已把同一入口扩展到文档、数据和知识库的真实作用域。专业页不会启动预读路由；客户点击后才回读 Provider、模型和思考状态。R3 其余状态反馈、Provider 发现和模型枚举仍未完成。
- 本轮没有发起真实模型调用，也没有消耗 Provider 额度。

后端离线验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
python scripts\verify_backend.py
```

知识库 K5.1 缓存验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_knowledge_retrieval_cache.py
```

Node Harness H0 验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_node_harness_runtime.py
```

Node Harness H1 契约验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_node_harness_adapter.py
```

Node Harness H1 profile 预检：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_node_harness_profile.py
```

后端真实端口验证：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
$env:AGENTFLOW_CHAT_MODE='mock'
python -m uvicorn main:app --host 127.0.0.1 --port 8765
python scripts\verify_live_backend.py
```

Qt 命令行构建验证：

```powershell
cmd /c ""D:\IDE\VS2022\buildtools\VC\Auxiliary\Build\vcvars64.bat" && "D:\IDE\qtcreator\Tools\CMake_64\bin\cmake.exe" -S "D:\project\AgentFlow\AgentFlow" -B "D:\project\AgentFlow\AgentFlow\build\codex-debug" -G Ninja -DCMAKE_MAKE_PROGRAM="D:\IDE\qtcreator\Tools\Ninja\ninja.exe" -DCMAKE_PREFIX_PATH="D:\IDE\qtcreator\6.11.0\msvc2022_64" -DCMAKE_BUILD_TYPE=Debug && "D:\IDE\qtcreator\Tools\CMake_64\bin\cmake.exe" --build "D:\project\AgentFlow\AgentFlow\build\codex-debug" --target AgentFlow -j 4"
```

## 历史页样式记录

- 历史任务页已经恢复为和其他 tab 一致的 `heroCard` 渐变视觉，并在 `mainwindow.ui` 里给 `historyMainCard` 直接写入同款渐变，避免样式链没有吃到时退回白底。
- 历史任务页头部保留 `historyIcon` 图标和 `heroTitle` 标题层级，后续修改该页时不能再当作普通文本卡片处理。

## 注意事项

- `CMakeLists.txt` 已为 MSVC 添加 `/utf-8`，不要删除。
- `main.cpp` 已固定 `QApplication` 使用 Fusion 样式；不要在未验证启动/退出烟测前移除，否则 Qt 6.11 Debug 可能重新加载 `qmodernwindowsstyled.dll` 并触发初始化崩溃。
- `main.cpp` 当前有意不在 `app.exec()` 返回后强制析构主窗口对象；这是 Qt 6.11 + MSVC Debug 退出期稳定性的临时兼容处理，后续若改动必须重跑正常路径和后端路径无效路径的关闭烟测。
- 当前不要批量恢复运行时 `QGraphicsDropShadowEffect` 动态阴影；如需恢复，先保证退出期 CRT 检查不再报错。
- 文档以中文为主，英文仅保留必要技术名词。
- 新增代码要在关键异步流程、协议转换、安全边界处添加适量中文注释。
- `PowerShell Invoke-RestMethod` 有时会让中文显示/请求体编码异常；后端 UTF-8 检查优先使用 Python 验证脚本。
- 不要急着把核心 Runtime 迁到 LangChain / LangGraph；先把本地闭环做稳，并保留后续接入点。
- 不要急着移动 Qt 目录结构；等前端文件明显变多后再重构。
