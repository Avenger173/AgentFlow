# 知识库 K0 技术决策记录

最后更新：2026-08-21

状态：**K0.1-K0.3 已完成。关键词、向量引擎、中文 Embedding、规模基准与稳定服务契约均已留存证据；K1.1-K1.3 已完成 migration runner、资料库/Profile/脱敏审计事实仓储、受控版本化导入与可追溯父子分块。**

## 决策范围

本记录只决定 AgentFlow 知识库 MVP 的检索底座试验方向，不代表已经向客户交付知识库功能，也不锁定 Embedding、Rerank、LangGraph 或 DeepSeek Harness。

## 已验证证据

### 关键词基线

- 环境：正式后端 Python 3.13.13 自带 SQLite，未新增依赖。
- 脚本：`backend/scripts/verify_knowledge_k0.py`。
- 夹具：4 份脱敏 Markdown、7 类固定问题，覆盖编号、中文关键词、制度条款、语义改写、跨文档、无答案和提示词注入材料。
- 结果：4 个要求关键词基线保证的用例全部在 Top 5 召回；输出为 `required_recall_at_5=4/4`。
- 结论：SQLite FTS5 + BM25 适合作为精确编号、条款、文件名和关键词检索的第一层。连续中文问题必须进入受控中文影子字段，且自然语言问题使用二元词 OR 召回，不能把整句所有词硬做 AND。

### 向量候选

- `qdrant-client==1.19.0` 已在隔离环境 `backend/.k0_qdrant_probe` 安装并执行 `backend/scripts/probe_knowledge_vector_engines.py --engine qdrant`。
- 结果：Qdrant Python Local Mode 的本地落盘、`knowledge_base_id` payload 过滤、关闭后重启回读均通过；不需要 Docker、服务端端口或网络请求。
- 正式后端 `backend/.venv` 未安装 Qdrant，`backend/requirements.txt` 未修改。隔离环境已由 `.gitignore` 排除。

### 本地 Embedding

- 候选：FastEmbed `0.8.0` + `BAAI/bge-small-zh-v1.5`，在 Qdrant 隔离环境中实测通过。模型为 512 维，首次下载后的本地缓存实际占用约 90.81MB。
- 冷启动模型初始化约 31.6 秒；同一缓存热启动约 119ms；4 份夹具嵌入约 75ms。
- 固定中文语义改写题达到 Top 1 `1/1`，并用同一向量写入 Qdrant Local 后再次按 `knowledge_base_id` 过滤检索通过。
- Windows 未开启开发者模式时 Hugging Face 缓存不能使用符号链接，会退化为复制文件并增大磁盘占用。K1 必须在“下载本地模型”操作前显示预计磁盘空间；首次下载不是普通问答的隐式副作用。

### 规模基准

| 引擎与写入方式 | 输入 | 索引耗时 | 查询 P50 / P95 | 结论 |
| --- | --- | ---: | ---: | --- |
| Qdrant Local + `upsert` | 10k x 512，256/批 | 117.1 秒 | 204 / 359ms | 写入过慢，不能作为 K1 默认持久化实现。 |
| Qdrant Local + `upload_collection` | 10k x 512，1024/批 | 超过 2 分钟 | 未完成 | 批量 API 未改善本机 Local Mode 的写入风险。 |
| Chroma PersistentClient | 10k x 512，1024/批 | 5.9 秒 | 33 / 67ms | 通过；索引目录约 29.30MB。 |
| Chroma PersistentClient | 100k x 512，1024/批 | 148.8 秒 | 317 / 453ms | 通过；必须作为后台索引任务，不能阻塞桌面交互。 |

所有基准只使用固定种子合成向量和最小 ID payload，不读取客户资料、不调用 LLM，且确认 `knowledge_base_id` 过滤没有跨库泄漏。

Chroma 基准首轮曾暴露 Windows 文件句柄残留：仅释放 Python 变量不足以删除 PersistentClient 的临时索引目录。探针现已在清理前显式 `close()` Client、释放 collection 并执行垃圾回收，后续 10k 复跑确认不会新增临时目录。K1 的索引版本切换、失败回滚和删除任务必须沿用同一关闭顺序，并把目录清理失败记录为可见错误，而不是静默忽略。

### Chroma 对比

- 官方资料确认 `PersistentClient(path=...)` 可以在本地目录自动持久化，且 Python 3.13 Windows 提供可解析的发行 wheel。
- 本机已在独立环境安装 `chromadb==1.5.9`，完成 10k 和 100k 持久化、过滤与查询基准。启动基准时明确关闭匿名遥测，未产生网络调用。
- Chroma 默认依赖链确实包含 ONNX Runtime、Kubernetes、OpenTelemetry、Tokenizers 等大量组件；这是发行体积与攻击面的代价，不能忽略。但其同机持久化性能显著优于 Qdrant Python Local Mode。

## 当前决定

K1 采用以下分层；K1.1-K1.3 已完成 SQLite 事实仓储、受控版本化导入与可追溯分块，Chroma/FastEmbed 的正式可选依赖和实际索引仍须在后续切片单独接入：

```text
SQLite：知识库、版本、来源、索引任务、审计的产品事实
SQLite FTS5：可重建的关键词/BM25 索引
Chroma PersistentClient：K1 的可重建 Dense 向量索引
FastEmbed bge-small-zh-v1.5：K1 的可选本地中文 Embedding 档位
Adapter：隔离 VectorIndex、Embedding、Rerank 和检索融合
```

Chroma 只允许使用嵌入式 `PersistentClient`，不启动服务器、不开放端口，也不启用匿名遥测。正式依赖要到 K1 开始时才写入 `requirements.txt`，并必须把向量引擎与本地模型包设计为可选安装/可诊断能力，避免主程序在低配设备启动时强制下载模型。

向量 metadata 只包含稳定 ID、版本与最小过滤字段；原文、API Key、绝对路径和权限信息继续以 SQLite/受控文件服务为准。Qdrant Local 继续保留在 `VectorIndexAdapter` 的候选列表，不进入 K1 默认路径；未来若携带独立 Qdrant 服务或服务端部署，再重新基准而非沿用本次 Local Mode 结论。

RerankAdapter 在 K1 保留空实现和能力状态，但默认关闭：当前可商用 FastEmbed 候选约 1.04GB，多语候选约 1.11GB 且为非商业许可证。小型资料库先以 FTS5 + Dense + RRF 运行；只有 K2 评测证明召回噪声造成实际问题时，再让客户明确下载可商用模型或选用云端重排。

## K0.3 契约与迁移决策

- K1 不会先修改 `sqlite.py` 堆表，而是先建立顺序、幂等、带 checksum 的 `schema_migrations` 机制；migration 失败必须停止启动，不能猜测修复。
- 知识库事实使用 SQLite，索引代次使用 immutable generation；候选 generation 经过 FTS/Chroma 回读验证后才在一个 SQLite 事务中切换为活动 generation。
- 文档更新失败时保留旧活动版本；批量导入可部分激活成功文档，但资料库必须标为 `partial_failure` 并明确未覆盖范围。
- 删除首先撤销活动 generation 和缓存，再清理 FTS、Chroma、受控副本与元数据；无法完全清理时保持 `deleting` 并允许安全重试。
- 稳定模型与离线验证位于 `backend/app/schemas/knowledge.py`、`backend/app/services/knowledge_contracts.py`、`backend/scripts/verify_knowledge_contracts.py`；完整迁移设计见 `docs/KNOWLEDGE_BASE_K0_CONTRACT.md`。

## 未决项与 K1 后续门槛

1. 用更丰富的中文、英文和混合资料扩大语义题集；当前 `1/1` 只证明运行链路，不证明模型质量足够。
2. 决定是否需要独立 BM25 库。SQLite FTS5 已满足基线，除非评测证明不足，否则不增加额外稀疏检索依赖。
3. 测试 Chroma 的版本切换、索引重建、单后端进程并发、删除清理和打包后的磁盘/内存行为；100k 基准未测物理内存峰值，不能外推为所有设备都适合。
4. 以 K0 题集确定 Hybrid、RRF、父子扩展和可选 Rerank 的真实阈值；不能从教程复制指标。
5. K0.3 的 Pydantic 契约和 SQLite migration/version 规则已在 K1.1-K1.3 落入 migration runner、基础 Repository、受控副本版本与来源分块。下一步是后台索引和 Chroma；K2 前还需扩大多语言、混合文档的语义质量题集，不能用当前 1/1 结果替代质量结论。

## 运行与复验

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe -X utf8 scripts\verify_knowledge_k0.py
.\.k0_qdrant_probe\Scripts\python.exe -X utf8 scripts\probe_knowledge_vector_engines.py --engine qdrant
.\.k0_qdrant_probe\Scripts\python.exe -X utf8 scripts\probe_knowledge_embeddings.py --with-qdrant
.\.k0_chroma_probe\Scripts\python.exe -X utf8 scripts\benchmark_knowledge_chroma_local.py --chunks 10000
```

除首次下载公开本地 Embedding 权重外，这些命令都不读取客户资料、不调用 LLM，也不访问外部知识库；隔离环境不属于正式发行依赖。

## 资料依据

SQLite 官方 FTS5 文档确认 FTS5 的 BM25 排序和查询语法；Qdrant 官方文档确认 Python Client 可在无服务端的本地模式下持久化向量；Chroma 官方文档确认 `PersistentClient` 的本地持久化能力。实际选择还以本项目 Windows/Python 3.13 试验结果为准。
