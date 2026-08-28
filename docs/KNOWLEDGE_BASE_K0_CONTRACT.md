# 知识库 K0.3 服务契约与迁移设计

最后更新：2026-08-21

状态：**K0.3 已完成设计与离线契约验证；K1.1-K1.6 已按本文件落地 migration、受控副本/逻辑版本、来源可追溯父子分块、FTS/Chroma generation、后台任务、取消/删除恢复与受控 API。K2 已在不改变本契约的前提下完成只读检索核心：只信任活动 ready generation、返回有限来源证据、支持 FTS/Dense 明确降级并通过固定夹具；本文件仍是 K3 引用和删除失效的强制约束。**

## 1. 目的与非目标

K0.3 只固定稳定边界，避免 K1 实现时把 Chroma、文件系统路径或原文内容直接塞进 Qt、任务历史和模型上下文。

- 已交付：Pydantic 契约、索引任务状态守卫、SQLite migration 设计、版本切换和删除失效规则、离线回归脚本。
- 未交付：正式 SQLite 表、受控资料复制、后台 Indexer、Chroma 依赖、API Router、Qt 页面、检索或模型问答。
- 客户不应看到 K0.3 的内部术语；客户侧状态仍使用“排队、解析、索引、就绪、部分失败、失败”。

代码入口：

- `backend/app/schemas/knowledge.py`：服务请求、记录、来源锚点、索引 profile、索引 generation 与 job 契约。
- `backend/app/services/knowledge_contracts.py`：不可逆 job 状态与活动 generation 守卫。
- `backend/scripts/verify_knowledge_contracts.py`：零网络、零模型、零客户资料的 K0.3 回归。

## 2. 不可变事实与派生产物

| 类别 | 唯一事实源 | 可以删除后重建 | 禁止进入普通日志/API |
| --- | --- | --- | --- |
| 资料库、逻辑文档、文档版本、索引 job、活动 generation、来源锚点 | SQLite | 否 | 原文、绝对路径、API Key |
| 受控源副本 | 知识库私有目录 + SQLite 不透明 `storage_ref` | 删除资料库时清理 | 物理路径、源文件字节 |
| 父/子块、FTS5 索引、Chroma collection/metadata | 根据活动文档版本和 profile 构建 | 是 | 向量数值、Chroma 对象、Embedding 缓存路径 |
| 解析/分块/Embedding/检索/Map 缓存 | 版本化 cache key | 是 | 原文、未脱敏模型输入 |

SQLite 记录 ID、哈希、版本、状态和来源位置；向量值始终仅在 Chroma 的可重建存储中存在。任何模型调用只接收受控证据包，不能接收数据库连接、文件系统路径、权限决策或向量对象。

## 3. K1 SQLite Migration 方案

当前数据库仅用 `CREATE TABLE IF NOT EXISTS` 初始化。K1 开始前先将 `backend/app/database/sqlite.py` 升级为小型、顺序、幂等的 migration runner：

1. 在启动锁和同一 SQLite 事务中建立 `schema_migrations(migration_id PRIMARY KEY, applied_at, checksum)`。
2. 每个 migration 使用不可复用 ID，例如 `20260820_knowledge_v1`，执行 DDL 后在同一事务写入已应用记录。
3. 已应用 migration 只校验 checksum，不重复执行；checksum 不一致即停止启动并给出维护错误，不能猜测修复。
4. SQLite `PRAGMA foreign_keys=ON`、WAL 与短连接策略保持现状。迁移函数不读模型、不触网、不写客户原文件。
5. 新增知识库迁移失败时整体回滚；索引目录和 Chroma 不是 SQLite 事务的一部分，必须以 job/generation 状态补偿，不能声称跨存储绝对原子。

首个知识库 migration 的表职责如下；表名在写入时需保持这一职责，不允许把运行时 JSON 当成唯一事实。

| 表/索引 | 关键职责 |
| --- | --- |
| `knowledge_bases` | 资料库元数据、默认 profile、`active_generation_number`、客户可见状态。 |
| `knowledge_documents` | 稳定逻辑文档 ID、显示名、当前活动 version；重名不以文件名作为主键。 |
| `knowledge_document_versions` | 不可变 `source_sha256`、解析 profile、私有 `storage_ref`、状态与脱敏失败摘要。 |
| `knowledge_index_generations` | 不可变文档版本快照、profile 快照、构建/ready/superseded 状态和激活时间。 |
| `knowledge_generation_documents` | generation 与实际参与检索的 document version 多对多映射。 |
| `knowledge_parent_chunks` / `knowledge_child_chunks` | 标题路径、字符范围、来源锚点、父子及邻接关系；原文只在受控 SQLite 内容列中保存。 |
| `knowledge_child_chunks_fts` | 可重建 FTS5/BM25 与中文影子字段，不承担版本事实。 |
| `knowledge_index_jobs` / `knowledge_index_failures` | 真实阶段计数、取消、失败项、重试来源；不用假百分比。 |
| `knowledge_audit_events` | 导入、激活、删除、重试的脱敏审计；禁止保存原文。 |

K1 初期不需要 `knowledge_queries`、`knowledge_citations` 正式表；它们属于 K2/K3，必须复用当前 `KnowledgeSourceAnchor` 契约。

## 4. 索引 generation 与原子切换

一次资料导入、更新或重试不直接改写当前可检索版本，而是按以下顺序产生候选 generation：

1. 后端仅接受既有 workspace 中明确选择的顶层文件名，复制到私有受控目录，生成新 `document_version` 与内容哈希。
2. 创建新的 `index_job` 和 `generation(building)`；旧活动 generation 继续可检索。
3. 解析、父子分块、FTS5 写入和 Chroma 本地写入都以候选 generation 为过滤条件。Chroma 必须使用独立 generation 目录，并在关闭 Client、释放 collection 后再进行目录切换或清理。
4. 对候选 generation 做确定性验证：每个参与版本状态为 `ready`、锚点范围有效、FTS 条目与 Chroma metadata 的 `knowledge_base_id + generation + child_chunk_id` 可回读。
5. 验证通过后，在一个 SQLite 事务中将 generation 标为 `ready`、切换 `knowledge_bases.active_generation_number`、更新各文档活动 version，并将旧 generation 标为 `superseded`。只有这一步之后查询才会看见新版本。
6. 若一个批次中某份新资料失败，成功资料可形成新的活动 generation，资料库状态为 `partial_failure` 并列出未覆盖文件；失败更新原有文档时继续保留旧活动 version。任何失败版本均不可进入活动 generation。

这不是把 Chroma 与 SQLite 包装成虚假的分布式事务。进程在切换间中断时，启动恢复应只信任 SQLite 的活动 generation：

- SQLite 已激活但 Chroma 目录缺失：将资料库标记为失败/不可检索，禁止返回旧缓存，并要求重建。
- Chroma 目录存在但 SQLite 未激活：视为孤儿候选，按 job 状态继续验证或清理。
- FTS5 与 Chroma 任一缺失：K1 只允许明确降级到可证明存在的关键词/受控逐文档搜索，不得对客户伪称 Hybrid 成功。

## 5. 缓存与版本失效

| 缓存 | 版本键 | 必须失效的时机 |
| --- | --- | --- |
| 解析 | `source_sha256 + parser_profile_version` | 源内容或解析器变化、删除。 |
| 分块 | `source_sha256 + splitter_profile_version` | 内容、分块参数或解析结果变化、删除。 |
| Embedding | `child_chunk_hash + embedding_profile_version` | 子块或 Embedding profile 变化、删除。 |
| 检索短缓存 | `knowledge_base_id + active_generation + retrieval_profile_version + normalized_query_sha256` | 活动 generation 切换、资料库删除、检索 profile 变化。 |
| Map-Reduce 子结果 | `parent_chunk_hash + task_contract_version + model_profile` | 父块、任务合同、模型 profile 变化、删除。 |

缓存 key 只保存哈希和稳定版本，不将完整问题或原文写进任务日志。K1 每次激活或删除都必须显式清掉受影响 key；不允许以 TTL 等待旧来源自然过期。

## 6. 删除、取消与重试

删除不是立即 `DELETE` 一条资料库记录，而是可恢复的清理任务：

1. 先将资料库设为 `deleting`，阻止新导入、查询和激活；向正在运行的 job 写取消请求。
2. 失效该资料库所有检索/Map 缓存，撤销活动 generation；此时任何请求都不能返回旧证据。
3. 关闭 Chroma Client 后清理候选与历史 generation 目录，删除 FTS5、父子块、受控副本和版本元数据。
4. 全部清理成功才标记 `deleted` 并保留不含原文的审计；失败则保持 `deleting`、记录脱敏原因，启动时可重试。

取消 Index Job 只取消候选 generation，不影响当前活动 generation。重试永远创建新的 job 和 generation，不能把失败或取消的 job 改回 `running`。

## 7. K1 开工门槛

进入 K1 前必须通过以下命令，并保持既有后端回归：

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe -X utf8 scripts\verify_knowledge_contracts.py
.\.venv\Scripts\python.exe -X utf8 scripts\verify_knowledge_k0.py
.\.venv\Scripts\python.exe -X utf8 scripts\verify_backend.py
```

K1 的实现顺序固定为：migration runner -> SQLite facts/repository -> 受控副本复制和版本 -> parser/parent-child splitter -> 后台 job 与状态恢复 -> Chroma Adapter -> 受控 API/Qt 状态页。以上均已完成并保持离线回归；任何检索、问答或真实模型调用都进入 K2/K3，不能因为资料已入库而提前开放。
