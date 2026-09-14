# 模型配置与任务路由设计

最后更新：2026-09-14

## 目标

本轮依据 `Agent开发技术要点（持续更新）.md` 的“大模型参数设置”章节，修复以下真实问题：

- 全部任务共用固定温度，分析、抽取和创作无法采用不同策略。
- Provider 切换只保存当前全局模型，非当前厂商的模型、地址和参数无法独立维护。
- 模型名只能手填，设置页没有账号模型目录，也没有可靠的手动兜底。
- Seedream 只读取环境变量，PPT 生图使用的 Key、Base URL 和模型不在统一配置入口中。
- UI、配置文件、任务路由和实际请求之间缺少参数生效回归。

## 与技术资料的对照

| 技术资料要求 | AgentFlow 实现 | 状态 |
| --- | --- | --- |
| 按任务选择 `temperature` | 每个任务路由有推荐值，也允许用户覆盖 | 已实现 |
| 常用参数保持可理解 | Provider 配置与任务路由只向客户展示温度和最大输出；其余兼容字段仅保留在 Gateway 契约中 | 已实现 |
| 精确任务使用低随机性，创作任务适当提高 | 分析/问答默认 `0.2`，规划/深度任务 `0.3`，PPT 创作 `0.7` | 已实现 |
| 参数应结合模型能力 | Provider Profile 声明参数能力，不支持的参数在保存时拒绝、请求时不发送 | 已实现 |
| 模型可以按业务自由选择 | 关键 LLM 作用域可继承全局模型或显式指定 Provider/模型 | 已实现 |
| 图像模型也应可配置 | Seedream 作为独立 image Provider 接入配置、路由和脱敏状态 | 已实现 |
| 模型列表应便于选择 | 优先读取供应商模型目录，失败时显示维护候选，同时保留自由输入 | 已实现，目录能力取决于供应商 |

## 配置分层

### Provider 配置

每个 Provider 独立保存：

- Base URL
- 模型名
- Thinking 偏好
- `temperature`、`max_tokens`
- 该 Provider 自己的 DPAPI 加密 API Key

文本 Provider 可以被设为全局默认；图像 Provider 只更新自身配置，不会替换全局聊天模型。非敏感配置写入 `data/model_config.json` v3，Key 只保存 DPAPI 密文，API 和 Qt 均不回显。

Provider 专属环境变量始终优先归属于对应厂商；通用 `AGENTFLOW_LLM_*` 变量只归属于 `AGENTFLOW_LLM_PROVIDER` 指定的 Provider。用户切换查看或测试其他厂商时，Gateway 不会把当前全局 Key 带到另一个 Base URL。Seedream 的旧 `AGENTFLOW_SEEDREAM_BASE_URL/MODEL/API_KEY` 配置继续作为部署兜底。

### 任务路由

| 路由 | 用途 | 推荐温度 |
| --- | --- | ---: |
| `commander_planning` | 意图理解与计划 | 0.3 |
| `document_analysis` | 文档读取与结构化分析 | 0.2 |
| `document_presentation` | 文档和 PPT 创作 | 0.7 |
| `data_insight` | 确定性统计后的解释 | 0.2 |
| `knowledge_answer` | 有证据约束的知识库回答 | 0.2 |
| `knowledge_deep_analysis` | Map-Reduce 小结与归并 | 0.3 |
| `visual_generation` | PPT AI 图片生成 | 不适用 |

`commander_synthesis` 仍是预留路由，因为当前汇总使用确定性实现，不能把未发生的 LLM 调用伪装成可配置能力。

最终参数优先级为：

1. 当前任务路由的用户覆盖值。
2. 当前任务类型的推荐值。
3. 当前 Provider 保存的默认参数。
4. 部署环境默认值或供应商默认行为。

空值表示不在该层强制发送。连接测试固定使用低成本、短输出和温度 0；DeepSeek 原生搜索查询也保持温度 0。这两处是确定性基础设施探针，不属于业务内容生成。

## Provider 能力治理

Provider Profile 统一描述协议、模型种类、Thinking、JSON、Tool Calls 和生成参数能力。正常业务 UI 只暴露温度和最大输出：前者决定回答的发散程度，后者限制单次输出长度。`top_p`、存在惩罚和频率惩罚虽然仍被 Gateway 识别以兼容历史配置与供应商差异，但不适合 AgentFlow 的日常任务配置，界面不再提供入口。

- DeepSeek、OpenAI、Qwen：按各 Profile 发送已支持的温度和最大输出。
- Kimi：不发送当前适配未核验兼容的温度。
- Anthropic：为兼容新模型的参数约束，当前 Profile 不发送温度。
- Seedream：使用独立图像生成 Runtime，不接收文本采样参数。
- Custom OpenAI-compatible：保留模型名和 Base URL 自由输入，但能力仍以当前 Gateway 契约为边界。

显式任务路由若缺 Key、模型、地址或所需能力，会在保存或执行前失败，不会偷偷换回全局模型。

## 模型目录

`POST /api/models/catalog` 按 Provider 尝试读取账号可见模型。OpenAI-compatible Provider 使用 `/models`，Anthropic 使用其模型目录端点。目录不可访问、无 Key 或供应商未提供兼容目录时，返回内置候选并保留可编辑下拉框；候选列表只是便捷入口，不冒充账号实时可用性。

## 主要接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/models/providers` | Provider 能力、独立配置和脱敏 Key 状态 |
| `GET /api/models/config` | 当前全局文本 Runtime |
| `PUT /api/models/config` | 保存某个 Provider 的模型、参数和 Key |
| `POST /api/models/catalog` | 刷新账号模型目录或取得候选列表 |
| `POST /api/models/test` | 使用当前表单做一次低成本连接测试 |
| `GET /api/models/routes` | 查看关键业务作用域的实际解析结果 |
| `PUT /api/models/routes/{route_id}` | 保存任务级模型和参数覆盖 |

任务审计只记录实际 Provider、模型、Thinking 和最终发送参数，不记录 Key、完整 Base URL、提示词或响应正文。

## 验收

离线专项脚本 `backend/scripts/verify_model_configuration.py` 覆盖：

- v2 配置读取和下一次保存迁移至 v3。
- DeepSeek 与 Seedream 配置独立保存，图像配置不替换默认文本模型。
- 三类任务推荐温度和用户覆盖值进入最终请求载荷。
- Kimi 不兼容参数被 API 拒绝，Gateway 不发送相关字段。
- Seedream 路由使用用户保存的 Base URL 和模型。
- Provider 状态能识别 DPAPI 或环境变量 Key，通用 Key 不跨 Provider 复用，响应和配置文件不出现明文 Key。
- 模型目录不可用时安全回退到候选或自由输入。

本轮已通过专项模型配置、C6.5 模型路由、全量后端和 PPT 工作室离线回归，以及 Qt Debug/Release 构建与 CTest。未调用真实供应商目录、聊天或图像生成，因此账号模型列表和每个真实模型的参数兼容性仍应以用户主动点击刷新/连接测试及后续受控真实验收为准。
