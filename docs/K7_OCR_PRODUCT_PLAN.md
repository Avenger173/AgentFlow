# K7 扫描件与图片型 PDF OCR 计划

## 已确认的产品决策

用户已确认 K7 只推进 **扫描件 / 图片型 PDF 的 OCR 增强**。网页、云盘、数据库连接器、外部知识源、多人 ACL、行业包和知识图谱不随 K7 进入开发或评估。

目标不是新增一个“识字按钮”，而是让客户能把扫描合同、拍照资料和图片型 PDF 安全地纳入既有文档助手与知识库：识别后的内容仍有页码/区域来源，可被现有检索、Evidence Gate、问答和深度任务使用。

## K7 MVP 范围

### 客户可完成的流程

1. 导入 PDF、PNG、JPG 或 JPEG 后，系统先尝试现有本地文本提取。
2. 当 PDF 没有可提取文本、或导入的是图片时，页面明确显示“需要 OCR”，而不是笼统报导入失败。
3. 客户显式确认准备本地 OCR 模型后，后台在受控副本上按页/图识别；原文件绝不修改。
4. 完成后，资料显示“文本提取”或“OCR 已完成”的状态、处理范围和失败页数；检索与回答继续使用带页码来源的受控文本。
5. OCR 单页失败不撤回其它成功页；全失败保留可操作原因，不把空内容伪装为已索引资料。

### 首期明确不做

- 不默认发送文件、页面图片或识别文本到云端模型；不在后台静默下载模型。
- 不把复杂表格还原、公式识别、手写体高精度识别、版面重建、翻译或 OCR 结果回写 PDF 当作首期交付。
- 不新增独立 OCR Agent 或页面；OCR 是文档导入/资料库解析的受控 Tool，Document Agent 与 Knowledge Agent 继续保有各自任务所有权。
- 不接入 MinerU 在线服务、网页/云盘连接器或任何通用 MCP。

## 架构与权限边界

- 增加独立 `OcrAdapter`，由受控解析层调用；它只接收受控副本和有限页/图范围，返回文本、置信度摘要、页码/区域锚点与脱敏错误类别。
- OCR 模型包按现有本地 Embedding 的模式通过显式“准备本地 OCR”动作下载或初始化；未准备时返回 `ocr_not_ready`，不自动联网。
- 本地 OCR 运行在后台线程/任务链，Qt 主线程只展示真实阶段和可恢复结果；不以假进度掩盖长页数等待。
- 解析版本、父子分块、index generation、删除和更新仍使用当前知识库契约。OCR 文字只进入受控版本与必要的来源锚点；任务日志、性能观测和评测不记录客户正文、整页图片、绝对路径或模型文件路径。
- 云端视觉模型仅作为未来独立 Adapter 候选：必须先新增数据出站确认、Provider 传输约束、成本上限、失败降级和真实验收；不属于 K7 MVP。

## K7.1 技术试验与选型门槛

首个候选是本地 PaddleOCR 的最小通用 OCR 能力，而不是安装全部文档理解组件：其官方文档支持按能力安装 Python 包和本地推理引擎。[PaddleOCR 安装文档](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/installation.en.md)

MinerU 保留为后续复杂版面/表格/公式的对比候选，不作为首期依赖：官方资料说明它能在 Windows 使用本地 PDF/图片输入和 CPU pipeline，但完整能力会启动本地服务并带来更高的依赖、模型和运行管理成本。[MinerU 快速开始](https://github.com/opendatalab/MinerU/blob/master/docs/en/quick_start/index.md) [MinerU CLI 使用说明](https://github.com/opendatalab/MinerU/blob/master/docs/en/usage/cli_tools.md)

K7.1 只允许在隔离临时目录完成下列探针，尚不向客户入口开放：

1. Windows + 当前 `backend/.venv` 的依赖、Python、CPU/GPU 与打包体积探针。
2. 合成中文/英文图片、无文本层 PDF、旋转页面和损坏文件的离线解析夹具。
3. 页码锚点、来源回读、单页失败、取消/超时、删除/重建和未准备模型的确定性回归。
4. 对比最小模型准备时间、单页/P95 耗时、内存和识别可用性；不以单张演示图决定选型。

只有探针满足“显式准备、本地离线、页码可追溯、失败可解释、不会破坏文本 PDF 与知识库 generation”的门槛，才进入 K7.2 Adapter 与受控解析实现。若本地方案无法满足 Windows 打包/性能边界，先报告证据并停在技术选择，不以云端调用替代。

### K7.1 选型结果（2026-08-25）

K7.1 已只在系统临时目录中完成，未修改 `backend/.venv`、默认 requirements、工作区、资料库或客户文件。探针脚本为 `backend/scripts/verify_ocr_technology_probe.py`；默认运行只输出 skipped，只有显式 `--live-local` 才下载候选模型并识别脚本生成的固定中英文字图片。

- 临时环境：Windows、CPython 3.13、`paddlepaddle 3.3.1`、`paddleocr 3.7.0`、`PyMuPDF 1.28.2`；依赖可导入且 `pip check` 通过。临时 Python site-packages 约 846 MB，说明发行版必须把 OCR 做成明确可选组件，不能静默塞入默认轻量安装。
- 默认中文 server 组合可识别，但同一双线程 CPU 上首次页约 64 秒、热调用约 38 秒，模型缓存约 173 MB，不进入首期候选。
- `PaddleOCR` 默认 oneDNN 路径在该 Windows/Paddle 组合发生 PIR 运行时 `NotImplementedError`；关闭 `PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT` 后，纯 CPU 路径稳定通过。K7.2 必须将该开关作为当前版本的受控兼容配置，不把它误写为普遍性能优化。
- 选定候选为 `PP-OCRv5_mobile_det + PP-OCRv5_mobile_rec + PP-LCNet_x1_0_doc_ori`：本地缓存约 28.6 MB。固定 5 次热调用中位/P95 为约 3.83/3.94 秒，冷页约 3.06 秒，无文本层 PDF 渲染页约 3.60 秒，进程 RSS 约 496 MB；四个文字区域、固定英文编号与中文标识均通过。
- 未加方向分类器的 mobile 组合对 90 度旋转图片只识别 `0/2` 个固定标识；方向分类候选识别 `2/2`，因此首期不能省略方向组件。损坏 PDF 在进入 OCR 前被 PyMuPDF 确定性拒绝。

这些只是合成夹具与当前机器上的可重复技术证据，不等同于真实扫描合同准确率、通用 P95 承诺、表格识别能力或正式客户功能验收。

## K7.2 受控 Adapter 边界

K7.2 才允许在生产代码增加延迟导入的 `OcrAdapter` 与解析协议，仍不开放独立 OCR 页面。它必须：

1. 保持 OCR 依赖在可选 requirements/profile 中；基础后端未安装或模型未准备时返回稳定的 `ocr_not_installed` / `ocr_not_ready`，绝不下载。
2. 仅从受控副本接收图片或单页渲染图，按页返回文本、置信度摘要、页码和区域锚点；不得把原图、全文、绝对路径或模型目录写入任务事件、SQLite 或 UI。
3. 为当前 Windows CPU 配置固定 `mobile-orientation` 档位和禁用 oneDNN 的兼容策略；模型准备动作显示约 29 MB 权重与“可选组件”说明，实际包/发行体积在 H5 打包阶段另行核验。
4. 先补离线 adapter 夹具：未安装、未准备、无文本层 PDF、普通图片、旋转图片、损坏文件、单页失败和页码来源；现有文本 PDF/DOCX 解析回归必须保持不变。
5. 只在后续客户入口明确批准时，才接入“准备本地 OCR”和后台导入状态；不把 K7.2 的后端协议伪装成已可用 UI 功能。

### K7.2 完成结果（2026-08-25）

- 已新增 `backend/requirements-ocr.txt`。基础 `requirements.txt` 保持不变，因此普通后端启动的能力诊断为 `paddleocr_available=false`、`model_initialized=false`，不会导入 Paddle、下载模型或拖慢既有知识库。
- 已新增延迟导入的 `app.services.ocr_adapter`：固定 K7.1 的移动模型 + 方向分类 profile，使用受控缓存、ready marker、显式 `allow_download`，并在当前 Windows 路线关闭 oneDNN。它只接收 Runtime 内部已受控的 `Path`，返回页码、文字、置信度摘要与区域锚点；它不注册 HTTP API、不写 SQLite、不读 workspace 名称、不返回绝对路径。
- `verify_ocr_adapter.py` 已用假引擎完成离线回归：未准备停驻、图片区域、旋转图片、无文本层两页 PDF 的单页失败保留、损坏 PDF、未知后缀和源图片不修改均已覆盖。`verify_knowledge_chunking.py`、`verify_backend.py`、`compileall` 与 `pip check` 同时通过。

## K7.3 受控解析接入完成结果（2026-08-25）

- `workspace_documents` 现接收受控 Base64 导入的 PNG/JPG/JPEG；图片逻辑类型为 `image`，仍受既有 10MB 二进制上限、文件名清洗和 workspace/知识库私有副本边界约束。
- 可复制文本 PDF 保持原有 PyMuPDF 文本提取路径，绝不额外调用 OCR；只有**整份** PDF 无可提取文本时才调用已准备的本地 Adapter。DOCX、TXT 与 Markdown 行为没有变化。
- Adapter 结果按“第 N 页 · 区域 M”转入既有解析文本和父子分块链路。PDF 的局部页面识别失败不会撤回其它成功页；未安装或未准备模型只返回“需要 OCR / 需要确认准备”的脱敏说明，不下载、不初始化模型。
- 为让已有知识库兼容 `image` 文档类型与 `region` 来源锚点，新增前向 SQLite migration `20260825_knowledge_ocr_contract_v1`。它以影子表复制、反向依赖替换和提交前 `foreign_key_check` 升级旧 CHECK 约束，保留 document/version/chunk/generation 的稳定 ID、内容与索引事实，不读取源文件、不重建索引。
- 新增 `backend/scripts/verify_ocr_parser_integration.py`：离线假 Adapter 覆盖图片、无文本层 PDF、普通文本 PDF 跳过 OCR、区域定位、图片进入知识库、未准备模型停驻，以及带历史数据的旧 SQLite 迁移。`compileall`、`verify_ocr_adapter.py`、`verify_knowledge_chunking.py`、`verify_backend.py` 与 `pip check` 均通过。

**K7.4 下一步：**实现面向客户的 OCR 能力状态与显式准备入口。它必须先展示“可选组件 / 约 29MB 模型 / 本地处理 / 不上传材料”，客户确认后才安装或准备；导入和索引不能暗中下载。随后再以后台任务、真实阶段和 Qt 工作台状态承接准备、OCR、部分页失败与重试，最后才进行真实扫描件客户验收。

### K7.4.1 客户可见能力状态与模型准备（已完成，2026-08-25）

- 后端新增只读 `GET /api/knowledge/ocr-capability`：只检查可选依赖与受控 ready marker，不导入 Paddle、不加载模型、不读取材料、更不联网。普通开发环境的真实诊断为“可选组件未安装 / 模型未准备”，这是可行动状态，不是 OCR 已失败。
- `POST /api/knowledge/ocr-model/prepare` 强制 `confirm_download=true`，并立即返回 `202` 与准备 ID；重复点击会复用当前的 `queued/preparing` 记录，不会启动第二次下载。`GET /api/knowledge/ocr-preparations/{id}` 只回传排队、准备中、已就绪或失败的真实阶段、时间和脱敏消息，不含模型缓存路径、下载 URL 或客户材料。
- 安装与模型准备只在同一次客户确认后的后台线程内执行：先以正在运行后端的 Python 对固定 `requirements-ocr.txt` 执行无交互 `pip install` 与 `pip check`，再调用 `prepare_local_ocr_model(allow_download=True)` 准备模型。导入、解析、索引和 capability 诊断均不能调用任一动作。该短任务不写客户任务历史；服务重启后由 capability 与 ready marker 作为最终事实来源。
- 知识库工作台已在“语义索引”下方增加紧凑的“本地 OCR”能力卡，含活动指示器、状态和确认动作。缺少依赖时显示蓝色 `安装并准备 OCR`；确认框明确固定 PaddleOCR/PaddlePaddle 组件约 850MB 磁盘占用、后续约 29MB 模型、本地处理、不上传材料及“导入不会自动下载”。依赖已安装但模型未准备时，按钮显示 `准备本地 OCR`，只进行后半步模型准备。
- Qt 导入文件筛选已与 K7.3 后端契约同步，支持 PNG/JPG/JPEG；PDF 整理入口仍只允许 PDF。`verify_ocr_preparation_api.py` 覆盖未确认拒绝、诊断不下载、重复点击去重、成功终态和缺少组件失败说明；`verify_ocr_adapter.py`、`verify_ocr_parser_integration.py`、`verify_knowledge_chunking.py`、`verify_backend.py`、`compileall`、`pip check` 和 Qt Debug 构建均通过。

### K7.4.2 页级索引状态与有限恢复（已完成，2026-08-25）

- `OcrAdapter` 现支持 Runtime 内部指定 PDF 页范围；只有首轮已经得到其它成功页、且某页是
  `ocr_page_failed` 时，解析器才对该失败页自动重试一次。`ocr_no_text` 不会重试，重试异常也
  不会撤回首轮成功页，更不会触发下载、重新准备模型或读取 workspace 原件。
- 解析结果和知识库版本仅保存 OCR 总页数、成功页数、未识别页数和重试页数；成功页继续按既有
  “第 N 页 · 区域 M”进入父子分块与 generation。版本/审计/普通 API 不保存 OCR 正文、图片、
  坐标、绝对路径或模型目录。
- 索引任务新增真实 `ocr_recognizing` 阶段。Qt 轮询将其展示为“正在识别扫描材料”，不再用笼统
  的 `parsing` 伪装耗时；终态若仍有未识别页，会明确标为部分完成，同时说明其它成功页仍可检索。
  材料表会显示例如 `OCR 2/3 页可用`，悬停可看到脱敏处理范围说明。
- 新增两条前向 SQLite migration：版本页级计数增列，以及索引任务阶段枚举的受控影子表重建。
  两者保留历史 version/job/generation/性能事实，并在提交前通过 `foreign_key_check`；不会触发
  OCR、扫描受控副本或重建索引。
- `verify_ocr_index_progress.py` 使用合成无文本层三页 PDF 和假 Adapter 覆盖旧任务表升级、真实
  OCR 阶段、一次页级重试、持久部分失败、页/区域来源、无正文审计与材料表元数据；未安装 Paddle、
  未加载模型、未发生网络请求。既有 Adapter、解析、关键词任务和后端回归继续通过。

**2026-08-25 体验修正：**本地 OCR 与语义索引模型属于低频准备项。未准备或正在准备时，工作台只显示
紧凑状态条；准备过程可展开显示真实阶段，准备完成后自动收起，避免持续挤占资料、索引和问答的主工作区。
新建资料库、刷新、提交索引与删除均必须在 Qt 中立即进入可见运行态：按钮显示对应的“创建中/刷新中/
提交中/删除中”，索引运行时锁住重复索引与材料变更，删除则轮询至资料库实际消失后才恢复选择。不得只依赖
一条可能滞后的文字提示让客户猜测操作是否已经受理。

**2026-08-26 状态语义修正：**导入材料只复制受控副本并登记 `queued` 候选版本，绝不解析、切分、
下载模型、创建 Index Job 或把资料库写成 `indexing`；“索引中”只能由客户明确点击建立索引后、后端
真实受理 `queued/running` Job 时出现。Qt 在材料列表返回后必须把临时“正在读取资料库材料”覆盖为
“已导入 N 份材料，尚未建立索引”。新增前向 migration 会把旧版本遗留的“无活动 generation、无运行
Job 却标成 indexing”的资料库修正为待索引，不触及真实索引。软删除仍保留脱敏审计，但完成时会释放原
名称；历史已删除记录也会在下一次同名创建时释放，删除中的同名资料库仍需等待其清理结束。

**下一步是客户真实扫描件验收，而不是继续堆 OCR 能力。** 在客户明确安装可选依赖、确认约 29MB
模型准备后，使用一份扫描 PDF 和一张 JPG/PNG 验证：文本 PDF 仍不进入 OCR、真实阶段可见、缺页
说明可理解、成功页可被检索并能回到页/区域来源。复杂版面、表格/公式、云端视觉和独立 OCR 页面仍
不进入 K7。

为避免把“本机可选组件尚未准备”误判成产品解析失败，新增
`backend/scripts/verify_live_ocr_acceptance.py` 作为人工授权的运行时前置验收。默认运行与未带
`--live-local` 时不会读取任何材料；显式传入 `--live-local --input <PDF/PNG/JPG>` 后，它只将客户
明确选择的材料复制到系统临时目录，复用 `parse_controlled_document` 输出脱敏的路由、页级统计和
来源锚点数量。它不安装依赖、不下载模型、不输出正文/文件名/绝对路径，也不写 workspace、资料库、
任务历史或客户原件。该脚本只证明 Runtime 识别链路；真正客户验收仍必须在 Qt 知识库页确认可见状态、
索引、检索和页/区域来源。

## 验收标准

- 现有可复制文本 PDF/DOCX/Markdown 行为和来源不退化。
- 扫描 PDF 与 PNG/JPG/JPEG 有明确状态；未准备模型、权限不足、超限、损坏、取消和部分页失败均有可行动提示。
- 识别文本可在当前资料库 generation 中检索，证据仍精确回到文件与页码；无可靠文本时 Evidence Gate 必须拒答。
- OCR 完成、失败、取消、更新、删除和重新索引不泄露原图、正文、绝对路径、模型目录或凭据。
- 所有本地探针和真实客户入口都不发生未确认网络请求；Qt 对耗时任务在 100ms 内显示提交状态，且不阻塞窗口。
