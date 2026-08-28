# AgentFlow 初版规划与对话总结

> 用途：这份文档用于给 Codex / Claude Code / 其他 AI 编程助手作为项目背景，让其理解 AgentFlow 的产品定位、技术架构、模块边界、开发优先级和长期商业化方向。  
> 项目当前定位：一个基于 **C++ Qt 桌面端 + Python FastAPI 后端 + 多 Agent 工作流编排 + 插件化 Agent 生态** 的 AI 工作流系统软件。  
> 目标：既能作为简历项目，也能自己长期使用，后续具备商业化、插件市场、定制 Agent、私有化部署的潜力。

> 2026-07-10 执行校准：本文保留长期愿景和最初讨论，不再作为当前进度或短期实现细节的唯一依据。当前阶段以 `docs/PROJECT_STATUS.md` 为准，交付顺序以 `docs/DEVELOPMENT_ROADMAP.md` 为准，Agent 工程边界以 `docs/AGENT_ENGINEERING_GUIDE.md` 和 `docs/AGENT_SPECIFICATIONS.md` 为准。阶段 5 先完成一个经确认的内置 Agent 纵向闭环；确定性单步能力归 Tool，只有工具面、权限、模型策略、职责所有权或输出契约明显不同才拆成新 Agent。LangGraph / LangChain、动态插件入口、PDF/Word/Excel、RAG 和任意 Shell 都不是首个正式 Agent 的前置条件。

---

# 1. 对话总体总结

我们围绕“从 0 开始做一个自己的 AI Agent 全能工具软件”进行了多轮讨论，逐步从一个普通 AI Agent 工具想法，演进成一个更完整、更工程化、更产品化的桌面端多 Agent 工作流平台。

最初的问题是：从 0 开始做一个自己的 AI Agent 全能工具难不难，需要做哪些技术。讨论后明确了一个核心观点：

**不需要自己训练大模型，真正要做的是一个能调用大模型、工具、文件、知识库、数据库、执行环境和多个专业 Agent 的工程系统。**

随后讨论了前端是否可以用 C++ Qt。结论是：

**完全可以。C++ Qt 很适合做桌面端高级 UI；Python 更适合做 AI Agent 后端核心。**

推荐架构为：

```text
C++ Qt 桌面客户端
    ↓ HTTP / WebSocket
Python FastAPI 后端服务
    ↓
Agent 编排核心
    ↓
LLM API / 本地模型 / 工具系统 / 数据库 / RAG / 文件处理
```

之后，用户提出更明确的想法：希望做一个“总 AI 指挥 + 很多各司其职的小 AI Agent”的系统。我们确认这本质上是：

```text
Multi-Agent Workflow System
多智能体工作流编排系统
```

其核心思想为：

```text
总指挥 Agent = 项目经理 / 调度中心
专业小 Agent = 不同领域的专家
工具 Tools = 各 Agent 可调用的具体能力
Workflow Engine = 工作流执行器
Shared Context = 多 Agent 共享上下文
```

后续又进一步扩展成更商业化的设想：

- 每个小 Agent 像独立软件一样，有自己的图标和页面。
- 小 Agent 可以单独使用，也可以被总指挥 Agent 调用。
- 每个 Agent 都应拥有自己的 LLM、Prompt、工具、记忆、知识库和配置。
- 总软件可以随时添加、删除、启用、禁用小 Agent。
- 后续可以做 Agent 插件市场。
- 用户可以下载 `.afagent` 插件包安装到 AgentFlow 中。
- 客户也可以联系开发者定制专属 Agent。
- 软件商业化时，用户可以自备多模态 LLM API Key。
- 每个 Agent 也可以设置不同模型和不同 API Key，以控制成本和能力。
- 软件最终可以做成 Windows 安装包，用户双击 Qt 前端后，Python 后端在后台自动启动并协作运行。

最终形成的产品定位是：

```text
AgentFlow = AI Agent 桌面操作系统 + 多 Agent 工作流平台 + Agent 插件生态
```

---

# 2. 项目名称与定位

## 2.1 推荐项目名

```text
AgentFlow
```

可选中文名：

```text
AgentFlow 多智能体 AI 工作流平台
AgentFlow AI Agent 桌面工作台
AgentFlow 多 Agent 插件化自动化系统
```

## 2.2 一句话定位

```text
AgentFlow 是一个基于 C++ Qt + Python FastAPI + LangGraph + RAG 的多智能体 AI 工作流编排桌面平台，支持总指挥 Agent 调度多个专业 Agent 协同完成文档、代码、数据、图像、视频、报告等复杂任务，并支持 Agent 插件化安装、卸载和定制扩展。
```

## 2.3 产品定位

AgentFlow 不是一个普通聊天软件，也不是简单的 ChatGPT 套壳，而是：

```text
一个可以安装多个 AI Agent 插件的桌面端 AI 工作流平台。
```

每个 Agent 可以理解为一个独立领域的软件 App，例如：

```text
文档 Agent：处理 PDF、Word、Markdown、TXT
代码 Agent：生成代码、解释代码、修复代码、运行代码
数据 Agent：分析 Excel、CSV，生成图表和报告
视觉 Agent：处理图片、视频，调用 YOLO、OpenCV、ONNX
报告 Agent：生成 Markdown、Word、PDF 报告
RAG Agent：管理本地知识库和检索增强问答
部署 Agent：生成 Linux、Docker、systemd 部署方案
```

---

# 3. 用户当前技术栈背景

用户目前掌握或了解的技术如下，这些技术可以尽量融入项目，但不能为了堆技术而乱用，应做到每个技术都有明确作用。

## 3.1 编程语言

```text
C++
Python
Golang
Rust
Java（了解）
```

## 3.2 开发框架 / 库

```text
Qt(C++)
OpenCV4.x
FFmpeg7.1.1
FastAPI
AI Agent
YOLO
ONNX Runtime
OpenGL
LangChain
LangGraph
RAG
PyTorch
深度学习模型训练与推理
HTML / CSS / JavaScript / Vue（了解）
```

## 3.3 数据库与中间件

```text
MySQL
SQLite
PostgreSQL
Redis
```

## 3.4 工具与环境

```text
Git
GitHub
Linux 常用命令
CMake 项目构建
systemd 服务部署
Docker
```

## 3.5 网络与协议

```text
WebSocket
RTMP
实时音视频系统中客户端、信令、媒体、存储之间的协作链路
```

## 3.6 并发与工程

```text
多线程并发
异步事件驱动
资源回收
配置化部署
云端联调
问题定位
```

## 3.7 专业领域

```text
数字图像处理
音视频编解码开发
实时音视频会议系统端到端架构
深度学习模型推理与工程化落地
人像分割
目标检测
视觉模型部署
AI 功能集成
AI Agent 应用开发
RAG 检索增强应用开发
海康相机 SDK 二次开发与行业应用对接
```

## 3.8 项目差异化优势

普通 AI Agent 项目多数只做：

```text
聊天 + RAG + LangChain
```

AgentFlow 可以突出：

```text
C++ Qt 桌面端
多 Agent 插件系统
LangGraph 工作流编排
RAG 私有知识库
OpenCV / FFmpeg / YOLO / ONNX 多模态视觉能力
本地化 / 私有化 / 用户自备 Key
Agent 插件市场与定制 Agent 商业模式
```

---

# 4. 总体系统架构

## 4.1 高层架构

```text
┌────────────────────────────────────────────┐
│              AgentFlow Qt Client            │
│  Agent 桌面 / 总指挥页面 / 插件管理 / 设置   │
└──────────────────────┬─────────────────────┘
                       │ HTTP / WebSocket
                       ↓
┌────────────────────────────────────────────┐
│           Local Python FastAPI Backend      │
│  API 服务 / WebSocket / Agent Runtime       │
└──────────────────────┬─────────────────────┘
                       ↓
┌────────────────────────────────────────────┐
│             Agent Orchestration Layer       │
│ Commander / Workflow Engine / LangGraph     │
└──────────────────────┬─────────────────────┘
                       ↓
┌────────────────────────────────────────────┐
│              Agent Plugin Layer             │
│ Document / Code / Data / Vision / Report    │
└──────────────────────┬─────────────────────┘
                       ↓
┌────────────────────────────────────────────┐
│               Capability Layer              │
│ LLM / RAG / Tools / MCP / DB / Sandbox       │
└────────────────────────────────────────────┘
```

## 4.2 分层说明

### 4.2.1 UI 表现层

技术：

```text
C++ Qt 6
QML 或 Qt Widgets + QSS
QNetworkAccessManager
QWebSocket
QProcess
```

职责：

```text
聊天窗口
Agent 图标桌面
单 Agent 独立页面
总指挥任务页面
工作流可视化
文件拖拽上传
插件管理
模型/API Key 设置
执行日志展示
结果文件管理
```

### 4.2.2 通信层

技术：

```text
HTTP
WebSocket
JSON
SSE 可选
```

职责：

```text
Qt 前端向 Python 后端发送任务
上传文件
获取 Agent 列表
获取插件状态
实时显示执行日志
实时显示流式输出
展示任务状态变化
```

### 4.2.3 后端服务层

技术：

```text
Python 3.11+
FastAPI
Uvicorn
Pydantic
SQLAlchemy
asyncio
```

职责：

```text
提供 HTTP API
提供 WebSocket 实时通道
管理任务生命周期
管理插件安装/卸载
管理 Agent Runtime
管理 RAG 服务
管理本地存储
管理模型供应商配置
```

### 4.2.4 Agent 编排层

技术：

```text
LangGraph
LangChain
自研 Workflow Engine
自研 Agent Registry
自研 Tool Registry
```

职责：

```text
总指挥 Agent 规划任务
根据任务生成 JSON DAG
多 Agent 串行/并行执行
失败重试
状态持久化
Human-in-the-loop 人工确认
结果评估
最终汇总
```

### 4.2.5 能力层

技术：

```text
OpenAI API / DeepSeek API / 通义千问 / 智谱 / Claude / Gemini / Ollama
RAG
Chroma / pgvector / Milvus
PyMuPDF
python-docx
openpyxl
pandas
matplotlib
ReportLab
OpenCV
FFmpeg
YOLO
ONNX Runtime
PyTorch
Docker Sandbox
MCP
```

职责：

```text
模型调用
文件读取
数据分析
图像处理
视频处理
知识库检索
代码执行
命令执行
报告生成
插件工具调用
```

---

# 5. 单 Agent 与多 Agent 的区别

## 5.1 单 Agent

单 Agent 类似一个“全科医生”。

架构：

```text
用户任务
 ↓
单个 Agent 判断
 ↓
单个 Agent 调用所有工具
 ↓
单个 Agent 返回结果
```

优点：

```text
结构简单
开发快
Token 消耗少
调试容易
适合小任务
```

缺点：

```text
Prompt 越来越长
工具太多容易选错
职责不清
复杂任务容易混乱
上下文容易膨胀
扩展性差
```

## 5.2 多 Agent

多 Agent 类似一个“公司团队”。

```text
总指挥 Agent = 项目经理
文档 Agent = 文档专家
代码 Agent = 程序员
数据 Agent = 数据分析师
视觉 Agent = 图像/视频专家
报告 Agent = 文档输出员
Evaluator Agent = 质量检查员
```

优点：

```text
职责清晰
每个 Agent 更专业
每个 Agent Prompt 更短
每个 Agent 工具权限独立
更适合复杂任务
更适合工作流可视化
更适合插件化商业模式
可以并行执行
```

缺点：

```text
系统复杂度更高
Token 消耗更多
调度逻辑更难
需要状态管理
Agent 结果可能冲突
需要总指挥做仲裁和汇总
```

## 5.3 本项目选择

AgentFlow 选择多 Agent 架构。

原因：

```text
1. 更适合商业化软件形态
2. 更适合插件市场和定制 Agent
3. 更能体现工程架构能力
4. 更适合简历项目展示
5. 更适合后续扩展视觉、音视频、部署等专业能力
```

---

# 6. Agent、Tool、Plugin 的定义

## 6.1 Tool

Tool 是一个具体、确定性的能力。

例如：

```text
read_pdf
read_docx
read_excel
run_python
generate_docx
detect_object
extract_video_frames
query_database
```

特点：

```text
通常不需要 LLM
输入明确
输出明确
功能单一
可被 Agent 调用
```

## 6.2 Agent

Agent 是一个具备智能决策能力的领域专家。

一个 Agent 应包含：

```text
LLM 配置
角色 Prompt
工具集
记忆
知识库
权限配置
UI 页面
执行接口
能力描述
```

## 6.3 Plugin Agent

Plugin Agent 是可以被安装、卸载、启用、禁用的 Agent 插件包。

特点：

```text
有独立图标
有独立页面
可以单独使用
可以被总指挥调用
可以参与多 Agent 工作流
可以单独配置模型/API Key
可以独立更新
可以作为商业插件销售
```

---

# 7. 推荐核心 Agent 设计

## 7.1 Commander Agent 总指挥

职责：

```text
理解用户任务
判断任务类型
拆解复杂任务
选择需要哪些 Agent
生成 JSON DAG 工作流计划
校验任务计划
分派任务
监控执行过程
处理失败重试
汇总最终结果
调用 Evaluator 评估结果
```

总指挥不是一个简单 Prompt，而应拆成多个内部模块：

```text
Commander Agent
├─ Intent Classifier      意图识别
├─ Task Planner           任务规划
├─ Agent Router           Agent 路由
├─ Tool Policy Checker    工具权限检查
├─ Workflow Builder       工作流生成
├─ Result Evaluator       结果评估
└─ Final Summarizer       最终汇总
```

## 7.2 Document Agent 文档精英

职责：

```text
读取 PDF
读取 Word
读取 TXT
读取 Markdown
提取文档正文
提取表格
总结文档
提取任务要求
生成文档摘要
```

工具：

```text
PyMuPDF
python-docx
markdown
OCR 可选
pandas
```

## 7.3 Code Agent 代码精英

职责：

```text
生成代码
解释代码
修复代码
生成项目结构
生成 README
运行 Python 代码
生成部署命令
分析报错
```

工具：

```text
文件写入
代码模板
Python Runner
Docker Sandbox
Git 工具
项目打包工具
```

## 7.4 Data Agent 数据精英

职责：

```text
读取 Excel / CSV
统计分析
清洗数据
生成图表
生成数据结论
导出分析结果
```

工具：

```text
pandas
openpyxl
matplotlib
numpy
```

## 7.5 RAG Agent 知识库精英

职责：

```text
管理知识库
上传文档入库
文档切分
向量化
检索相关内容
基于资料回答问题
提供引用来源
```

工具：

```text
LangChain Document Loader
Text Splitter
Embedding Model
Chroma / pgvector
Retriever
```

## 7.6 Report Agent 报告精英

职责：

```text
生成 Markdown
生成 Word
生成 PDF
整理最终报告
生成任务总结
生成项目说明文档
```

工具：

```text
python-docx
ReportLab
Markdown
Jinja2 模板
```

## 7.7 Vision Agent 视觉精英

职责：

```text
图片目标检测
图片分割
图像增强
视频抽帧
视频目标检测
检测结果可视化
生成视觉分析报告
```

工具：

```text
OpenCV
YOLO
ONNX Runtime
PyTorch
FFmpeg
```

这是本项目差异化亮点之一。

## 7.8 Media Agent 音视频精英

职责：

```text
音视频转码
抽取音频
视频切片
视频压缩
生成缩略图
提取关键帧
音视频基本信息分析
```

工具：

```text
FFmpeg
ffprobe
OpenCV
```

## 7.9 Deploy Agent 部署精英

职责：

```text
生成 Linux 部署方案
生成 Dockerfile
生成 docker-compose.yml
生成 systemd 服务文件
分析部署报错
生成 Nginx 配置
```

工具：

```text
Docker
Linux 命令模板
systemd 模板
Nginx 模板
```

## 7.10 Evaluator Agent 质量评估精英

职责：

```text
检查任务是否完成
检查代码是否可运行
检查报告是否完整
检查是否缺少用户要求
给结果打分
提出改进建议
```

---

# 8. 多 Agent 是否并行执行

多 Agent 可以并行执行，但不是所有任务都适合并行。

## 8.1 可并行任务

例如：

```text
分析一个项目，同时生成：
1. README 优化建议
2. 代码结构分析
3. 安全风险分析
4. 部署方案
```

这些可以并行：

```text
Code Agent
Document Agent
Security Agent
Deploy Agent
```

## 8.2 必须串行任务

例如：

```text
读取 Excel → 分析数据 → 生成报告
```

必须按依赖执行：

```text
Data Agent 读取 Excel
 ↓
Data Agent 分析数据
 ↓
Report Agent 生成报告
```

## 8.3 推荐实现：DAG 工作流

使用 DAG 表达任务依赖：

```json
{
  "workflow_name": "excel_analysis_report",
  "steps": [
    {
      "id": "step_1",
      "agent": "data_agent",
      "action": "read_excel",
      "depends_on": []
    },
    {
      "id": "step_2",
      "agent": "data_agent",
      "action": "analyze_statistics",
      "depends_on": ["step_1"]
    },
    {
      "id": "step_3",
      "agent": "report_agent",
      "action": "generate_report",
      "depends_on": ["step_2"]
    }
  ]
}
```

Workflow Engine 根据依赖关系判断哪些步骤能并行，哪些必须等待。

---

# 9. 每个小 Agent 是否都调用 LLM

设计原则：

```text
正式 Agent = Agent Definition（职责/指令/模型/工具/权限/契约）
           + 通用 AgentRunner（模型与工具循环）
           + Harness（状态/审批/验证/追踪）。
```

记忆、知识库和独立 UI 都是按场景选配，不要求每个 Agent 各自复制一套。一次调用即可确定完成的读取、搜索、转换、写入和验证应优先定义为 Tool；只有任务所有权、工具面、权限、模型策略或输出契约实质不同，才值得拆成独立 Agent。

但实际执行中可以优化：

```text
简单确定性任务：Agent 直接调用工具，不必每次调用 LLM。
复杂理解任务：Agent 调用 LLM。
总结解释任务：Agent 调用 LLM。
```

所以正确设计为：

```text
每个 Agent 具备 LLM 能力，但并非每一步都必须调用 LLM。
```

这样既能保持“小 Agent 是领域精英”的产品形态，又能减少 Token 成本。

---

# 10. Token 成本控制

多 Agent 通常比单 Agent 更耗 Token，因为多了：

```text
总指挥规划
Agent 间通信
每个 Agent 的系统提示词
中间结果总结
最终汇总
Evaluator 评估
```

优化策略：

```text
1. 简单任务走单 Agent，不启动完整多 Agent 工作流。
2. 复杂任务才走 Commander + 多 Agent。
3. Commander 使用强模型。
4. 小 Agent 根据任务选择便宜模型或专业模型。
5. 工具型步骤不调用 LLM。
6. 中间结果做摘要压缩。
7. RAG 只传最相关片段。
8. 给每个 Agent 限制最大上下文。
9. 每个任务设置 Token 成本上限。
10. 用户可在界面上查看本次任务 Token 消耗。
```

---

# 11. 模型配置设计

## 11.1 全局模型配置

```text
默认供应商
默认模型
默认 API Key
默认 Base URL
默认 temperature
默认 max_tokens
默认 timeout
```

工程实现里建议把这层收口到 `ModelGateway`，让全局配置只负责“当前默认用哪个 provider”，
真正的 DeepSeek / OpenAI / Claude / Qwen / 自定义 OpenAI-compatible 连接细节交给 gateway profile。
这样 Agent / Workflow / Tool 层就不会被某一家厂商的字段绑死。
当前仓库里 Qt 已先做了一个只读模型页入口，用来展示 `/api/models/providers` 返回的 provider profile 和 current runtime；后端已新增 `/api/models/config` 安全读写接口，支持本地配置持久化和 Windows DPAPI Key 加密存储。provider 切换、Key 保存/清空等可视化交互仍需继续接入 Qt 模型页。

## 11.2 Agent 独立模型配置

每个 Agent 可单独配置：

```text
model_provider
model_name
api_key
base_url
temperature
max_tokens
cost_limit
timeout
```

例如：

```text
Commander Agent：强模型
Code Agent：代码能力强的模型
Document Agent：便宜长上下文模型
Vision Agent：多模态模型
Report Agent：长文本模型
```

这里的 `allow_override` 语义也建议保留成“是否允许被全局默认配置覆盖或继承”，
而不是把 provider 逻辑散落到各个 Agent 里。

## 11.3 单次任务临时配置

用户可以在执行任务前设置：

```text
是否允许联网
是否允许执行命令
是否允许读取本地文件
是否允许写入文件
最大预算
使用强模型 / 经济模型
```

---

# 12. 插件化 Agent 架构

## 12.1 插件目录结构

推荐：

```text
AgentFlow/
├─ AgentFlow.exe
├─ backend/
│  └─ agent_server.exe
├─ data/
│  ├─ agentflow.db
│  ├─ knowledge/
│  └─ outputs/
├─ agents/
│  ├─ document_agent/
│  │  ├─ manifest.yaml
│  │  ├─ agent.py
│  │  ├─ tools.py
│  │  ├─ prompts/
│  │  │  ├─ system.md
│  │  │  └─ planner.md
│  │  ├─ ui/
│  │  │  └─ panel.json
│  │  ├─ assets/
│  │  │  └─ icon.png
│  │  ├─ knowledge/
│  │  ├─ requirements.txt
│  │  └─ README.md
│  ├─ code_agent/
│  ├─ data_agent/
│  ├─ vision_agent/
│  └─ report_agent/
└─ plugins/
```

## 12.2 `.afagent` 插件包格式

定义：

```text
.afagent = zip 格式的 Agent 插件包
```

内部结构：

```text
my_agent.afagent
├─ manifest.yaml
├─ agent.py
├─ tools.py
├─ prompts/
│  ├─ system.md
│  └─ planner.md
├─ ui/
│  └─ panel.json
├─ assets/
│  └─ icon.png
├─ knowledge/
│  └─ default_docs/
├─ requirements.txt
└─ README.md
```

## 12.3 manifest.yaml 示例

```yaml
id: document_agent
name: 文档精英
version: 1.0.0
description: 负责 PDF、Word、Markdown、TXT 文档解析、总结和结构化提取
icon: assets/icon.png

entry: agent.py
class_name: DocumentAgent

category: 文档处理
author: Layla

llm:
  provider: inherit
  model: inherit
  allow_override: true

tools:
  - read_pdf
  - read_docx
  - extract_tables
  - summarize_document

permissions:
  file_read: true
  file_write: true
  network: false
  shell: false
  database: false

ui:
  type: qt_dynamic_panel
  config: ui/panel.json

rag:
  enabled: true
  knowledge_path: knowledge/

dependencies:
  python:
    - pymupdf
    - python-docx
    - pandas
```

## 12.4 插件安装流程

```text
1. 用户点击“安装 Agent 插件”
2. 选择 .afagent 文件
3. 软件解压到 agents/ 目录
4. 校验 manifest.yaml
5. 检查插件签名
6. 展示权限申请
7. 用户确认安装
8. 安装 Python 依赖
9. 注册到数据库
10. Qt 主界面显示 Agent 图标
```

## 12.5 插件卸载流程

```text
1. 用户点击卸载
2. 禁用 Agent
3. 停止该 Agent 相关任务
4. 删除 Agent 文件夹或标记为已卸载
5. 删除数据库注册信息
6. 可选保留用户数据和历史记录
```

## 12.6 插件启用/禁用

```text
启用：Agent 可被单独打开，也可被 Commander 调用。
禁用：Agent 图标变灰，Commander 不会调用它。
卸载：从系统中移除。
```

---

# 13. Agent 统一接口设计

每个 Agent 应实现统一接口：

```python
class BaseAgent:
    id: str
    name: str
    description: str

    async def chat(self, message: str, context: dict) -> dict:
        # 单独使用 Agent 时的聊天接口
        pass

    async def plan(self, task: str, context: dict) -> dict:
        # Agent 自己为子任务生成执行计划
        pass

    async def execute(self, action: str, input_data: dict, context: dict) -> dict:
        # 被 Commander 或 Workflow Engine 调用时执行具体动作
        pass

    async def get_tools(self) -> list:
        # 返回该 Agent 可用工具
        pass

    async def get_capabilities(self) -> dict:
        # 返回该 Agent 能力描述，用于 Commander 路由
        pass
```

2026-07-10 实现校准：上面的类接口保留为长期插件抽象，不要求阶段 5 为每个内置 Agent 创建一份 `agent.py`。第一版先由通用 AgentRunner 执行 Agent Definition，专业差异放在指令、工具白名单、权限和输入输出 schema 中；动态导入第三方 entrypoint 等插件隔离成熟后再启用。

## 13.1 单独使用 Agent

```text
用户 → Document Agent.chat()
```

## 13.2 被总指挥调用

```text
Commander Agent → Document Agent.execute(action)
```

## 13.3 多 Agent 协作

```text
Commander 生成 DAG
Workflow Engine 按依赖执行
多个 Agent 写入 Shared Context
Commander 汇总最终结果
```

---

# 14. Shared Context Store 设计

多 Agent 协作必须有共享上下文。

Shared Context 应保存：

```text
用户原始任务
任务 ID
上传文件路径
执行计划
每一步输入
每一步输出
中间摘要
Agent 消息
工具调用记录
生成文件路径
错误日志
Token 消耗
耗时统计
权限确认记录
```

推荐结构：

```json
{
  "task_id": "task_001",
  "user_request": "帮我分析这个 Excel 并生成报告",
  "files": ["sales.xlsx"],
  "workflow_plan": {},
  "steps": {
    "step_1": {
      "agent": "data_agent",
      "status": "success",
      "output": {}
    }
  },
  "artifacts": ["report.docx", "chart.png"],
  "logs": [],
  "cost": {
    "input_tokens": 0,
    "output_tokens": 0,
    "estimated_cost": 0
  }
}
```

---

# 15. 工作流执行器设计

## 15.1 任务状态

```text
PENDING
PLANNING
WAITING_CONFIRMATION
RUNNING
SUCCESS
FAILED
CANCELLED
PAUSED
```

## 15.2 Step 状态

```text
PENDING
RUNNING
SUCCESS
FAILED
SKIPPED
WAITING_DEPENDENCY
WAITING_CONFIRMATION
```

## 15.3 执行流程

```text
1. 用户输入任务
2. Commander 生成结构化 JSON 计划
3. Workflow Validator 校验计划
4. 如涉及危险操作，进入 WAITING_CONFIRMATION
5. Workflow Engine 根据 DAG 执行步骤
6. 每一步调用对应 Agent
7. Agent 调用自己的 Tool
8. 结果写入 Shared Context
9. 出错时按策略重试
10. Evaluator 检查结果
11. Commander 汇总输出
12. 保存任务历史和生成文件
```

`WAITING_CONFIRMATION` 是原 run 的可恢复暂停：保存待审批调用与恢复状态，批准/拒绝后继续同一个任务和审计链，不另起一个看似无关的新任务。

## 15.4 并发执行

```text
无依赖步骤可并行。
有依赖步骤必须等待前置步骤成功。
失败步骤根据策略重试或终止。
```

---

# 16. MCP 与 A2A 预留

## 16.1 MCP

MCP 适合做工具插件生态。

AgentFlow 可以作为 MCP Host，不同 Tool 插件可以作为 MCP Server。

例如：

```text
filesystem_mcp
git_mcp
database_mcp
browser_mcp
camera_mcp
office_mcp
```

Agent 可以通过 MCP 调用外部工具。

## 16.2 A2A

A2A 适合未来做远程 Agent 协作。

第一版不需要实现完整 A2A，但架构上可以预留：

```text
本地 Agent Registry
 ↓
远程 Agent Registry
 ↓
A2A 通信层
```

未来用途：

```text
调用第三方远程 Agent
企业内部多个 AgentFlow 节点互相协作
别人软件里的 Agent 调用 AgentFlow Agent
```

---

# 17. RAG 设计

## 17.1 RAG 用途

不要只把 RAG 当 PDF 问答，应作为系统长期记忆层。

用途：

```text
个人知识库
项目知识库
课程资料库
公司文档库
Agent 自带知识库
历史任务经验库
错误解决方案库
工具说明库
```

## 17.2 RAG 流程

```text
上传文档
 ↓
文档解析
 ↓
文本切分
 ↓
Embedding 向量化
 ↓
存入向量数据库
 ↓
任务执行时检索相关内容
 ↓
将相关片段传给 Agent
 ↓
Agent 基于资料回答或执行
```

## 17.3 向量库选择

第一版：

```text
Chroma
SQLite-vec 可选
```

正式版：

```text
PostgreSQL + pgvector
```

大规模版：

```text
Milvus
```

---

# 18. 安全设计

商业化必须重视安全。

## 18.1 主要风险

```text
用户 API Key 泄露
用户文件泄露
插件恶意代码
LLM 被提示注入
Agent 误删文件
Agent 乱执行命令
Agent 乱联网发送数据
数据库被误操作
MCP 工具被污染
第三方插件崩溃影响主程序
```

## 18.2 安全机制

```text
1. 插件签名校验
2. 插件权限声明
3. 安装插件时展示权限
4. 危险操作二次确认
5. 文件访问白名单
6. 网络访问白名单
7. 命令执行黑名单
8. Docker Sandbox / 子进程隔离
9. API Key 加密存储
10. 工具调用审计日志
11. Prompt Injection 防御
12. RAG 内容与系统指令隔离
13. 插件独立进程运行
14. Agent 崩溃自动隔离
15. 任务可取消、可暂停、可回滚
```

## 18.3 API Key 存储

Windows：

```text
DPAPI
Windows Credential Manager
```

Linux：

```text
Secret Service
加密配置文件
```

macOS：

```text
Keychain
```

第一版可先使用本地加密配置文件，但要预留安全接口。

---

# 19. Qt + Python 后端协同启动

## 19.1 软件启动方式

目标效果：

```text
用户双击 AgentFlow.exe
 ↓
Qt 前端启动
 ↓
Qt 使用 QProcess 启动 Python 后端 agent_server.exe
 ↓
Qt 轮询 /health
 ↓
后端启动成功后进入主界面
 ↓
Qt 通过 HTTP / WebSocket 与后端通信
```

## 19.2 目录结构

```text
AgentFlow/
├─ AgentFlow.exe
├─ backend/
│  ├─ agent_server.exe
│  ├─ config/
│  └─ logs/
├─ agents/
├─ data/
├─ resources/
└─ AgentFlow.db
```

## 19.3 Qt 需要做的事情

```text
检测后端是否运行
启动后端进程
健康检查
端口占用处理
后端崩溃自动重启
关闭软件时关闭后端
重定向后端日志
显示后端状态
```

## 19.4 打包建议

不建议强行一个真正单文件 exe。推荐商业软件形式：

```text
AgentFlowSetup.exe 安装包
安装后桌面一个快捷方式
内部包含 Qt 主程序 + Python 后端 + 插件目录
```

可用：

```text
PyInstaller 打包 Python 后端
Inno Setup / NSIS / Qt Installer Framework 打包安装程序
```

---

# 20. UI 设计规划

## 20.1 首页：Agent 桌面

```text
┌──────────────────────────────────────────┐
│ AgentFlow                                │
├──────────────────────────────────────────┤
│  🤖 总指挥     📄 文档精英     💻 代码精英 │
│  📊 数据精英   👁 视觉精英     🎬 视频精英 │
│  🧠 知识库     📝 报告精英     🚀 部署精英 │
└──────────────────────────────────────────┘
```

## 20.2 总指挥页面

```text
左侧：Agent 列表
中间：任务对话
右侧：工作流图 / 执行日志
底部：文件上传 / 模型选择 / 权限开关
```

## 20.3 单 Agent 页面

每个 Agent 有自己的独立页面。

文档 Agent 页面：

```text
左侧：文档列表
中间：文档对话
右侧：摘要、表格、引用来源
底部：上传文件、选择模型、导出报告
```

代码 Agent 页面：

```text
左侧：项目文件树
中间：聊天 / 代码编辑区
右侧：终端日志 / 运行结果
底部：执行、停止、回滚、生成 README
```

视觉 Agent 页面：

```text
左侧：图片/视频列表
中间：预览窗口
右侧：检测结果
底部：模型选择、阈值、导出报告
```

## 20.4 插件管理页面

```text
已安装 Agent
启用 / 禁用
卸载
更新
权限查看
模型配置
依赖检查
```

## 20.5 模型设置页面

```text
供应商
Base URL
API Key
模型名称
温度
最大 Token
成本限制
测试连接
```

---

# 21. 数据库设计初稿

## 21.1 表：agents

```text
id
name
version
description
icon_path
category
enabled
install_path
created_at
updated_at
```

## 21.2 表：agent_configs

```text
id
agent_id
model_provider
model_name
base_url
api_key_encrypted
temperature
max_tokens
cost_limit
created_at
updated_at
```

## 21.3 表：tasks

```text
id
user_request
status
workflow_plan_json
created_at
updated_at
finished_at
total_tokens
estimated_cost
```

## 21.4 表：task_steps

```text
id
task_id
step_id
agent_id
action
status
input_json
output_json
error_message
started_at
finished_at
```

## 21.5 表：artifacts

```text
id
task_id
step_id
file_path
file_type
description
created_at
```

## 21.6 表：tool_calls

```text
id
task_id
step_id
agent_id
tool_name
input_json
output_json
status
created_at
```

## 21.7 表：knowledge_bases

```text
id
name
description
owner_agent_id
vector_store_type
path
created_at
```

---

# 22. 初版开发路线

## 阶段 0：项目骨架

目标：

```text
创建 Qt 前端项目
创建 FastAPI 后端项目
建立基础通信
建立目录结构
```

成果：

```text
Qt 能启动
FastAPI 能运行
Qt 能调用 /health
```

## 阶段 1：基础聊天

目标：

```text
Qt 聊天界面
后端调用 LLM
返回回答
WebSocket 或 HTTP 返回结果
```

成果：

```text
用户可以在 Qt 里和 LLM 聊天
```

## 阶段 2：Agent Registry

目标：

```text
定义 BaseAgent
定义 Agent manifest
扫描 agents/ 目录
加载内置 Agent
Qt 显示 Agent 图标
```

成果：

```text
首页能看到总指挥、文档、代码、报告等 Agent 图标
双击可打开单 Agent 页面
```

## 阶段 3：Commander Agent

目标：

```text
实现总指挥 Agent
能识别任务类型
能选择合适 Agent
能生成 JSON 工作流计划
```

成果：

```text
用户输入复杂任务，总指挥能输出结构化计划
```

## 阶段 4：Workflow Engine

目标：

```text
实现任务计划校验
实现 DAG 执行
实现步骤状态
实现 WebSocket 日志推送
```

成果：

```text
任务可以一步步执行，前端能看到实时日志
```

## 阶段 5：内置小 Agent

先做：

```text
Document Agent
Code Agent
Report Agent
```

最小能力：

```text
读取 txt / markdown
生成代码文件
生成 markdown 报告
```

成果：

```text
上传作业要求 → 文档 Agent 提取 → 代码 Agent 生成代码 → 报告 Agent 生成说明
```

## 阶段 6：文件与报告能力

加入：

```text
PDF 读取
Word 读取
Excel 读取
Word 报告生成
PDF 报告生成
```

## 阶段 7：RAG

加入：

```text
知识库创建
文档入库
向量检索
基于知识库问答
Agent 使用 RAG 上下文
```

## 阶段 8：插件系统

加入：

```text
.afagent 安装
插件 manifest 校验
插件启用/禁用
插件卸载
插件权限展示
```

## 阶段 9：Vision Agent

加入：

```text
OpenCV 图片读取
YOLO 目标检测
ONNX Runtime 推理
检测结果展示
图像分析报告生成
```

## 阶段 10：打包与发布

加入：

```text
PyInstaller 打包后端
Qt Release 打包
Inno Setup 制作安装包
日志目录
配置目录
自动启动后端
```

---

# 23. 初版 MVP 功能范围

初版不要做太大，建议先完成以下核心功能：

```text
1. Qt 桌面端主界面
2. Python FastAPI 本地后端
3. Qt 自动启动后端
4. LLM 基础聊天
5. Agent Registry
6. Commander Agent
7. Document Agent
8. Code Agent
9. Report Agent
10. 简单 Workflow Engine
11. WebSocket 执行日志
12. 本地 SQLite 保存任务历史
13. Markdown 报告生成
14. 插件 manifest 规范
15. Agent 图标桌面
```

MVP 演示场景：

```text
用户上传一个 txt / markdown 作业要求
 ↓
Commander 分析任务
 ↓
Document Agent 提取要求
 ↓
Code Agent 生成代码
 ↓
Report Agent 生成说明文档
 ↓
Qt 展示执行日志
 ↓
用户下载结果文件
```

这个 Demo 已经足够体现：

```text
Qt 桌面端
Python 后端
多 Agent
工作流
工具调用
文件处理
报告生成
实时日志
```

---

# 24. 商业化方向

## 24.1 产品版本

### 免费版

```text
基础 Commander
文档 Agent
代码 Agent
报告 Agent
SQLite
用户自备 API Key
```

### 专业版

```text
更多 Agent
工作流模板
高级 RAG
视觉 Agent
视频 Agent
批量任务
插件市场
```

### 定制版

```text
企业专属 Agent
行业知识库
私有化部署
海康相机 Agent
视频会议分析 Agent
工厂视觉检测 Agent
客服 Agent
论文 Agent
教育作业 Agent
```

## 24.2 插件市场

后续可以做：

```text
Agent 插件市场
用户下载 .afagent 插件
用户安装到 AgentFlow
部分插件免费
部分插件收费
部分插件需要联系开发者定制
```

## 24.3 定制服务

可以提供：

```text
定制行业 Agent
定制企业知识库
定制私有化部署
定制视觉检测流程
定制文档自动化流程
定制客服 Agent
```

## 24.4 与 Cursor / Claude Code 差异化

AgentFlow 不必正面打败 Cursor 或 Claude Code。

差异化定位：

```text
Cursor 偏代码编辑器
Claude Code 偏命令行开发助手
AgentFlow 偏桌面端多 Agent 工作流 + 插件市场 + 本地文件/视觉/音视频/办公自动化
```

优势：

```text
可视化桌面端
小 Agent 像 App 一样单独使用
多 Agent 协作
插件市场
支持文档、代码、数据、图像、视频
支持用户自备 Key
支持本地化和私有化
支持行业定制
```

---

# 25. 可移植性规划

## 25.1 Windows

优先支持。

```text
Qt exe
Python 后端 exe
Inno Setup 安装包
用户双击桌面图标使用
```

## 25.2 Linux

第二阶段支持。

```text
Qt Linux 客户端
Python 后端
AppImage / deb
Docker / systemd
```

## 25.3 macOS

可后续考虑，但签名、公证、打包较麻烦。

## 25.4 Android

不建议做完整本地版。

推荐未来做：

```text
Android 轻客户端
 ↓
连接电脑端 AgentFlow 或云端 AgentFlow Server
```

---

# 26. 可以借鉴的先进思想

不要直接研究或复制任何泄露源码，应借鉴公开产品和公开文档中的架构思想。

可以借鉴的思想：

```text
1. Agent 主循环
模型思考 → 调用工具 → 观察结果 → 继续思考 → 最终回答

2. Handoff
总指挥把任务交给专业 Agent

3. Guardrails
工具调用前后进行规则校验

4. Structured Output
让 LLM 输出 JSON 计划，而不是自然语言

5. Tracing
记录 Agent 每一步调用链路

6. Hooks
任务开始前、工具调用前、工具调用后、任务完成后插入逻辑

7. Skills
把某类能力封装成技能包

8. Context Compaction
上下文过长时自动压缩

9. Session Storage
会话和任务可恢复

10. Workspace Isolation
代码任务在独立工作区执行

11. MCP
统一工具插件协议

12. A2A
未来支持 Agent 间远程协作

13. AI Harness Agent
Agent = Model + Harness。模型只是“大脑”，Harness 负责 Context、Planner、Tool、Runtime、Memory、Verifier、Governance。
AgentFlow 中，Commander、Workflow Engine、Tool Registry、SQLite 任务记录、权限审计、计划校验器、WebSocket 日志都属于 Harness。

14. ModelGateway
模型供应商必须隔离在 ModelGateway，DeepSeek、OpenAI、Claude、Qwen、本地模型和私有 OpenAI-compatible 网关都只是 profile。
Agent / Workflow / Tool 层只依赖统一模型能力，不直接拼某家 API。

15. Focused Agent / Contract-based Split
先完成一个职责清楚的 Agent。只有工具面、权限策略、模型/上下文、输出契约或任务所有权不同才拆专家，不按页面和营销名称凑 Agent 数量。

16. Manager vs Handoff
阶段 5 由 Commander 保持任务和最终回复所有权，小 Agent 作为受控专业能力；只有确实需要专家接管后续会话时才使用 handoff。

17. Local Context vs Model Context
凭据、数据库对象、日志器、权限状态和内部路径留在 Runtime 本地上下文；模型真正需要的事实再通过输入、检索或脱敏工具结果提供。

18. Guardrail Layers / Resumable Approval
输入、工具参数/结果、最终输出分别校验；有副作用的调用通过人工审批暂停原 run，审批后从保存状态恢复。

19. Trace-first Evaluation
先记录一条 run 中的模型、工具、Guardrail、路由、审批和终态，再把代表性 trace 固化为离线回归用例。
```

---

# 27. 简历项目包装

项目名称：

```text
AgentFlow 多智能体 AI 工作流编排桌面平台
```

项目描述：

```text
基于 C++ Qt + Python FastAPI + LangGraph + RAG 设计并实现一套多智能体 AI 工作流系统。
系统采用 Commander Agent 作为总控中心，负责用户意图识别、任务拆解、Agent 路由和结果评估；
通过 LangGraph 构建状态化工作流，支持多 Agent 串行/并行执行、工具调用、失败重试、执行日志推送和任务状态持久化。
平台集成文档解析、代码生成、数据分析、知识库检索、图像/视频处理、报告生成等能力，
并通过 Qt 桌面端提供 Agent 图标桌面、单 Agent 独立页面、工作流可视化、文件拖拽上传、实时日志和结果文件管理功能。
```

技术栈：

```text
C++ Qt、Python、FastAPI、WebSocket、LangChain、LangGraph、RAG、SQLite/PostgreSQL、Redis、
OpenCV、FFmpeg、YOLO、ONNX Runtime、Docker、PyInstaller、Inno Setup
```

核心亮点：

```text
1. 设计多 Agent 注册与路由机制，实现 Commander Agent 对专业 Agent 的任务调度。
2. 基于 DAG 设计工作流执行器，支持串行、并行、依赖等待、失败重试和状态持久化。
3. 实现 Agent 插件标准 .afagent，支持 Agent 安装、卸载、启用、禁用和权限声明。
4. 实现工具注册与权限控制机制，为不同 Agent 绑定独立工具集。
5. 基于 WebSocket 实现 Agent 执行过程、工具调用日志和流式输出实时推送。
6. 集成 RAG 知识库，实现私有文档检索增强问答。
7. 集成 OpenCV/FFmpeg/YOLO/ONNX Runtime，实现图像和视频智能分析能力。
8. 使用 QProcess 管理本地 Python 后端服务，实现桌面端与 AI 服务协同启动和生命周期管理。
```

---

# 28. Codex 开发指令建议

给 Codex 开发时，建议先让它做“项目骨架”，不要一开始做完整大系统。

## 28.1 第一轮让 Codex 做

```text
请根据 AgentFlow 初版规划，创建一个最小可运行项目骨架：

1. 创建 server/ Python FastAPI 后端
2. 创建 client-qt/ C++ Qt 前端
3. 后端提供 /health 接口
4. 后端提供 /api/agents 接口，返回内置 Agent 列表
5. 后端提供 /api/chat 接口，暂时返回模拟回答
6. 后端提供 WebSocket /ws/tasks/{task_id}，可以推送模拟日志
7. Qt 前端启动后调用 /health
8. Qt 前端显示 Agent 图标列表
9. Qt 前端有一个简单聊天输入框
10. Qt 前端可以连接 WebSocket 显示日志
11. 暂时不接真实 LLM，先把前后端通信和目录结构跑通
```

## 28.2 第二轮让 Codex 做

```text
在现有项目基础上，实现 Agent Registry：

1. 定义 BaseAgent
2. 定义 Agent manifest.yaml 格式
3. 后端启动时扫描 agents/ 目录
4. 加载内置 document_agent、code_agent、report_agent
5. /api/agents 返回真实扫描结果
6. Qt 页面显示 Agent 名称、图标、描述、启用状态
```

## 28.3 第三轮让 Codex 做

```text
实现 Commander Agent 的初版：

1. 用户输入任务
2. Commander 根据关键词判断需要哪些 Agent
3. 生成 JSON workflow_plan
4. 前端显示该计划
5. 暂时不真正执行，只展示计划
```

## 28.4 第四轮让 Codex 做

```text
实现 Workflow Engine 初版：

1. 接收 workflow_plan
2. 按 steps 顺序执行
3. 调用对应 Agent 的 execute 方法
4. 通过 WebSocket 推送每一步日志
5. 保存任务状态到 SQLite
```

## 28.5 第五轮让 Codex 做

```text
接入真实 LLM：

1. 增加模型配置页面或配置文件
2. 支持 OpenAI-compatible API
3. Commander 调用 LLM 生成结构化 JSON 计划
4. 小 Agent 调用 LLM 完成自己的任务
5. 增加 JSON 输出校验
```

---

# 29. 当前最重要的开发原则

```text
1. 先做必要架构；阶段门槛达到后立即用一个经确认的 Agent 跑纵向闭环，不无限补底层。
2. 先做能跑通的闭环，不急着做高级 UI。
3. 先做内置 Agent，再做插件市场。
4. 先做本地导入 .afagent，再做在线下载。
5. 先做普通进程插件，再做 MCP 化。
6. 先做 Windows 桌面版，再考虑 Linux、Android。
7. 先做用户自备 API Key，降低运营成本。
8. 先做权限系统，后续商业化才不会返工。
9. 先做日志和状态持久化，方便调试。
10. 每个技术都要有明确用途，不要为了简历堆料而堆料。
```

---

# 30. 最终目标

AgentFlow 的最终目标可以定义为：

```text
一个面向个人和小团队的可扩展 AI Agent 桌面平台。
用户可以像安装 App 一样安装不同领域的小 Agent；
可以单独打开某个 Agent 完成专业任务；
也可以通过 Commander Agent 让多个 Agent 协同完成复杂工作流；
系统支持文档、代码、数据、图像、视频、部署、报告等多领域任务；
用户可以自备 LLM API Key，也可以使用本地模型；
开发者可以开发和售卖 .afagent 插件；
软件可以面向个人用户、学生、开发者、小团队和行业客户进行定制销售。
```

---

# 附录 A：推荐初版目录结构

```text
agentflow/
├─ README.md
├─ docs/
│  ├─ architecture.md
│  ├─ plugin_spec.md
│  ├─ api_spec.md
│  └─ development_plan.md
│
├─ client-qt/
│  ├─ CMakeLists.txt
│  ├─ src/
│  │  ├─ main.cpp
│  │  ├─ MainWindow.cpp
│  │  ├─ MainWindow.h
│  │  ├─ BackendManager.cpp
│  │  ├─ BackendManager.h
│  │  ├─ AgentDesktopWidget.cpp
│  │  ├─ ChatWidget.cpp
│  │  └─ LogWidget.cpp
│  └─ resources/
│
├─ server/
│  ├─ requirements.txt
│  ├─ main.py
│  ├─ app/
│  │  ├─ api/
│  │  │  ├─ health_api.py
│  │  │  ├─ agent_api.py
│  │  │  ├─ chat_api.py
│  │  │  ├─ workflow_api.py
│  │  │  └─ websocket_api.py
│  │  │
│  │  ├─ core/
│  │  │  ├─ config.py
│  │  │  ├─ logger.py
│  │  │  ├─ security.py
│  │  │  └─ lifecycle.py
│  │  │
│  │  ├─ agent/
│  │  │  ├─ base_agent.py
│  │  │  ├─ commander.py
│  │  │  ├─ registry.py
│  │  │  └─ builtin/
│  │  │     ├─ document_agent.py
│  │  │     ├─ code_agent.py
│  │  │     └─ report_agent.py
│  │  │
│  │  ├─ workflow/
│  │  │  ├─ planner.py
│  │  │  ├─ executor.py
│  │  │  ├─ dag.py
│  │  │  ├─ state.py
│  │  │  └─ checkpoint.py
│  │  │
│  │  ├─ tools/
│  │  │  ├─ base_tool.py
│  │  │  ├─ registry.py
│  │  │  ├─ file_tools.py
│  │  │  ├─ document_tools.py
│  │  │  ├─ code_tools.py
│  │  │  └─ report_tools.py
│  │  │
│  │  ├─ rag/
│  │  │  ├─ loader.py
│  │  │  ├─ splitter.py
│  │  │  ├─ embedding.py
│  │  │  ├─ vector_store.py
│  │  │  └─ retriever.py
│  │  │
│  │  ├─ database/
│  │  │  ├─ models.py
│  │  │  ├─ session.py
│  │  │  └─ repository.py
│  │  │
│  │  └─ sandbox/
│  │     ├─ python_runner.py
│  │     ├─ docker_runner.py
│  │     └─ permission.py
│  │
│  ├─ agents/
│  │  ├─ document_agent/
│  │  │  ├─ manifest.yaml
│  │  │  ├─ agent.py
│  │  │  ├─ tools.py
│  │  │  ├─ prompts/
│  │  │  ├─ ui/
│  │  │  └─ assets/
│  │  ├─ code_agent/
│  │  └─ report_agent/
│  │
│  ├─ data/
│  │  ├─ agentflow.db
│  │  ├─ outputs/
│  │  ├─ uploads/
│  │  └─ knowledge/
│  │
│  └─ logs/
│
└─ packaging/
   ├─ pyinstaller/
   ├─ inno_setup/
   └─ scripts/
```

---

# 附录 B：第一版 API 草案

```text
GET  /health
GET  /api/agents
GET  /api/agents/{agent_id}
POST /api/agents/{agent_id}/chat
POST /api/tasks
GET  /api/tasks/{task_id}
GET  /api/tasks/{task_id}/steps
POST /api/tasks/{task_id}/cancel
GET  /api/artifacts/{artifact_id}
POST /api/plugins/install
POST /api/plugins/{agent_id}/enable
POST /api/plugins/{agent_id}/disable
DELETE /api/plugins/{agent_id}
WS   /ws/tasks/{task_id}
```

---

# 附录 C：第一版技术栈定稿

```text
桌面端：
C++ Qt 6
Qt Widgets 或 QML
QNetworkAccessManager
QWebSocket
QProcess
CMake

后端：
Python 3.11+
FastAPI
Uvicorn
Pydantic
SQLAlchemy
SQLite
asyncio

Agent：
LangChain
LangGraph
OpenAI-compatible API
自研 Agent Registry
自研 Workflow Engine

RAG：
Chroma 起步
后续 pgvector

文件处理：
PyMuPDF
python-docx
openpyxl
pandas
markdown

报告生成：
Markdown
python-docx
ReportLab

视觉后续：
OpenCV
FFmpeg
YOLO
ONNX Runtime

工程化：
Git
Docker
PyInstaller
Inno Setup
日志系统
配置文件
```

---

# 附录 D：最小闭环 Demo

输入：

```text
帮我根据这个作业要求生成代码和报告。
```

执行流程：

```text
1. 用户上传 assignment.txt
2. Commander Agent 读取用户任务
3. Commander 生成 workflow_plan
4. Document Agent 读取 assignment.txt
5. Code Agent 生成 main.py
6. Report Agent 生成 README.md
7. Workflow Engine 保存执行日志
8. Qt 前端实时展示：
   [Commander] 正在规划任务
   [Document Agent] 正在读取文件
   [Code Agent] 正在生成代码
   [Report Agent] 正在生成报告
   [完成] 已生成 main.py 和 README.md
9. 用户点击下载结果
```

这个最小闭环就是第一阶段最重要的目标。

---

# 附录 E：给 Codex 的总要求

开发时优先保证：

```text
1. 项目能跑起来
2. 前后端能通信
3. 目录结构清晰
4. Agent 接口稳定
5. 插件规范预留
6. 工作流计划使用 JSON
7. 状态和日志可见
8. 后续容易扩展
```

不要一开始追求：

```text
1. UI 极致美观
2. 所有 Agent 一次做完
3. 插件市场一次做完
4. MCP / A2A 一次实现
5. 真正商业加密授权一次完成
```

第一目标：

```text
跑通 AgentFlow 的多 Agent 工作流最小闭环。
```
