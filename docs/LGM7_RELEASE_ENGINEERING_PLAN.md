# LGM7 目录发行工程计划

最后更新：2026-09-09

## 目标与边界

LGM7 的目标是把已验证的 Native 能力变成可诊断、可升级的 Windows **目录式发行**。客户只从
`AgentFlow.exe` 启动；Python 后端、Qt DLL、可选 Node Harness 与客户可变数据有明确边界。

本计划不把 LangGraph 重新放入客户路径：LGM5.7 的真实试点已因启动/常驻资源超过门槛被拒绝，
Native 仍是唯一默认 Runtime。LangChain 也不随发行新增。

发行形态采用 PyInstaller `onedir`。官方文档说明 onedir 是默认目录形式、便于诊断载荷，并建议在尝试
单文件之前先让目录包稳定运行；因此本项目不以单文件作为当前交付目标。参考：
[PyInstaller operating mode](https://www.pyinstaller.org/en/stable/operating-mode.html)、
[PyInstaller usage](https://www.pyinstaller.org/en/stable/usage.html)。

目录结构目标：

```text
AgentFlow/
  AgentFlow.exe                         # 唯一客户入口
  backend/AgentFlowBackend.exe          # PyInstaller onedir 后端
  backend/_internal/                    # Python 依赖与只读后端资源
  runtime/deepseek_harness_node/        # 仅显式携带 Harness 时存在
  runtime/node/node.exe                 # 仅显式携带 Harness 时存在
  release-manifest.json

%LOCALAPPDATA%/AgentFlow/
  data/                                  # SQLite、导入副本、索引、模型缓存、状态
  output/                                # 文档、PPT、数据、知识库正式交付物
  agents/                                # 用户安装的插件/Agent（未来受控入口）
```

发行载荷绝不收集 `.env`、API Key、`data/`、`output/`、客户插件、开发日志或 Node 探针状态。
原始客户文件及既有产物不因卸载、禁用 MCP 或移除可选 Node Harness 被删除。

## LGM7.0：运行时解析与后端目录包基础

状态：**已完成工程基础，尚未构建对外候选包。**

- Qt `BackendManager` 在 `AGENTFLOW_RELEASE_MODE=directory` 时只接受
  `backend/AgentFlowBackend.exe`；客户直接双击目录包时也会根据同级受控后端自动识别发行模式。
  缺失时明确停止，不再退回全局 Python。
- 开发模式继续优先使用 `AGENTFLOW_PYTHON` 或 `backend/.venv/Scripts/python.exe`；系统 Python
  必须通过 `AGENTFLOW_ALLOW_SYSTEM_PYTHON=true` 显式允许。
- 目录发行由 Qt 为后端注入受控的 `AGENTFLOW_DATA_DIR`、`AGENTFLOW_OUTPUT_DIR`、用户 Agent 根、
  项目根与可选 Node 路径；后端所有正式交付路径从统一 `output_dir` 派生。
- Node Harness 在目录发行只从 `runtime/node/node.exe` 发现 Node，并把该目录前置给 `dsh.cmd`；
  Node 不存在时可选 Harness 诊断为未就绪，不能破坏 Native 路径。
- `backend/packaging/agentflow_backend.spec` 固定 PyInstaller onedir 后端，显式收集正式 Python
  依赖、内置 Agent 与只读 Harness profile，不收集 Node `node_modules` 或任何敏感/客户目录。
- `backend/scripts/build_directory_backend.py` 只在显式给出空 `--release-root` 时构建后端载荷；
  Node Harness 还须显式选择并提供便携 Node。`--dry-run` 不写文件、不调用 PyInstaller。

本阶段验证：`verify_release_contract.py`、Python 编译、依赖一致性和 Qt `BackendManagerTests`。
这不是“正式安装包已完成”声明：构建机尚未安装 PyInstaller，且 Qt 主程序/DLL 尚未装配到候选目录。

## 后续顺序与出口

### LGM7.1：候选目录装配与 SBOM

状态：**已完成一次本机构建候选验证，尚非对外发行。**

- 在隔离构建环境安装锁定的 `requirements-dev.txt`；构建 `AgentFlowBackend` onedir 并运行离线
  `/health`、启动/停止、SQLite 写入、受控 artifact 回读测试。
- 使用 CMake install / `qt_generate_deploy_app_script` 将 Release `AgentFlow.exe` 与 Qt DLL 装入同一目录；
  根目录只保留一个客户启动入口。Qt 部署目录与入口统一为 `.`；
  `verify_directory_release_layout.py` 拒绝遗留 `bin/` 等双入口布局。
- 生成版本、Python、Qt、Node（如携带）、MCP SDK、许可证与哈希的 SBOM；禁止记录密钥、用户路径、
  文件名、材料正文和任务内容。
- `generate_release_sbom.py` 只从正式 runtime requirements 与构建环境公开元数据生成 Python 依赖清单；
  许可证只保存可读摘要，正文仍以第三方发行物为准。目录装配会自动写入该 SBOM。
  `verify_directory_backend_payload.py` 在临时用户数据目录启动打包后端并回读 `/health`；
  `verify_directory_client_smoke.py` 进一步验证根级 Qt 入口自动拉起随包后端并正常关闭。两者都不调用模型或读取客户材料。

本机候选事实：PyInstaller `6.22.2`、Release Qt 主程序、根级 `AgentFlow.exe`、随包
`backend/AgentFlowBackend.exe`、Qt DLL/插件、`release-manifest.json` 与 `release-sbom.json` 已形成干净目录。
候选共 6,733 个文件、约 859.7 MiB，仅用于工程诊断，未压缩、未签名、未对外发布；默认未携带 Node Harness。
`verify_directory_release_layout.py`、`verify_directory_backend_payload.py` 与
`verify_directory_client_smoke.py` 已通过，后者确认自动发现同级后端、`/health` 与正常关闭后的端口释放。

出口：**已达到本机候选出口。**LGM7.2 仍需覆盖离线与损坏依赖矩阵，才能讨论客户可拿到的候选包。

### LGM7.2：可选运行时与离线故障矩阵

状态：**自动化离线出口已完成；真实远端服务与客户可见故障提示留到 LGM7.3 的明确授权验收。**

- 已验证 MCP 默认停用、损坏本地连接状态的安全隔离和“一键停用即重置”、Node 缺失、Embedding/OCR
  模型未准备、无效数值配置、随包后端缺失以及端口被占用等场景。Native 后端不因可选能力不可用而退出。
- 目录发行默认排除 Paddle/PaddleOCR/PaddleX；构建机存在 OCR 开发依赖也不会让客户候选包隐式携带它们。
  向量/OCR 状态查询只检查包和 ready marker，不在启动期导入模型依赖、加载模型或下载权重。
- 真实远端 MCP Server 的不可达、认证和协议变更仍不作为本轮离线候选测试对象；只有在客户明确授权的
  LGM7.3 验收中连接外部服务，且只记录脱敏状态和聚合耗时。
- 验证可选 Node Harness 携带时不读取客户工作区 `.env`，禁用后不启动子进程；MCP 连接备份、迁移、禁用与
  密钥清理只作用于连接配置和专属状态。
- 已回归旧 SQLite migration、Native 深度任务 checkpoint、被拒绝的 LangGraph 试点隔离和遗留百分号编码
  artifact 回读；不迁移或删除客户成果。

出口：**已达到自动化离线出口。**基础 Native 能力可在任一默认可选依赖缺失条件下运行；损坏的 MCP
状态可被安全重置，客户资料、任务和正式产物不受影响。

### LGM7.3：性能与关闭验收

- 同机测量冷启动、首次健康检查、常驻内存、并发只读任务、长任务取消和 Qt 关闭后后端/Node/MCP 子进程清理。
- 每个新增运行时与无可选能力基线比较；超过当前 10% 默认门槛的组件继续保持关闭，不靠提升阈值放行。
- 真实模型或远程 MCP 验收只能在明确授权后进行，记录有限聚合指标与脱敏状态，不记录客户正文或 Key。

出口：发行候选的启动、关闭、离线、损坏依赖和恢复行为都有自动回归与一次明确的人工 UI 验收说明。

## 不做

- 不构建单文件 Python 后端；官方 PyInstaller 文档也建议先稳定验证 onedir，单文件会增加启动与临时解压复杂度。
- 不把项目中现有的开发 `.venv`、`node_modules`、构建缓存或三方示例材料直接复制进客户包。
- 不为“看起来技术栈更多”携带 LangGraph、LangChain、未批准的 MCP 或全局 Node 依赖。
- 不在这一阶段改变 Agent、工具权限、模型路由、客户任务或输出协议。
