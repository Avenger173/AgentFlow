"""本地知识库 K0.3 的稳定服务契约。

本模块只固定 Knowledge Retrieval Service 与后续 API/Repository 之间交换的元数据。
它不建立数据库表、不读取文件、不启动索引，也不承载客户原文、绝对路径、向量值或凭据；
这些边界能防止 K1 的存储实现细节泄漏到 Qt、任务日志或模型上下文。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import WorkflowRunStatus


KnowledgeBaseStatus = Literal[
    "empty",
    "indexing",
    "ready",
    "partial_failure",
    "failed",
    "deleting",
    "deleted",
]
KnowledgeDocumentVersionStatus = Literal[
    "queued",
    "parsing",
    "parsed",
    "indexing",
    "ready",
    "partial_failure",
    "failed",
    "superseded",
    "deleting",
    "deleted",
]
KnowledgeIndexJobStatus = Literal[
    "queued",
    "running",
    "completed",
    "partial_failure",
    "failed",
    "cancelled",
]
KnowledgeIndexJobStage = Literal[
    "queued",
    "parsing",
    "ocr_recognizing",
    "chunking",
    "keyword_indexing",
    "vector_indexing",
    "verifying",
    "activating",
    "completed",
    "partial_failure",
    "failed",
    "cancelled",
]
KnowledgeIndexGenerationStatus = Literal[
    "building",
    "ready",
    "superseded",
    "deleting",
    "deleted",
    "failed",
]
KnowledgeDocumentType = Literal["text", "pdf", "docx", "image"]
KnowledgeSourceKind = Literal["line", "page", "paragraph", "table", "region", "mixed"]


def _validate_workspace_document_name(value: str) -> str:
    """只接受现有 workspace 服务可识别的顶层文件名。

    K1 导入会先由现有 ``workspace_documents`` 完成受控选择，再复制到知识库私有目录；
    因此契约不能接受 ``C:\\``、网络路径、父目录穿越或任意嵌套路径。
    """

    candidate = value.strip()
    if not candidate:
        raise ValueError("资料文件名不能为空。")
    if "/" in candidate or "\\" in candidate or candidate in {".", ".."}:
        raise ValueError("资料引用只能是已导入 workspace 的顶层文件名。")
    if candidate.startswith("~") or re.match(r"^[A-Za-z]:", candidate):
        raise ValueError("资料引用不能是本机绝对路径。")
    return candidate


class KnowledgeBaseCreateRequest(BaseModel):
    """创建一个本地资料库的最小请求。

    首期没有共享成员、云同步或外部连接器；描述用于客户识别资料范围，不作为模型提示词。
    """

    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)


class KnowledgeDocumentImportRequest(BaseModel):
    """把已受控导入的 workspace 文件复制进指定知识库。

    这里不接收 Base64、客户端绝对路径或文档内容，避免知识库导入成为绕开既有文件边界的
    第二条通道。实际复制、哈希和解析仍只能在后端服务内执行。
    """

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    workspace_document_names: list[str] = Field(min_length=1, max_length=20)

    @field_validator("workspace_document_names")
    @classmethod
    def validate_workspace_document_names(cls, values: list[str]) -> list[str]:
        normalized = [_validate_workspace_document_name(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("同一次导入不能重复选择同一份资料。")
        return normalized


class KnowledgeIndexProfile(BaseModel):
    """一次索引代次必须快照保存的检索实现版本。

    Profile 记录的是可重建派生索引的算法身份，而非模型下载路径或任何向量内容。未来更换
    splitter、Embedding 或向量后端时必须建立新代次，不能让不同版本静默混查。
    """

    profile_id: str = Field(pattern=r"^kb_profile_[a-z0-9_]{3,63}$")
    keyword_backend: Literal["sqlite_fts5"] = "sqlite_fts5"
    keyword_profile_version: str = Field(min_length=3, max_length=80)
    splitter_profile_version: str = Field(min_length=3, max_length=80)
    vector_backend: Literal["chroma_persistent", "qdrant_local", "disabled"] = "chroma_persistent"
    embedding_provider: Literal["fastembed", "disabled"] = "fastembed"
    # 向量索引明确关闭时模型名必须为空；启用时再由下方 Validator 要求非空，避免协议层
    # 把“安全降级”写成一个永远无法构造的状态。
    embedding_model: str = Field(default="BAAI/bge-small-zh-v1.5", max_length=160)
    embedding_profile_version: str = Field(min_length=3, max_length=80)
    rerank_mode: Literal["disabled", "optional"] = "disabled"

    @model_validator(mode="after")
    def validate_vector_configuration(self) -> "KnowledgeIndexProfile":
        # K1 默认 Chroma + FastEmbed；disabled 只给后续诊断和明确降级使用，不能留下看似
        # 正常、实际没有向量能力的半配置。
        if self.vector_backend == "disabled":
            if self.embedding_provider != "disabled":
                raise ValueError("关闭向量索引时 Embedding provider 也必须关闭。")
            if self.embedding_model:
                raise ValueError("关闭向量索引时不能保留 Embedding 模型名。")
        elif self.embedding_provider == "disabled":
            raise ValueError("启用向量索引时必须指定可用的 Embedding provider。")
        elif len(self.embedding_model.strip()) < 3:
            raise ValueError("启用向量索引时必须指定有效的 Embedding 模型名。")
        return self


class KnowledgeBaseRecord(BaseModel):
    """资料库列表与 Repository 返回的脱敏事实记录。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    status: KnowledgeBaseStatus
    default_index_profile_id: str = Field(pattern=r"^kb_profile_[a-z0-9_]{3,63}$")
    active_index_generation: int = Field(default=0, ge=0)
    active_document_version_count: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_active_generation(self) -> "KnowledgeBaseRecord":
        # 只有真正可查询的 ready 状态才承诺存在一个已验证的活动索引快照；indexing 可以
        # 继续读取旧 generation，empty/deleted 则绝不能暴露一个可查询指针。
        if self.status == "ready" and (
            self.active_index_generation < 1 or self.active_document_version_count < 1
        ):
            raise ValueError("就绪资料库必须指向至少一个已验证的活动文档版本。")
        if self.status in {"empty", "deleted"} and self.active_index_generation != 0:
            raise ValueError("空或已删除资料库不能保留活动索引代次。")
        return self


class KnowledgeDocumentRecord(BaseModel):
    """逻辑文档身份及活动版本的最小处理状态，不包含任何材料正文。"""

    document_id: str = Field(pattern=r"^kb_doc_[a-z0-9]{8,32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    display_name: str = Field(min_length=1, max_length=180)
    document_type: KnowledgeDocumentType
    active_version_id: str | None = Field(default=None, pattern=r"^kb_ver_[a-z0-9]{8,32}$")
    active_version_status: KnowledgeDocumentVersionStatus | None = None
    active_ocr_page_count: int = Field(default=0, ge=0)
    active_ocr_completed_page_count: int = Field(default=0, ge=0)
    active_ocr_failed_page_count: int = Field(default=0, ge=0)
    active_ocr_retried_page_count: int = Field(default=0, ge=0)
    active_failure_summary: str = Field(default="", max_length=500)
    created_at: str
    updated_at: str


class KnowledgeDocumentVersionRecord(BaseModel):
    """一次受控副本、解析和索引的不可变版本元数据。"""

    document_version_id: str = Field(pattern=r"^kb_ver_[a-z0-9]{8,32}$")
    document_id: str = Field(pattern=r"^kb_doc_[a-z0-9]{8,32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    version_number: int = Field(ge=1)
    # storage_ref 是服务端私有的不透明标识，不是可传给 Qt、模型或日志的物理路径。
    storage_ref: str = Field(pattern=r"^kb_store_[a-z0-9]{8,48}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_type: KnowledgeDocumentType
    parser_profile_version: str = Field(min_length=3, max_length=80)
    status: KnowledgeDocumentVersionStatus
    extracted_char_count: int = Field(default=0, ge=0)
    parent_chunk_count: int = Field(default=0, ge=0)
    child_chunk_count: int = Field(default=0, ge=0)
    # K7.4.2 仅持久化扫描页的计数事实。没有 OCR 的文本/PDF/DOCX 始终是零，正文、图片、
    # 坐标和模型目录不会出现在版本元数据或普通 API 响应中。
    ocr_page_count: int = Field(default=0, ge=0)
    ocr_completed_page_count: int = Field(default=0, ge=0)
    ocr_failed_page_count: int = Field(default=0, ge=0)
    ocr_retried_page_count: int = Field(default=0, ge=0)
    failure_summary: str = Field(default="", max_length=500)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_terminal_version_facts(self) -> "KnowledgeDocumentVersionRecord":
        if self.status == "ready" and self.child_chunk_count < 1:
            raise ValueError("就绪文档版本必须至少拥有一个已索引子块。")
        if self.status == "failed" and not self.failure_summary:
            raise ValueError("失败文档版本必须保留脱敏失败摘要。")
        if self.ocr_completed_page_count + self.ocr_failed_page_count > self.ocr_page_count:
            raise ValueError("OCR 成功与失败页数不能超过 OCR 总页数。")
        if self.ocr_retried_page_count > self.ocr_page_count:
            raise ValueError("OCR 重试页数不能超过 OCR 总页数。")
        return self


class KnowledgeDocumentImportItem(BaseModel):
    """一次受控文件导入的单项回执。"""

    workspace_document_name: str = Field(min_length=1, max_length=180)
    outcome: Literal["created", "updated", "duplicate"]
    document: KnowledgeDocumentRecord
    document_version: KnowledgeDocumentVersionRecord


class KnowledgeDocumentImportResponse(BaseModel):
    """K1.2 导入结果；所有新版本仅进入待解析队列，尚不能被问答检索。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    items: list[KnowledgeDocumentImportItem] = Field(default_factory=list, min_length=1, max_length=20)
    # 导入只是写入资料库私有副本和候选版本；解析、分块与索引必须等待客户明确点击。
    # 不能把“待建立索引”伪装成后台已经开始工作的 ``indexing``。
    status: Literal["queued"] = "queued"


class KnowledgeDocumentListResponse(BaseModel):
    """资料库详情页未来使用的文档元数据列表，不包含任何文档正文。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    documents: list[KnowledgeDocumentRecord] = Field(default_factory=list)


class KnowledgeBaseListResponse(BaseModel):
    """资料库列表的脱敏回执，不包含文档正文或本机目录。"""

    knowledge_bases: list[KnowledgeBaseRecord] = Field(default_factory=list)


class KnowledgeIndexJobListResponse(BaseModel):
    """资料库索引任务列表，供状态页恢复真实阶段而非猜测百分比。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    jobs: list["KnowledgeIndexJobRecord"] = Field(default_factory=list)


class KnowledgeEmbeddingPrepareRequest(BaseModel):
    """首次本地模型下载的明确客户确认。"""

    confirm_download: Literal[True]


class KnowledgeEmbeddingPrepareResponse(BaseModel):
    """本地 Embedding 准备回执；不包含缓存绝对路径。"""

    status: Literal["ready"]
    model: str
    dimension: int = Field(gt=0)
    message: str


KnowledgeOcrPreparationStatus = Literal["queued", "preparing", "ready", "failed"]


class KnowledgeOcrCapabilityResponse(BaseModel):
    """K7.4 的本地 OCR 能力快照；仅包含可安全展示的准备状态。"""

    paddleocr_available: bool
    model_initialized: bool
    profile: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=240)


class KnowledgeOcrPrepareRequest(BaseModel):
    """客户确认本地模型准备的双层确认字段。"""

    confirm_download: Literal[True]


class KnowledgeOcrPreparationResponse(BaseModel):
    """后台 OCR 模型准备的真实阶段，不伪造百分比或暴露本机缓存路径。"""

    preparation_id: str = Field(pattern=r"^ocr_prepare_[a-z0-9]{8,32}$")
    status: KnowledgeOcrPreparationStatus
    model_profile: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=240)
    started_at: str = Field(min_length=1, max_length=64)
    completed_at: str = Field(default="", max_length=64)


class KnowledgeSourceAnchor(BaseModel):
    """回答、审查和检索证据都复用的稳定来源锚点。"""

    document_id: str = Field(pattern=r"^kb_doc_[a-z0-9]{8,32}$")
    document_version_id: str = Field(pattern=r"^kb_ver_[a-z0-9]{8,32}$")
    source_kind: KnowledgeSourceKind
    source_locator: str = Field(min_length=1, max_length=240)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    heading_path: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_char_range(self) -> "KnowledgeSourceAnchor":
        if self.end_char <= self.start_char:
            raise ValueError("来源锚点结束位置必须大于起始位置。")
        return self


class KnowledgeIndexGenerationRecord(BaseModel):
    """一个可查询的不可变索引快照，不保存 FTS/Chroma 的物理目录。"""

    index_generation_id: str = Field(pattern=r"^kb_gen_[a-z0-9]{8,32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    generation_number: int = Field(ge=1)
    status: KnowledgeIndexGenerationStatus
    index_profile: KnowledgeIndexProfile
    document_version_ids: list[str] = Field(default_factory=list, max_length=500)
    created_at: str
    activated_at: str = ""
    failure_summary: str = Field(default="", max_length=500)

    @field_validator("document_version_ids")
    @classmethod
    def validate_document_version_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("同一索引代次不能重复关联文档版本。")
        for value in values:
            if not re.fullmatch(r"kb_ver_[a-z0-9]{8,32}", value):
                raise ValueError("索引代次包含无效的文档版本标识。")
        return values

    @model_validator(mode="after")
    def validate_generation_activation(self) -> "KnowledgeIndexGenerationRecord":
        if self.status == "ready" and (not self.document_version_ids or not self.activated_at):
            raise ValueError("可用索引代次必须包含版本快照和激活时间。")
        if self.status == "failed" and not self.failure_summary:
            raise ValueError("失败索引代次必须保留脱敏失败摘要。")
        return self


class KnowledgeIndexJobRecord(BaseModel):
    """后台索引任务的可恢复状态与无正文性能事实，不使用伪百分比。"""

    index_job_id: str = Field(pattern=r"^kb_job_[a-z0-9]{8,32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    target_generation_number: int = Field(ge=1)
    status: KnowledgeIndexJobStatus
    stage: KnowledgeIndexJobStage
    total_document_count: int = Field(ge=1)
    parsed_document_count: int = Field(default=0, ge=0)
    indexed_document_count: int = Field(default=0, ge=0)
    failed_document_count: int = Field(default=0, ge=0)
    # 解析成功不等于本次重新解析：未改变的 ready/parsed 版本会复用已持久化的受控分块。
    # 这个计数让性能诊断能够区分“新材料解析慢”和“generation 重建复用已有材料”。
    reused_parsed_document_count: int = Field(default=0, ge=0)
    # 只保存当前索引任务实际花费的阶段时长，不保存文件路径、正文、向量或模型调用内容。
    parse_and_chunk_elapsed_ms: int = Field(default=0, ge=0)
    vector_index_elapsed_ms: int = Field(default=0, ge=0)
    keyword_index_elapsed_ms: int = Field(default=0, ge=0)
    total_elapsed_ms: int = Field(default=0, ge=0)
    # K5.6 仅计量当前 generation 实际写入的向量来源。向量仍只保存在 Chroma 私有目录，
    # SQLite 不保存向量值、正文或可反推内容的缓存键。
    vector_indexed_child_count: int = Field(default=0, ge=0)
    reused_vector_child_count: int = Field(default=0, ge=0)
    embedded_child_count: int = Field(default=0, ge=0)
    failure_summaries: list[str] = Field(default_factory=list, max_length=20)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_job_progress_facts(self) -> "KnowledgeIndexJobRecord":
        if self.parsed_document_count > self.total_document_count:
            raise ValueError("已解析文档数不能超过任务总数。")
        if self.reused_parsed_document_count > self.parsed_document_count:
            raise ValueError("复用的已解析文档数不能超过已解析文档数。")
        if self.indexed_document_count + self.failed_document_count > self.total_document_count:
            raise ValueError("已索引与失败文档数不能超过任务总数。")
        if self.reused_vector_child_count + self.embedded_child_count != self.vector_indexed_child_count:
            raise ValueError("复用与新嵌入子块数必须等于本次写入的向量子块数。")
        if self.status == "completed":
            if self.stage != "completed" or self.indexed_document_count != self.total_document_count:
                raise ValueError("完成任务必须完成全部文档的索引。")
            if self.failed_document_count or self.failure_summaries:
                raise ValueError("完成任务不能同时包含失败文档。")
        if self.status == "partial_failure" and not self.failure_summaries:
            raise ValueError("部分失败任务必须提供脱敏失败摘要。")
        return self


KnowledgePerformanceTier = Literal["low", "medium", "high"]
KnowledgePerformanceState = Literal["insufficient_data", "stable", "attention"]
KnowledgeStorageState = Literal["sufficient", "attention", "low"]
KnowledgeRuntimeWorkKind = Literal["index", "deep_task"]
KnowledgeRuntimeQueueItemState = Literal["active", "waiting"]


class KnowledgeRuntimeQueueItem(BaseModel):
    """一个进程内知识库重任务的队列快照，不包含资料库或客户内容。"""

    work_id: str = Field(min_length=1, max_length=80)
    work_kind: KnowledgeRuntimeWorkKind
    state: KnowledgeRuntimeQueueItemState
    queue_position: int = Field(ge=1)
    waited_ms: int = Field(default=0, ge=0)


class KnowledgeRuntimeQueueSnapshot(BaseModel):
    """索引与深度任务的本机运行队列事实。

    队列故意只在当前后端进程中存在：任务本身仍由 SQLite checkpoint 恢复；服务重启时不会
    把旧内存顺序伪装成可恢复的执行承诺。每条重负载通道最多一个活动任务，避免同类解析、
    向量构建或深度链路同时占用 Windows 文件句柄与本机资源。
    """

    active_items: list[KnowledgeRuntimeQueueItem] = Field(default_factory=list, max_length=2)
    waiting_items: list[KnowledgeRuntimeQueueItem] = Field(default_factory=list, max_length=32)
    index_active_limit: int = Field(default=1, ge=1, le=1)
    deep_task_active_limit: int = Field(default=1, ge=1, le=1)
    max_active_work_kinds: int = Field(default=2, ge=1, le=2)
    process_local: bool = True
    message: str = Field(min_length=1, max_length=600)


class KnowledgePerformanceObservation(BaseModel):
    """某条无正文性能观察窗口的聚合结果。"""

    sample_count: int = Field(ge=0, le=48)
    median_elapsed_ms: int | None = Field(default=None, ge=0)
    p95_elapsed_ms: int | None = Field(default=None, ge=0)
    source: Literal[
        "persisted_index_jobs",
        "persisted_task_metrics",
        "process_local_runtime_samples",
        "mixed_runtime_metrics",
        "not_available",
    ]


class KnowledgePerformanceProfileResponse(BaseModel):
    """K5.8 的本机性能建议，供知识库页和后续运行状态面板按需读取。"""

    resource_tier: KnowledgePerformanceTier
    performance_state: KnowledgePerformanceState
    logical_cpu_count: int = Field(ge=1)
    data_storage_free_gib: float = Field(ge=0)
    data_storage_state: KnowledgeStorageState
    index_observation: KnowledgePerformanceObservation
    retrieval_observation: KnowledgePerformanceObservation
    deep_task_observation: KnowledgePerformanceObservation
    runtime_queue: KnowledgeRuntimeQueueSnapshot
    recommendations: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    privacy_notice: str = Field(min_length=1, max_length=400)


class KnowledgeRetrievalCacheIdentity(BaseModel):
    """本地检索短缓存的版本化身份，不保存问题正文或检索片段。

    ``top_k`` 也属于检索结果的合同。省略它会让一次较小预算的调用错误复用到较大
    预算，进而把“缓存命中”变成悄悄少给证据的功能回归。
    """

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    active_index_generation: int = Field(ge=1)
    retrieval_profile_version: str = Field(min_length=3, max_length=80)
    normalized_query_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    top_k: int = Field(ge=1, le=8)


class KnowledgeRetrievalRequest(BaseModel):
    """K2 受控检索请求；调用方只能指定资料库、问题与有限结果预算。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    query: str = Field(min_length=1, max_length=800)
    top_k: int = Field(default=5, ge=1, le=8)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        # FTS 语法由服务端从普通文本生成；这里保留语义内容但压缩无意义空白，避免同一个
        # 问题以不同空格形态污染后续检索诊断或缓存身份。
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("检索问题不能为空。")
        return normalized


class KnowledgeRetrievalEvidence(BaseModel):
    """一个可送入后续 Evidence Gate 的父块证据包，尚不代表模型回答或客户结论。"""

    child_chunk_id: str = Field(pattern=r"^kb_child_[a-z0-9]{8,32}$")
    parent_chunk_id: str = Field(pattern=r"^kb_parent_[a-z0-9]{8,32}$")
    document_id: str = Field(pattern=r"^kb_doc_[a-z0-9]{8,32}$")
    document_version_id: str = Field(pattern=r"^kb_ver_[a-z0-9]{8,32}$")
    document_name: str = Field(min_length=1, max_length=180)
    source: KnowledgeSourceAnchor
    heading_path: list[str] = Field(default_factory=list, max_length=12)
    # 原文只在 Retrieval Service 与后续受控模型证据包之间传递，不会写入普通列表、审计或
    # 模型配置接口。上限防止一条异常父块占满后续上下文预算。
    parent_content: str = Field(min_length=1, max_length=24_000)
    matched_content: str = Field(min_length=1, max_length=12_000)
    retrieval_score: float = Field(ge=0.0)
    retrieval_channels: list[Literal["keyword", "dense"]] = Field(min_length=1, max_length=2)


class KnowledgeRetrievalDiagnostics(BaseModel):
    """K2 的可解释检索事实；不携带向量、路径、完整 FTS 查询或模型内部状态。"""

    mode: Literal["keyword", "hybrid", "keyword_fallback", "no_result"]
    active_index_generation: int = Field(ge=1)
    keyword_candidate_count: int = Field(ge=0)
    dense_candidate_count: int = Field(ge=0)
    parent_deduplicated_count: int = Field(ge=0)
    # 只公开本进程内已实际发生的短缓存事实；不回显缓存键、问题摘要哈希、TTL 或正文。
    local_cache_state: Literal["hit", "miss"] = "miss"
    local_cache_age_ms: int | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=8)


class KnowledgeRetrievalResult(BaseModel):
    """K2 内部稳定返回；K3 才会把其中证据经过引用校验后展示给客户。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    query: str = Field(min_length=1, max_length=800)
    evidences: list[KnowledgeRetrievalEvidence] = Field(default_factory=list, max_length=8)
    diagnostics: KnowledgeRetrievalDiagnostics


class KnowledgeAnswerSource(BaseModel):
    """K3 对客户与回答模型使用的最小来源卡，不暴露父块全文或存储实现。"""

    source_id: str = Field(pattern=r"^kb_src_[1-8]$")
    document_id: str = Field(pattern=r"^kb_doc_[a-z0-9]{8,32}$")
    document_version_id: str = Field(pattern=r"^kb_ver_[a-z0-9]{8,32}$")
    document_name: str = Field(min_length=1, max_length=180)
    source: KnowledgeSourceAnchor
    heading_path: list[str] = Field(default_factory=list, max_length=12)
    excerpt: str = Field(min_length=1, max_length=900)
    retrieval_channels: list[Literal["keyword", "dense"]] = Field(min_length=1, max_length=2)


class KnowledgeEvidenceGateResult(BaseModel):
    """K3 Evidence Gate 的确定性结论；它不是模型回答，也不声明资料事实。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    query: str = Field(min_length=1, max_length=800)
    active_index_generation: int = Field(ge=1)
    evidence_state: Literal["sufficient", "partial", "insufficient"]
    required_document_count: int = Field(ge=1, le=2)
    covered_document_count: int = Field(ge=0)
    sources: list[KnowledgeAnswerSource] = Field(default_factory=list, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_evidence_state(self) -> "KnowledgeEvidenceGateResult":
        if self.evidence_state == "sufficient" and self.covered_document_count < self.required_document_count:
            raise ValueError("证据充分状态必须满足最小资料覆盖要求。")
        if self.evidence_state == "insufficient" and self.sources:
            raise ValueError("证据不足状态不能携带可供模型回答的来源。")
        return self


class KnowledgeAnswerClaim(BaseModel):
    """未来回答模型必须交付的关键结论；每条均显式绑定 Gate 颁发的 source_id。"""

    claim_id: str = Field(pattern=r"^kb_claim_[1-9][0-9]{0,1}$")
    statement: str = Field(min_length=1, max_length=1200)
    source_ids: list[str] = Field(min_length=1, max_length=4)


class KnowledgeTrustedAnswer(BaseModel):
    """K3 最终回答契约草案；服务层还会二次校验 claim 引用只能来自本次 Gate。"""

    answer_markdown: str = Field(min_length=1, max_length=12_000)
    claims: list[KnowledgeAnswerClaim] = Field(min_length=1, max_length=12)
    source_ids: list[str] = Field(min_length=1, max_length=8)
    evidence_state: Literal["sufficient", "partial"]
    warnings: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_internal_citations(self) -> "KnowledgeTrustedAnswer":
        """先收紧回答自身的引用结构，具体来源是否获 Gate 批准仍由服务层检查。"""

        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("回答中的 claim_id 不能重复。")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("回答的 source_ids 不能重复。")

        claim_source_ids: list[str] = []
        for claim in self.claims:
            if len(claim.source_ids) != len(set(claim.source_ids)):
                raise ValueError("单条结论中的 source_ids 不能重复。")
            claim_source_ids.extend(claim.source_ids)
        if set(claim_source_ids) != set(self.source_ids):
            raise ValueError("回答来源必须与各条结论实际引用的来源完全一致。")
        return self


class KnowledgeAnswerRequest(KnowledgeRetrievalRequest):
    """K3 的受控问答请求；检索、Gate 与回答都绑定同一份普通问题。"""


KnowledgeContextStage = Literal["knowledge_answer", "deep_map", "deep_reduce"]
KnowledgeContextRoute = Literal["retrieval_evidence", "map_reduce"]
KnowledgeContextBudgetState = Literal["within_budget", "over_budget"]
KnowledgeLongContextProviderState = Literal["not_checked", "not_confirmed", "confirmed_not_enabled"]


class KnowledgeContextRouteDecision(BaseModel):
    """一次知识库模型调用的无正文上下文路由事实。

    K5.7 不把供应商声明的长窗口当作“可以直接塞整库正文”的许可证。这个模型只记录本次
    实际准备发送的系统指令和受控用户消息字符数、产品内部字符预算，以及为何仍选择检索或
    Map-Reduce。真实 token 和缓存命中仍只能从 Provider 响应 usage 观察。
    """

    stage: KnowledgeContextStage
    route: KnowledgeContextRoute
    route_reason: Literal["interactive_question", "chapter_checkpoint", "summary_checkpoint"]
    model_input_char_count: int = Field(ge=0)
    model_input_char_budget: int = Field(gt=0)
    budget_state: KnowledgeContextBudgetState
    # 仅在当前 Runtime 的 Provider + 模型组合已有本地核验记录时返回；未知不等于没有长窗口。
    confirmed_model_context_window_tokens: int | None = Field(default=None, ge=1)
    provider_long_context_state: KnowledgeLongContextProviderState
    # 首期无论 Provider 声称多长窗口，均保持这一事实为 false；未来的直读路线必须另行评估、
    # 立项和回归，不能因为配置中出现大数字而绕过 K2/K3/K4 的证据与检查点边界。
    long_context_direct_execution: bool = False
    cache_usage_policy: Literal["response_usage_only"] = "response_usage_only"

    @model_validator(mode="after")
    def validate_budget_and_long_context_boundary(self) -> "KnowledgeContextRouteDecision":
        expected_state: KnowledgeContextBudgetState = (
            "within_budget" if self.model_input_char_count <= self.model_input_char_budget else "over_budget"
        )
        if self.budget_state != expected_state:
            raise ValueError("上下文预算状态必须与实际字符计数一致。")
        if self.long_context_direct_execution:
            raise ValueError("当前知识库版本不允许直接长上下文执行。")
        if self.provider_long_context_state == "confirmed_not_enabled" and (
            self.confirmed_model_context_window_tokens is None
        ):
            raise ValueError("已确认长窗口状态必须携带已核验的窗口大小。")
        return self


class KnowledgeAnswerResponse(BaseModel):
    """K3 回答入口的稳定结果。

    Gate 未通过、模型不可用或模型输出失效都以明确状态返回；不会用模型猜测填补空答案。
    """

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    query: str = Field(min_length=1, max_length=800)
    status: Literal["completed", "insufficient_evidence", "failed"]
    stop_reason: str = Field(min_length=1, max_length=120)
    evidence_gate: KnowledgeEvidenceGateResult
    # 检索诊断只保留当前活动 generation、通道与有限计数。它既能让客户理解降级状态，也让
    # 统一任务历史可审计本轮到底走了关键词还是 Hybrid，而不会写入向量或父块全文。
    retrieval_diagnostics: KnowledgeRetrievalDiagnostics
    # 只在 Gate 通过并已构造模型输入后出现；证据不足时不构造模型上下文，也就没有路由事实。
    context_route: KnowledgeContextRouteDecision | None = None
    answer: KnowledgeTrustedAnswer | None = None
    message: str = Field(min_length=1, max_length=1_200)
    model_turn_count: int = Field(ge=0, le=2)
    # 只记录本次真正解析出的脱敏路由。资料不足、本地夹具或旧任务保持空列表，不能由 UI
    # 根据当前模型配置反推或补写。
    model_routes: list[ModelRouteAuditSnapshot] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_answer_status(self) -> "KnowledgeAnswerResponse":
        if self.status == "completed" and self.answer is None:
            raise ValueError("完成状态必须包含已验证回答。")
        if self.status != "completed" and self.answer is not None:
            raise ValueError("未完成状态不能携带未验证回答。")
        return self


class KnowledgeAnswerTaskStartResponse(BaseModel):
    """K3 后台问答的受理回执；正文和来源只在终态结果通过验证后返回。"""

    task_id: str = Field(pattern=r"^task_kb_[a-z0-9]{8,32}$")
    status: Literal["queued"] = "queued"


class KnowledgeAnswerTaskResultResponse(BaseModel):
    """可从统一任务历史恢复的知识库问答终态。"""

    task_id: str = Field(pattern=r"^task_kb_[a-z0-9]{8,32}$")
    status: WorkflowRunStatus
    summary: str = Field(default="", max_length=1_200)
    message: str = Field(default="", max_length=1_200)
    result: KnowledgeAnswerResponse | None = None


# ``audit`` 只用于读取早期已入库任务，新的客户入口不再创建泛化审查任务。真正的审查维度
# 由“全库深度总结”的内部检查项按目标启用，避免把含义重叠的动作堆成多个产品按钮。
KnowledgeDeepTaskKind = Literal["summary", "comparison", "audit"]


class KnowledgeDeepTaskRequest(BaseModel):
    """K4 深度任务的最小输入。

    深度总结默认覆盖当前活动索引的所有资料；资料对照必须由客户明确选择两份或以上资料，
    避免系统在整个资料库中猜测“应该比较什么”。``audit`` 保留为历史兼容值，新界面不提供。
    """

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    task_kind: KnowledgeDeepTaskKind
    task_goal: str = Field(min_length=1, max_length=800)
    document_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("task_goal")
    @classmethod
    def normalize_task_goal(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("深度任务目标不能为空。")
        return normalized

    @field_validator("document_ids")
    @classmethod
    def normalize_document_ids(cls, value: list[str]) -> list[str]:
        """保留客户选中的稳定文档 ID 顺序，并拒绝重复对照列。"""

        normalized = [item.strip() for item in value if item and item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("资料对照不能重复选择同一份资料。")
        if any(not re.fullmatch(r"kb_doc_[a-z0-9]{8,32}", item) for item in normalized):
            raise ValueError("资料对照包含无效的资料标识。")
        return normalized

    @model_validator(mode="after")
    def validate_task_input(self) -> "KnowledgeDeepTaskRequest":
        if self.task_kind == "comparison" and len(self.document_ids) < 2:
            raise ValueError("资料对照请至少选择两份资料。")
        if self.task_kind != "comparison" and self.document_ids:
            raise ValueError("全库深度总结不需要额外选择资料范围。")
        return self


class KnowledgeDeepTaskMapUnit(BaseModel):
    """一个可恢复 Map 单元的元数据快照，不携带父块正文。

    执行时会由服务端按这份稳定 ID 再次受控回读父块；把内容保存在 checkpoint 或普通 API
    回执会让大任务既难恢复，也会把客户材料扩散到不必要的审计面。
    """

    map_unit_id: str = Field(pattern=r"^kb_map_[a-f0-9]{16}$")
    parent_chunk_id: str = Field(pattern=r"^kb_parent_[a-z0-9]{8,32}$")
    document_id: str = Field(pattern=r"^kb_doc_[a-z0-9]{8,32}$")
    document_version_id: str = Field(pattern=r"^kb_ver_[a-z0-9]{8,32}$")
    document_name: str = Field(min_length=1, max_length=180)
    parent_ordinal: int = Field(ge=1)
    source: KnowledgeSourceAnchor
    heading_path: list[str] = Field(default_factory=list, max_length=12)
    character_count: int = Field(gt=0, le=24_000)


class KnowledgeDeepTaskScope(BaseModel):
    """K4 Map-Reduce 运行前冻结的活动 generation 与覆盖清单。"""

    knowledge_base_id: str = Field(pattern=r"^kb_[a-z0-9]{8,32}$")
    task_kind: KnowledgeDeepTaskKind
    task_goal: str = Field(min_length=1, max_length=800)
    index_generation_id: str = Field(pattern=r"^kb_gen_[a-z0-9]{8,32}$")
    active_index_generation: int = Field(ge=1)
    # ``selected_document_ids`` 为空代表全库深度总结；对照任务会固定客户选择的资料及其顺序，
    # 这个顺序同时就是最终表格列顺序。范围本身不再带 24/500 这样的产品截断上限。
    selected_document_ids: list[str] = Field(default_factory=list)
    covered_document_count: int = Field(ge=1)
    available_document_count: int = Field(default=0, ge=0)
    available_map_count: int = Field(default=0, ge=0)
    # ``goal_focused`` 仅用于读取 K4.9 的历史 checkpoint；K4.15 起新任务只会冻结全库或客户
    # 选择的对照资料，不再创建按 24 章节裁剪的范围。
    scope_mode: Literal["complete", "selected_documents", "goal_focused"] = "complete"
    scope_notice: str = Field(default="", max_length=700)
    map_units: list[KnowledgeDeepTaskMapUnit] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope_coverage(self) -> "KnowledgeDeepTaskScope":
        unit_ids = [unit.map_unit_id for unit in self.map_units]
        parent_ids = [unit.parent_chunk_id for unit in self.map_units]
        if len(unit_ids) != len(set(unit_ids)) or len(parent_ids) != len(set(parent_ids)):
            raise ValueError("深度任务范围不能重复包含同一 Map 单元或父块。")
        if len({unit.document_version_id for unit in self.map_units}) != self.covered_document_count:
            raise ValueError("深度任务覆盖文档数必须与 Map 单元中的活动版本一致。")
        if self.available_document_count and self.available_document_count < self.covered_document_count:
            raise ValueError("资料库可用文档数不能小于本次冻结范围。")
        if self.available_map_count and self.available_map_count < len(self.map_units):
            raise ValueError("资料库可用章节数不能小于本次冻结范围。")
        if self.scope_mode == "complete" and self.available_map_count and self.available_map_count != len(self.map_units):
            raise ValueError("完整范围的可用章节数必须与冻结章节数一致。")
        if self.task_kind == "comparison":
            if len(self.selected_document_ids) < 2:
                raise ValueError("资料对照范围必须包含至少两份资料。")
            if len(self.selected_document_ids) != len(set(self.selected_document_ids)):
                raise ValueError("资料对照范围不能重复包含同一份资料。")
            if set(self.selected_document_ids) != {unit.document_id for unit in self.map_units}:
                raise ValueError("资料对照范围与冻结章节的资料集合不一致。")
        elif self.selected_document_ids:
            raise ValueError("非资料对照任务不能保存额外的选中资料范围。")
        return self


class KnowledgeDeepMapFinding(BaseModel):
    """一个章节级事实或风险点；首期只允许引用当前 Map 单元。"""

    finding_id: str = Field(pattern=r"^kb_map_finding_[1-9][0-9]{0,1}$")
    statement: str = Field(min_length=1, max_length=900)
    source_ids: list[str] = Field(min_length=1, max_length=1)


class KnowledgeDeepMapDraft(BaseModel):
    """模型在单章节 Map 回合中实际需要填写的最小草稿。

    ``map_unit_id``、来源数组和发现编号均能由 Runtime 从冻结 scope 确定。让模型重复填写这些
    动态值会把“理解章节”的一次调用变成字符串复制考试，真实 Provider 很容易因漏填或 ID 拼错
    而失败。草稿只收集可读内容，服务端会在写 checkpoint 前补齐并重新验证完整结果。
    """

    summary: str = Field(min_length=1, max_length=2_000)
    findings: list[str] = Field(default_factory=list, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_payload(cls, value: object) -> object:
        """接住常见字段别名，避免 Provider 的表达差异无谓中断一个章节。"""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        findings = payload.get("findings", payload.get("key_findings", payload.get("points", [])))
        summary = payload.get("summary", payload.get("overview", payload.get("conclusion", "")))
        if not isinstance(summary, str) or not summary.strip():
            # 模型若已经给出了有效发现，直接将这些已返回的内容拼成小结；不补写任何新事实。
            candidate_texts = _semantic_text_items(findings, limit=4, item_limit=360)
            if candidate_texts:
                summary = "；".join(candidate_texts)
        payload["summary"] = summary
        payload["findings"] = findings
        payload["warnings"] = payload.get("warnings", payload.get("notes", payload.get("caveats", [])))
        return payload

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        """把只有空白的模型小结当作无效输出，阻止其进入可恢复检查点。"""

        normalized = value.strip()[:2_000]
        if not normalized:
            raise ValueError("章节小结不能为空白文本。")
        return normalized

    @field_validator("findings", mode="before")
    @classmethod
    def normalize_finding_shapes(cls, value: object) -> list[str]:
        """兼容模型偶发返回的旧对象形态，只保留其 statement 文字。"""

        return _semantic_text_items(value, limit=8, item_limit=900)

    @field_validator("warnings", mode="before")
    @classmethod
    def normalize_warning_shapes(cls, value: object) -> list[str]:
        """允许单条字符串或数组，避免非关键提示字段阻断整个章节检查点。"""

        return _semantic_text_items(value, limit=6, item_limit=480)


class KnowledgeDeepMapResult(BaseModel):
    """K4 Map 节点的可恢复最小结果。

    每个模型回合只读取一个父章节，所有输出必须回指该章节的 ``map_unit_id``。这样 Reduce
    阶段只能合并已知来源的小结，不能把模型臆测出的跨章节关系当成已核验结论。
    """

    map_unit_id: str = Field(pattern=r"^kb_map_[a-f0-9]{16}$")
    summary: str = Field(min_length=1, max_length=1_200)
    findings: list[KnowledgeDeepMapFinding] = Field(default_factory=list, max_length=6)
    source_ids: list[str] = Field(min_length=1, max_length=1)
    warnings: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_local_sources(self) -> "KnowledgeDeepMapResult":
        expected_sources = [self.map_unit_id]
        if self.source_ids != expected_sources:
            raise ValueError("Map 结果只能引用当前章节的 map_unit_id。")
        if any(finding.source_ids != expected_sources for finding in self.findings):
            raise ValueError("Map 发现项只能引用当前章节的 map_unit_id。")
        return self


class KnowledgeDeepTaskMapRunResponse(BaseModel):
    """K4.2 Map 阶段回执；它不是面向客户的最终 Reduce 报告。"""

    task_id: str = Field(pattern=r"^task_k4_[a-z0-9]{8,32}$")
    status: WorkflowRunStatus
    completed_map_count: int = Field(ge=0)
    failed_map_unit_ids: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=1_200)


class KnowledgeDeepReduceFinding(BaseModel):
    """Reduce 阶段可进入最终报告的一条聚合结论，必须保留 Map 来源集合。"""

    finding_id: str = Field(pattern=r"^kb_reduce_finding_[1-9][0-9]{0,1}$")
    statement: str = Field(min_length=1, max_length=1_200)
    # 该数组只由 Runtime 从当前冻结范围写入。完整资料库会通过分层 Reduce 汇总，但最终结论
    # 仍必须保留其实际覆盖的章节集合，不能因为历史展示上限而截断来源范围。
    source_ids: list[str] = Field(min_length=1)


class KnowledgeDeepReduceConflict(BaseModel):
    """跨章节存在差异时的保留项；Reduce 不能静默选择其中一个版本。"""

    conflict_id: str = Field(pattern=r"^kb_reduce_conflict_[1-9][0-9]{0,1}$")
    topic: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=1_000)
    source_ids: list[str] = Field(min_length=2)


class KnowledgeDeepComparisonDraftRow(BaseModel):
    """模型为资料对照表提供的一行语义草稿，列顺序由冻结 scope 决定。"""

    dimension: str = Field(min_length=1, max_length=120)
    values: list[str] = Field(min_length=1, max_length=12)
    conclusion: str = Field(default="", max_length=360)

    @field_validator("dimension", "conclusion")
    @classmethod
    def normalize_row_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("values", mode="before")
    @classmethod
    def normalize_row_values(cls, value: object) -> list[str]:
        return _semantic_text_items(value, limit=12, item_limit=520)


class KnowledgeDeepComparisonRow(BaseModel):
    """客户可读对照表的一行；每个值与 ``scope.selected_document_ids`` 同序。"""

    dimension: str = Field(min_length=1, max_length=120)
    values: list[str] = Field(min_length=2)
    conclusion: str = Field(default="", max_length=360)
    source_ids: list[str] = Field(min_length=2)


class KnowledgeDeepReduceDraftConflict(BaseModel):
    """Reduce 模型只表达待确认事项，不复制内部来源编号。"""

    topic: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="before")
    @classmethod
    def normalize_shape(cls, value: object) -> object:
        if isinstance(value, str):
            return {"topic": "待确认差异", "description": value}
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        payload["topic"] = payload.get("topic", payload.get("title", "待确认差异"))
        payload["description"] = payload.get(
            "description",
            payload.get("statement", payload.get("detail", payload.get("content", ""))),
        )
        return payload

    @field_validator("topic", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("待确认事项不能为空白文本。")
        return normalized


class KnowledgeDeepReduceDraft(BaseModel):
    """一次 Reduce 模型回合的语义草稿；来源和编号一律由 Runtime 投影。"""

    overview: str = Field(min_length=1, max_length=2_000)
    findings: list[str] = Field(default_factory=list, max_length=12)
    conflicts: list[KnowledgeDeepReduceDraftConflict] = Field(default_factory=list, max_length=8)
    comparison_rows: list[KnowledgeDeepComparisonDraftRow] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_payload(cls, value: object) -> object:
        """兼容不同模型的语义字段名，动态 ID 绝不进入模型契约。"""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        findings = payload.get("findings", payload.get("key_findings", payload.get("points", [])))
        overview = payload.get("overview", payload.get("summary", payload.get("conclusion", "")))
        if not isinstance(overview, str) or not overview.strip():
            candidate_texts = _semantic_text_items(findings, limit=4, item_limit=420)
            if candidate_texts:
                overview = "；".join(candidate_texts)
        payload["overview"] = overview
        payload["findings"] = findings
        payload["conflicts"] = payload.get("conflicts", payload.get("open_questions", payload.get("issues", [])))
        payload["comparison_rows"] = payload.get(
            "comparison_rows",
            payload.get("table_rows", payload.get("comparison_table", [])),
        )
        payload["warnings"] = payload.get("warnings", payload.get("notes", payload.get("caveats", [])))
        return payload

    @field_validator("overview")
    @classmethod
    def normalize_overview(cls, value: str) -> str:
        normalized = value.strip()[:2_000]
        if not normalized:
            raise ValueError("Reduce 概述不能为空白文本。")
        return normalized

    @field_validator("findings", mode="before")
    @classmethod
    def normalize_finding_shapes(cls, value: object) -> list[str]:
        return _semantic_text_items(value, limit=12, item_limit=1_200)

    @field_validator("warnings", mode="before")
    @classmethod
    def normalize_warning_shapes(cls, value: object) -> list[str]:
        return _semantic_text_items(value, limit=8, item_limit=480)


class KnowledgeDeepReduceResult(BaseModel):
    """K4 最终 Reduce 检查点，附带确定性覆盖事实而非模型自报覆盖。"""

    overview: str = Field(min_length=1, max_length=2_000)
    findings: list[KnowledgeDeepReduceFinding] = Field(default_factory=list, max_length=12)
    conflicts: list[KnowledgeDeepReduceConflict] = Field(default_factory=list, max_length=8)
    comparison_rows: list[KnowledgeDeepComparisonRow] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=8)
    task_kind: KnowledgeDeepTaskKind
    covered_map_unit_ids: list[str] = Field(min_length=1)
    failed_map_unit_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_coverage(self) -> "KnowledgeDeepReduceResult":
        if len(self.covered_map_unit_ids) != len(set(self.covered_map_unit_ids)):
            raise ValueError("Reduce 覆盖范围不能重复包含同一 Map 单元。")
        if set(self.covered_map_unit_ids).intersection(self.failed_map_unit_ids):
            raise ValueError("同一 Map 单元不能同时标记为已覆盖和失败。")
        return self


def _semantic_text_items(value: object, *, limit: int, item_limit: int) -> list[str]:
    """把模型常见的字符串/对象混合列表收束为有限的业务文本。

    这个辅助函数只提取模型已经返回的 ``statement``、``text`` 等语义字段，不尝试推断来源、
    编号或任何事实。它用于消除 Provider 的轻微 JSON 形态差异，而不是放宽知识库证据边界。
    """

    raw_items = [value] if isinstance(value, (str, dict)) else value
    if not isinstance(raw_items, list):
        return []
    normalized: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = next(
                (
                    candidate
                    for candidate in (
                        item.get("statement"),
                        item.get("text"),
                        item.get("content"),
                        item.get("description"),
                    )
                    if isinstance(candidate, str)
                ),
                "",
            )
        else:
            continue
        text = text.strip()
        if text:
            normalized.append(text[:item_limit])
        if len(normalized) >= limit:
            break
    return normalized


class KnowledgeDeepTaskReduceRunResponse(BaseModel):
    """K4.3 Reduce 阶段回执；正式文件和客户阅读 UI 仍在后续阶段。"""

    task_id: str = Field(pattern=r"^task_k4_[a-z0-9]{8,32}$")
    status: WorkflowRunStatus
    completed_reduce_batch_count: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=1_200)
    result: KnowledgeDeepReduceResult | None = None


class KnowledgeDeepTaskStartResponse(BaseModel):
    """K4 后台深度任务的受理回执；正文和最终报告均不在受理接口返回。"""

    task_id: str = Field(pattern=r"^task_k4_[a-z0-9]{8,32}$")
    status: Literal["queued"] = "queued"


KnowledgeDeepTaskCoverageState = Literal["in_progress", "partial", "complete", "unavailable"]
KnowledgeDeepTaskReportState = Literal["not_ready", "partial_preview", "ready_for_export"]


class KnowledgeDeepTaskCoverage(BaseModel):
    """K4 当前可交付范围；只携带 Map 输出，不回显父块正文。"""

    state: KnowledgeDeepTaskCoverageState
    total_map_count: int = Field(ge=1)
    completed_map_unit_ids: list[str] = Field(default_factory=list)
    failed_map_unit_ids: list[str] = Field(default_factory=list)
    cancelled_map_unit_ids: list[str] = Field(default_factory=list)
    pending_map_unit_ids: list[str] = Field(default_factory=list)
    completed_map_results: list[KnowledgeDeepMapResult] = Field(default_factory=list)
    total_reduce_count: int = Field(ge=1)
    completed_reduce_count: int = Field(ge=0)
    failed_reduce_count: int = Field(ge=0)
    cancelled_reduce_count: int = Field(ge=0)
    pending_reduce_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_progress_partition(self) -> "KnowledgeDeepTaskCoverage":
        map_ids = (
            self.completed_map_unit_ids
            + self.failed_map_unit_ids
            + self.cancelled_map_unit_ids
            + self.pending_map_unit_ids
        )
        if len(map_ids) != self.total_map_count or len(map_ids) != len(set(map_ids)):
            raise ValueError("深度任务 Map 覆盖必须完整且每个章节只能处于一种状态。")
        completed_result_ids = [item.map_unit_id for item in self.completed_map_results]
        if len(completed_result_ids) != len(set(completed_result_ids)) or not set(completed_result_ids).issubset(
            self.completed_map_unit_ids
        ):
            raise ValueError("部分章节结果只能引用已完成且不重复的 Map 单元。")
        if (
            self.completed_reduce_count
            + self.failed_reduce_count
            + self.cancelled_reduce_count
            + self.pending_reduce_count
            != self.total_reduce_count
        ):
            raise ValueError("深度任务 Reduce 覆盖必须完整且每个节点只能处于一种状态。")
        return self


class KnowledgeDeepTaskReportReadiness(BaseModel):
    """报告交付边界：部分结果可阅读，但只有完整 Reduce 才能确认导出。"""

    state: KnowledgeDeepTaskReportState
    can_export: bool
    message: str = Field(min_length=1, max_length=1_200)
    missing_map_unit_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_export_boundary(self) -> "KnowledgeDeepTaskReportReadiness":
        if self.can_export != (self.state == "ready_for_export"):
            raise ValueError("只有完整深度任务可以进入确认导出状态。")
        return self


class KnowledgeDeepTaskReportExportRequest(BaseModel):
    """客户确认将一个完整 K4 深度任务写为新的 Markdown 正式报告。"""

    confirmed: bool = False
    filename: str = Field(default="", max_length=120)

    @field_validator("filename")
    @classmethod
    def normalize_filename(cls, value: str) -> str:
        # 文件名的目录边界和 Windows 非法字符由交付服务二次处理；这里仅消除无意义前后空白，
        # 防止同一客户名称因为界面复制产生看不见的冲突。
        return value.strip()


class KnowledgeDeepTaskReportExportResponse(BaseModel):
    """已确认报告的受控 artifact 回执；客户端不会收到真实输出路径。"""

    task_id: str = Field(pattern=r"^task_k4_[a-z0-9]{8,32}$")
    artifact_id: str = Field(min_length=1, max_length=180)
    filename: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(min_length=1, max_length=255)
    artifact_uri: str = Field(min_length=1, max_length=255)
    character_count: int = Field(gt=0, le=80_000)
    message: str = Field(min_length=1, max_length=1_200)


class KnowledgeDeepTaskResultResponse(BaseModel):
    """从统一任务历史恢复的 K4 状态或 Reduce 结果。"""

    task_id: str = Field(pattern=r"^task_k4_[a-z0-9]{8,32}$")
    status: WorkflowRunStatus
    summary: str = Field(default="", max_length=1_200)
    scope: KnowledgeDeepTaskScope | None = None
    result: KnowledgeDeepReduceResult | None = None
    coverage: KnowledgeDeepTaskCoverage | None = None
    report_readiness: KnowledgeDeepTaskReportReadiness | None = None


KnowledgeDeepTaskControlAction = Literal["pause", "resume", "cancel"]


class KnowledgeDeepTaskControlResponse(BaseModel):
    """K4 协作控制的轻量回执，不暴露完整步骤输出或材料正文。"""

    task_id: str = Field(pattern=r"^task_k4_[a-z0-9]{8,32}$")
    action: KnowledgeDeepTaskControlAction
    accepted: bool
    status: WorkflowRunStatus
    message: str = Field(min_length=1, max_length=1_200)


class KnowledgeIndexEnqueueResponse(BaseModel):
    """K1 后台索引受理回执；Qt 只订阅真实 job 状态，不自行推测完成比例。"""

    knowledge_base_id: str
    index_job_id: str
    status: Literal["queued"] = "queued"
