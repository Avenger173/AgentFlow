# AgentFlow

> 面向 Windows 的本地优先 Agent 桌面工作台：让 AI 不只回答问题，还能规划任务、调用专业 Agent、交付文件，并保留可审计的执行过程。

![Platform](https://img.shields.io/badge/platform-Windows-0078D4)
![Desktop](https://img.shields.io/badge/desktop-Qt%206-41CD52)
![Backend](https://img.shields.io/badge/backend-FastAPI-009688)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![Status](https://img.shields.io/badge/status-active%20development-F59E0B)

AgentFlow 采用 **C++ / Qt 桌面端 + Python / FastAPI 本地后端**。用户可以从一个桌面应用中完成任务编排、文档与 PPT 制作、数据分析、本地知识库问答、任务追踪和长期记忆管理。

项目强调三个原则：

- **结果可交付**：任务需要产出可使用的文档、表格、图表、PPTX 或明确状态，而不只是聊天文本。
- **过程可验证**：计划、权限、工具调用、来源、任务状态和产物都可以回看。
- **边界可控制**：文件读取、联网、模型调用和写入动作均经过范围校验，敏感操作需要用户确认。

> [!IMPORTANT]
> AgentFlow 目前处于持续开发阶段，尚未提供稳定的公开安装包。当前推荐在 Windows 开发环境中从源码运行。

## 功能界面展示

总览：
<img src="docs/images/readme/image-20260914160735088.png" alt="image-20260914160735088" style="zoom:67%;" />

模型自由组合搭配：

<img src="docs/images/readme/image-20260914161020484.png" alt="image-20260914161020484" style="zoom:67%;" />
AI调度台总览：

<img src="docs/images/readme/image-20260914150653796.png" alt="image-20260914150653796" style="zoom: 67%;" />

> **AI 调度台总览**
>
> 展示自然语言任务输入、材料绑定、项目范围、工作流计划和实时执行状态。

### AI 调度与任务执行

Commander 将用户目标转换为结构化计划，只调度已经注册且具备运行条件的专业 Agent。任务开始后，界面持续展示步骤、状态、权限请求、工具调用和最终产物。

<img src="docs/images/readme/image-20260914150918963.png" alt="image-20260914150918963" style="zoom: 67%;" />

> **工作流执行过程**
>
> 展示计划审阅、执行进度、暂停/继续/取消、权限确认和交付结果。

### 文档与 PPT 制作

文档助手支持受控导入 TXT、Markdown、PDF 和 DOCX，能够进行来源追踪的理解、审查、草稿创作与版本处理。PPT 工作台提供创作简报、视觉方向、逐页计划和确认导出，可生成可编辑 PPTX；只有通过数据合同与来源校验的数值才会进入原生表格和图表。

文档助手总览：

<img src="docs/images/readme/image-20260914151021932.png" alt="image-20260914151021932" style="zoom: 67%;" />

PPT智能制作总览：
<img src="docs/images/readme/image-20260914151100736.png" alt="image-20260914151100736" style="zoom: 67%;" /><img src="docs/images/readme/image-20260914151606361.png" alt="image-20260914151606361" style="zoom:67%;" /><img src="docs/images/readme/image-20260914151922958.png" alt="image-20260914151922958" style="zoom:67%;" />

> **PPT 创作工作台**
>
> 展示创作计划、页面结构、视觉配置、数据视图和 PPTX 导出结果。

### 数据工作台

数据工作台面向 CSV/XLSX 的本地分析流程，支持数据画像、字段检查、白名单聚合、图表生成、字段加工和新工作簿交付。所有变换先预览，确认后生成新文件，不覆盖原始数据。

数据工作台界面总览：
<img src="docs/images/readme/image-20260914152359905.png" alt="image-20260914152359905" style="zoom:67%;" />
数据工作台分析csv文件任务：
<img src="docs/images/readme/image-20260914152602804.png" alt="image-20260914152602804" style="zoom:67%;" />
<img src="docs/images/readme/image-20260914152631049.png" alt="image-20260914152631049" style="zoom:67%;" />
<img src="docs/images/readme/image-20260914152715594.png" alt="image-20260914152715594" style="zoom:67%;" />
<img src="docs/images/readme/image-20260914152856677.png" alt="image-20260914152856677" style="zoom:67%;" />

> **数据工作台**
>
> 展示数据概览、分析结论、图表看板、字段加工预览和 Excel 交付。

### 本地知识库

知识库提供受控副本、文档版本、关键词与可选语义索引、来源约束问答和可恢复的深度分析任务。回答必须绑定当前有效版本和来源位置，资料更新后旧索引不会继续冒充最新证据。

知识库界面总览：
<img src="docs/images/readme/image-20260914153006671.png" alt="image-20260914153006671" style="zoom:67%;" />
RAG资料库提问：
<img src="docs/images/readme/image-20260914153322570.png" alt="image-20260914153322570" style="zoom:67%;" />
AI调度台总指挥同样可以使用：
<img src="docs/images/readme/image-20260914153806860.png" alt="image-20260914153806860" style="zoom:67%;" />

> **本地知识库**
>
> 展示资料库、索引状态、可信问答、来源侧栏和深度任务进度。

### 任务历史与长期记忆

任务历史保留计划、步骤、事件、权限、工具调用、指标和产物。短期记忆负责同一会话的连续上下文，长期记忆保存用户确认过的偏好、项目约束和可复用经验；用户可以查看、编辑、关闭或删除这些记录。

任务历史界面：
<img src="docs/images/readme/image-20260914153859201.png" alt="image-20260914153859201" style="zoom:67%;" />
系统具有长期记忆能力：
<img src="docs/images/readme/image-20260914153949685.png" alt="image-20260914153949685" style="zoom:67%;" />

> **任务历史与长期记忆**
>
> 展示任务事件与产物追踪，以及长期记忆的候选确认和管理界面。

## 核心能力

| 模块 | 当前能力 |
| --- | --- |
| AI 调度台 | 自然语言任务入口、结构化规划、Agent 路由、材料绑定、项目范围和交付汇总 |
| Workflow Runtime | Dry-run、后台执行、状态机、检查点、暂停/继续/取消、失败恢复和 WebSocket 事件 |
| 文档助手 | TXT/Markdown/PDF/DOCX 解析、来源追踪、文档审查、草稿与版本处理、PPTX 交付 |
| 数据工作台 | CSV/XLSX 导入、数据画像、聚合分析、PNG 图表、字段加工和可编辑 Excel 交付 |
| 本地知识库 | 文档版本、FTS5、可选 Chroma/FastEmbed、可信问答、深度 Map-Reduce 和可选本地 OCR |
| 记忆系统 | 会话归档、有限近轮上下文、Compaction、跨会话短事实记忆和显式确认 |
| 模型网关 | DeepSeek、Kimi/Moonshot、OpenAI、Anthropic、Qwen 和自定义 OpenAI-compatible 服务 |
| 治理与审计 | 权限确认、作用域隔离、Tool 审计、任务历史、来源验证、产物回读和敏感信息过滤 |

## 工作方式

```mermaid
flowchart LR
    UI[Qt Desktop] --> API[FastAPI API]
    API --> Commander[Commander]
    Commander --> Registry[Agent Registry]
    Registry --> Document[Document Agent]
    Registry --> Data[Data Agent]
    Registry --> Knowledge[Knowledge Agent]
    Document --> Tools[Tool Registry]
    Data --> Tools
    Knowledge --> Tools
    Commander --> Runtime[Workflow Runtime]
    Runtime --> Audit[Tasks / Events / Artifacts]
    API --> Memory[Session & Long-term Memory]
    API --> Gateway[ModelGateway]
    Gateway --> Providers[LLM Providers]
    Audit --> SQLite[(SQLite)]
    Memory --> SQLite
```

一次典型任务会经过以下步骤：

1. 用户输入目标，并按需绑定文档、数据集或知识库。
2. Commander 结合可用 Agent、材料范围、会话状态和已确认记忆生成结构化计划。
3. Runtime 校验动作、权限和工作区边界；需要确认时暂停等待用户决定。
4. 专业 Agent 通过受控 Tool 读取材料、调用模型或生成交付物。
5. Verifier 回读文件和结构化结果，确认交付内容与任务声明一致。
6. Qt 展示最终结果，任务历史保存可恢复、可审计的执行记录。

## 技术特点

### 可审计的 Agent Runtime

AgentFlow 将“模型建议”和“系统执行”分开。模型可以参与理解与规划，但最终可执行动作必须通过 Agent Registry、Action Admission、权限策略和 Runtime 状态机。

### 分层记忆

- 短期记忆使用 `conversation_id`、最近消息、确定性摘要和 Working State 保持当前任务连续性。
- 长期记忆使用 SQLite 保存用户确认的短事实，并通过作用域过滤后的 FTS5/BM25 最多召回三条。
- 完整对话归档、长期偏好和任务执行状态分别存储，避免把全部历史无界塞入模型上下文。

### 可信检索

知识库将文档版本、索引 generation、父子分块和来源锚点绑定在一起。关键词检索默认可用；用户明确准备本地 Embedding 后，可增加 Chroma/FastEmbed 语义检索。回答生成前后都会校验证据范围。

### 可替换模型网关

所有模型调用统一经过 `ModelGateway`，Agent 与 Tool 不直接依赖具体厂商 SDK。不同 Provider 的鉴权、模型参数、Thinking 模式、错误和 usage 在网关层归一化。每个 Provider 可独立保存模型与参数，关键任务还可覆盖自己的模型路由；Seedream 作为图像 Provider 使用同一配置入口，但不会替换默认聊天模型。

### 交付物回读验证

PPTX、XLSX、Markdown、PDF 和图表文件写入受控输出目录后，会按对应格式重新打开并检查关键结构。任务只有在文件真实存在且验证通过后，才会登记成功产物。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 桌面端 | C++、Qt 6 Widgets、QNetworkAccessManager、QWebSocket、QProcess |
| 构建 | CMake 3.19+、MSVC 2022 / Ninja |
| 后端 | Python 3.11+、FastAPI、Uvicorn、Pydantic、asyncio |
| 持久化 | SQLite、WAL、FTS5 |
| 数据处理 | pandas、openpyxl、matplotlib |
| 文档与交付 | PyMuPDF、python-docx、python-pptx、Pillow |
| 知识检索 | SQLite FTS5、Chroma、FastEmbed |
| 安全 | Windows DPAPI、作用域校验、显式确认、脱敏审计 |

## 快速开始

### 环境要求

- Windows 10/11 x64
- Visual Studio 2022 C++ Build Tools
- Qt 6.5 或更高版本的 MSVC x64 Kit
- CMake 3.19+
- Python 3.11+

### 1. 获取代码

```powershell
git clone https://github.com/Avenger173/AgentFlow.git
cd AgentFlow
```

### 2. 准备后端环境

```powershell
py -3.11 -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install --upgrade pip
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
```

### 3. 配置模型

推荐在桌面端的“模型服务”页面选择 Provider、刷新账号可见模型或直接输入模型名，并按需设置温度和最大输出。Key 会使用 Windows DPAPI 加密保存在本机，接口不会回显明文。需要不同任务使用不同模型或参数时，打开“任务模型路由”；PPT 生图使用的 Seedream 也在本页独立配置。

也可以使用环境变量或 `backend/.env`：

```powershell
Copy-Item backend\.env.example backend\.env
```

最小配置示例：

```dotenv
AGENTFLOW_CHAT_MODE=llm
AGENTFLOW_LLM_PROVIDER=deepseek
AGENTFLOW_LLM_BASE_URL=https://api.deepseek.com
AGENTFLOW_LLM_MODEL=deepseek-v4-flash
AGENTFLOW_LLM_API_KEY=replace_me
```

### 4. 构建并运行桌面端

推荐使用 Qt Creator 打开根目录的 `CMakeLists.txt`，选择 Qt 6 MSVC x64 Kit 后构建并运行。开发版桌面端会自动探测并启动 `backend/.venv` 中的本地 FastAPI 后端。

也可以使用 Visual Studio 2022 生成器构建；请将 Qt 路径替换为本机实际安装位置：

```powershell
cmake -S . -B build\dev -G "Visual Studio 17 2022" -A x64 -DCMAKE_PREFIX_PATH="C:\Qt\6.8.3\msvc2022_64"
cmake --build build\dev --config Debug --parallel
.\build\dev\Debug\AgentFlow.exe
```

如需单独启动后端：

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8765 --reload
```

启动后可访问 `http://127.0.0.1:8765/health` 检查后端状态。

## 配置说明

| 配置 | 作用 | 默认值 |
| --- | --- | --- |
| `AGENTFLOW_CHAT_MODE` | `llm` 使用真实模型，`mock` 用于离线开发 | `mock` |
| `AGENTFLOW_LLM_PROVIDER` | 模型供应商 Profile | `mock` |
| `AGENTFLOW_LLM_BASE_URL` | 模型 API 地址 | Provider 默认地址 |
| `AGENTFLOW_LLM_MODEL` | 默认模型名称 | 随 Profile 解析 |
| `AGENTFLOW_LLM_API_KEY` | 模型 API Key | 未设置 |
| `AGENTFLOW_DATABASE_PATH` | SQLite 数据库位置 | `data/agentflow.db` |
| `AGENTFLOW_DATA_DIR` | 本地状态和受控数据目录 | `data/` |

Pexels、Seedream、Tavily、OCR 和本地 Embedding 都是可选能力，未配置时应明确降级或停止相关交付，不影响基础聊天与本地任务管理。完整示例见 [`backend/.env.example`](backend/.env.example)。

## 项目结构

```text
AgentFlow/
├─ CMakeLists.txt              # Qt 桌面端构建入口
├─ mainwindow.*                # 主窗口与工作台交互
├─ backendclient.*             # Qt 与本地 API/WebSocket 通信
├─ backendmanager.*            # 本地后端生命周期管理
├─ backend/
│  ├─ main.py                  # FastAPI 入口
│  ├─ app/                     # API、Agent、Runtime、Tool、Repository
│  ├─ scripts/                 # 离线回归与验收脚本
│  └─ requirements*.txt
├─ agents/                     # 用户 Agent manifest 预留目录
├─ docs/                       # 产品规格、架构决策、计划与验收记录
├─ icons/                      # Qt 资源
├─ packaging/                  # Windows 目录发行工程
└─ output/                     # 本地生成的受控交付物
```

## 验证

运行后端基础回归：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\verify_backend.py
```

运行记忆系统专项回归：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\verify_commander_memory_lifecycle.py
backend\.venv\Scripts\python.exe backend\scripts\verify_long_term_memory_retrieval.py
```

运行 Qt 构建与测试：

```powershell
cmake --build build\dev --config Debug --parallel
ctest --test-dir build\dev -C Debug --output-on-failure
```

更多专项脚本位于 [`backend/scripts/`](backend/scripts/)。涉及真实模型、联网搜索或本地 OCR 的验收不会默认运行，以免未经确认消耗额度、下载模型或读取材料。

## 当前状态

| 状态 | 内容 |
| --- | --- |
| 已形成闭环 | Commander 调度、文档助手、数据工作台、本地知识库、任务历史、权限审计、分层记忆、多模型网关 |
| 可选能力 | 本地语义索引、本地 OCR、Pexels 图片、Seedream 图片生成、联网资料核验 |
| 正在完善 | Windows 目录式发行、真实材料体验验收、长期记忆命中可视化和用户画像聚合 |
| 尚未作为正式能力 | 通用 Code Agent、第三方 Agent 插件市场、多用户/多租户、跨平台桌面发行 |

项目状态以 [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) 为准；开发计划与未完成边界以 [`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md) 为准。README 只描述当前对外可理解的产品能力，不承担开发流水账职责。

## 文档导航

- [当前项目状态](docs/PROJECT_STATUS.md)
- [开发路线与阶段门禁](docs/DEVELOPMENT_ROADMAP.md)
- [Agent 与 Workflow 规格](docs/AGENT_SPECIFICATIONS.md)
- [Agent 工程指南](docs/AGENT_ENGINEERING_GUIDE.md)
- [记忆系统开发计划](docs/AGENT_MEMORY_DEVELOPMENT_PLAN.md)
- [多媒体助手开发计划（规划中）](docs/MULTIMEDIA_AGENT_DEVELOPMENT_PLAN.md)
- [多媒体助手评测与验证方案](docs/MULTIMEDIA_AGENT_EVALUATION.md)
- [数据工作台产品规格](docs/DATA_WORKSPACE_PRODUCT_SPEC.md)
- [知识库产品规格](docs/KNOWLEDGE_BASE_PRODUCT_SPEC.md)
- [PPT 数据与研究设计](docs/PPT_RESEARCH_DATA_DESIGN.md)
- [LangGraph、LangChain 与 MCP 集成计划](docs/LANGGRAPH_LANGCHAIN_MCP_INTEGRATION_PLAN.md)
- [Windows 目录发行计划](docs/LGM7_RELEASE_ENGINEERING_PLAN.md)

## 开发约束

- 模型输出不是执行权限，所有真实动作必须经过 Runtime 和 Tool 边界。
- 新功能必须给出真实使用场景、失败边界、验收标准和可回退路径。
- 文件交付默认新建，不覆盖用户原文件；成功状态必须来自回读验证。
- 不把完整对话、原始材料、密钥或绝对路径静默写入长期记忆。
- 不把尚未通过真实验收的试验能力描述为已经稳定交付。

欢迎通过 Issues 提交问题、真实使用反馈和可复现案例。进行较大改动前，请先阅读 [`SKILL.md`](SKILL.md) 以及对应模块的产品规格。
