# 新电脑重建与迁移指南

> 适用场景：从旧电脑迁移 AgentFlow 开发环境到一台新的 Windows 电脑。
>
> 本指南对应已准备好的迁移包：`F:\AgentFlow-Migration-20260930`。其中已有源码离线包、项目数据、历史交付物和私密配置备份。

## 先说结论

新电脑上需要做两件事：

1. 重新安装开发环境并从 GitHub 获取代码。
2. 将 U 盘里的 `data` 和 `output` 恢复到新项目中。

这样可以保留以前的任务历史、长期记忆、知识库、导入的视频、工作区和已生成的交付物。

模型 API Key 需要在新电脑重新填写。旧电脑的 Key 由 Windows DPAPI 加密，复制后无法在另一台电脑解密，这是正常的安全机制。

## 1. U 盘中已经有什么

迁移包根目录为：

```text
F:\AgentFlow-Migration-20260930\
├─ AgentFlow.bundle                 # GitHub 不可用时的完整离线源码包
├─ data\                            # 任务、记忆、知识库、媒体和工作区数据
├─ output\                          # 已生成的交付文件
└─ private\
   ├─ backend.env                    # 私密本地环境配置备份
   └─ model_config_dpapi_encrypted_backup.json
                                     # 仅备份参考，不能直接恢复使用
```

`data` 已包含下列真正需要延续的内容：

- `agentflow.db`：任务历史、会话、长期记忆、项目和 Artifact 元数据。
- `knowledge_bases`、`knowledge_vectors`：已有知识库正文和索引。
- `knowledge_embedding_models`：已下载的 BGE 向量模型缓存。
- `media_sources`、`media_workspace`：受控视频素材和短视频剪辑项目。
- `workspaces`、`data_workspace`、`CSV`：文档与数据工作区材料。
- `ocr_models`：已下载的 OCR 模型。

没有带走 `build`、`backend/.venv`、历史模型探针和评测缓存。这些都可以在新电脑重新生成，不影响继续开发。

## 2. 新电脑需要安装的软件

按下面顺序安装。所有软件均安装 64 位版本。

| 软件 | 必需原因 | 建议版本或组件 |
| --- | --- | --- |
| Windows | 桌面端运行环境 | Windows 10/11 x64 |
| Git for Windows | 拉取、提交和推送代码 | 最新稳定版即可 |
| Python | FastAPI 后端 | Python 3.11 x64 |
| Visual Studio 2022 Build Tools | 编译 C++/Qt 桌面端 | MSVC v143 C++ x64/x86 Build Tools + Windows 10/11 SDK |
| Qt Creator + Qt | 编辑、构建和运行 Qt 桌面端 | Qt `6.11.0`，组件选择 `MSVC 2022 64-bit` |
| FFmpeg | 视频探测、音轨提取和 EDL 渲染 | Gyan FFmpeg Essentials 或同等 Windows 发行版，并加入 PATH |

只有需要维护历史 Node DeepSeek Harness 时，才额外安装 Node.js LTS。当前 AgentFlow 主链不依赖它。

安装 FFmpeg 后，在 PowerShell 验证：

```powershell
ffmpeg -version
ffprobe -version
```

若命令找不到，可使用 Windows Package Manager：

```powershell
winget install Gyan.FFmpeg.Essentials
```

## 3. 获取源码

推荐直接从 GitHub 获取最新版本。在你准备存放项目的位置执行：

```powershell
git clone https://github.com/Avenger173/AgentFlow.git
cd AgentFlow
```

如果新电脑暂时没有网络，使用 U 盘中的离线 Git 包：

```powershell
git clone F:\AgentFlow-Migration-20260930\AgentFlow.bundle AgentFlow
cd AgentFlow
git remote set-url origin https://github.com/Avenger173/AgentFlow.git
git switch main
```

之后网络恢复时，在项目根目录执行下面两条命令，即可同步 GitHub 的最新代码：

```powershell
git fetch origin
git pull
```

## 4. 恢复旧电脑数据

**先恢复数据，再首次启动 AgentFlow。**

假设新项目位于 `D:\project\AgentFlow`，执行：

```powershell
cd D:\project\AgentFlow

robocopy F:\AgentFlow-Migration-20260930\data .\data /E /COPY:DAT /DCOPY:DAT /R:2 /W:1
robocopy F:\AgentFlow-Migration-20260930\output .\output /E /COPY:DAT /DCOPY:DAT /R:2 /W:1

Copy-Item F:\AgentFlow-Migration-20260930\private\backend.env .\backend\.env -Force
```

`robocopy` 显示退出码 `0` 或 `1` 都表示正常。复制完成后，`data\agentflow.db`、知识库和受控媒体目录应当存在。

**不要执行下面这件事：**

```text
不要把 private\model_config_dpapi_encrypted_backup.json
复制成 data\model_config.json
```

它含有仅旧电脑可解密的 DPAPI 密文。保留在 U 盘作备份参考即可。

## 5. 创建后端 Python 环境

在项目根目录执行：

```powershell
py -3.11 -m venv backend\.venv
.\backend\.venv\Scripts\python.exe -X utf8 -m pip install --upgrade pip
.\backend\.venv\Scripts\python.exe -X utf8 -m pip install -r backend\requirements-dev.txt
.\backend\.venv\Scripts\python.exe -m pip check
```

若 `py -3.11` 找不到 Python，说明 Python 3.11 未安装，或安装时没有启用 Python Launcher。重新安装 Python 3.11 后再执行本节命令。

## 6. 构建 Qt 桌面端

### 方式 A：Qt Creator，推荐

1. 打开 Qt Creator。
2. 选择“打开项目”，选中项目根目录的 `CMakeLists.txt`。
3. 选择 Qt `6.11.0` 的 `MSVC 2022 64-bit` Kit。
4. 配置完成后点击构建，再点击运行。

桌面端会自动探测并启动 `backend/.venv` 中的 FastAPI 后端，不需要先单独开一个后端终端。

### 方式 B：命令行 CMake

此方式要求在已加载 MSVC x64 编译环境的终端中执行。把 Qt 路径替换为你自己的实际安装路径：

```powershell
cmake -S . -B build\dev -G Ninja -DCMAKE_PREFIX_PATH="C:\Qt\6.11.0\msvc2022_64"
cmake --build build\dev --parallel
.\build\dev\AgentFlow.exe
```

若 CMake 提示找不到 Qt，优先确认 `CMAKE_PREFIX_PATH` 指向包含 `lib\cmake\Qt6` 的 Qt Kit 根目录。

## 7. 首次启动后的必做检查

启动桌面端后，按顺序检查：

1. 页面右上角或状态区显示本地后端已连接。
2. 打开“历史任务”，确认旧任务记录和交付物仍可见。
3. 打开“知识库”，确认以前的资料库和索引仍在。
4. 打开“短视频剪辑”，确认受控素材项目仍在。
5. 打开“模型密钥”，为需要使用的 Provider 重新填写 API Key，并测试连接。

至少重新配置：Qwen 文本/ASR、DeepSeek 或 Kimi 文本模型，以及需要使用时的 Seedream 图像 Provider。供应商、模型名、温度和任务路由可以按旧配置参考重新设置，但密钥必须重新输入。

## 8. 无法自动启动后端时

先在项目根目录确认 Python 环境存在：

```powershell
Test-Path .\backend\.venv\Scripts\python.exe
```

若返回 `True`，可手动启动后端以查看错误：

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8765 --reload
```

然后在浏览器打开：

```text
http://127.0.0.1:8765/health
```

能返回健康状态后，再启动 Qt 桌面端。手动调试结束时在该终端按 `Ctrl+C` 停止后端。

## 9. 常见问题

| 现象 | 处理方式 |
| --- | --- |
| `ffmpeg` 或 `ffprobe` 找不到 | 安装 FFmpeg 并重开终端；确认其 `bin` 目录已加入 PATH。 |
| CMake 找不到 Qt6 | Qt Kit 必须是 `MSVC 2022 64-bit`，并修正 `CMAKE_PREFIX_PATH`。 |
| `pip install` 中文依赖文件报编码错误 | 使用文档中的 `-X utf8` 参数，不要把 `requirements` 文件转为 ANSI。 |
| 模型页面显示 Key 已配置但调用失败 | 不要恢复旧 `model_config.json`；在新电脑重新保存该 Provider 的 Key。 |
| 历史任务或知识库为空 | 检查是否在第一次启动前把 U 盘 `data` 完整复制到项目根目录。 |
| 视频任务提示 FFmpeg 未就绪 | 先执行 `ffmpeg -version` 和 `ffprobe -version`，再重启 AgentFlow。 |

## 10. 换机完成后的第一轮验证

完成上述步骤后，建议在项目根目录运行：

```powershell
.\backend\.venv\Scripts\python.exe -m compileall -q backend\app
```

然后用 Qt Creator 构建并启动一次桌面端，确认后端连接、历史任务、知识库和模型配置都正常。确认无误后，U 盘中的迁移包仍建议保留一份，不要立刻删除。
