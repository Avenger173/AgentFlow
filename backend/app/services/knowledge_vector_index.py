"""知识库 K1.5 的 Chroma PersistentClient Adapter。

Adapter 只接受调用方已经得到的向量，不自行读取文件或调用模型；FastEmbed 初始化另设显式
确认入口，防止桌面端在普通导入或启动时下载本地模型。每个 generation 使用独立目录，
Windows 清理前始终关闭 Client 并释放 collection。
"""

from __future__ import annotations

from dataclasses import dataclass
import gc
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Sequence

from app.core.config import settings


@dataclass(frozen=True)
class VectorIndexCapability:
    """不含绝对路径的本地向量能力诊断。"""

    chroma_available: bool
    fastembed_available: bool
    model_initialized: bool
    message: str


@dataclass(frozen=True)
class VectorRecord:
    """Chroma 中允许保存的最小向量记录，不携带原文、路径或权限信息。"""

    child_chunk_id: str
    embedding: Sequence[float]
    knowledge_base_id: str
    document_version_id: str


@dataclass(frozen=True)
class VectorSearchHit:
    """Chroma 检索的最小回执；正文和路径继续从 SQLite 受控事实表按 ID 回读。"""

    child_chunk_id: str
    distance: float
    document_version_id: str


class LocalEmbeddingConfirmationRequired(RuntimeError):
    """本地模型尚未确认下载，调用方必须先走客户可见的确认流程。"""


EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_EMBEDDING_MARKER_NAME = "bge_small_zh_v1_5.ready"


def vector_index_capability() -> VectorIndexCapability:
    """仅检查依赖与本项目成功初始化标记，不下载/加载任何模型。"""

    try:
        import chromadb  # noqa: F401

        chroma_available = True
    except ImportError:
        chroma_available = False
    try:
        import fastembed  # noqa: F401

        fastembed_available = True
    except ImportError:
        fastembed_available = False
    marker = _embedding_marker_path()
    model_initialized = marker.is_file()
    if not chroma_available or not fastembed_available:
        message = "本地向量依赖未安装。"
    elif model_initialized:
        message = "本地向量依赖与已确认模型均已准备。"
    else:
        message = "本地向量依赖已准备，需由客户确认下载并初始化 Embedding 模型。"
    return VectorIndexCapability(
        chroma_available=chroma_available,
        fastembed_available=fastembed_available,
        model_initialized=model_initialized,
        message=message,
    )


def prepare_local_embedding_model(*, allow_download: bool) -> int:
    """初始化本地 Embedding 模型，并仅在明确批准时允许下载权重。

    该函数不接收客户文本。首次初始化后只写一个模型名和维度的受控标记，能力诊断据此避免
    在每次应用启动时重复加载模型。模型缓存目录固定在 AgentFlow data 目录内。
    """

    capability = vector_index_capability()
    if not capability.fastembed_available:
        raise RuntimeError("本地 FastEmbed 依赖未安装。")
    marker_path = _embedding_marker_path()
    if not marker_path.is_file() and not allow_download:
        raise LocalEmbeddingConfirmationRequired(
            "本地语义检索需要下载约 91MB 的 Embedding 模型，请先确认下载。"
        )
    # ``allow_download`` 只会在客户确认 API 中为 True；后台索引始终走 local_files_only，
    # 即使 marker 因手工清理而残留，也不能在客户不知情时重新联网拉取模型。
    model = _load_embedding_model(allow_download=allow_download)
    dimension = int(model.embedding_size)
    marker_path.write_text(
        json.dumps(
            {"model": EMBEDDING_MODEL_NAME, "dimension": dimension},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return dimension


def embed_local_texts(texts: Sequence[str], *, allow_download: bool) -> list[list[float]]:
    """为已受控子块生成本地向量；调用方不得把 API Key、路径或未授权原文传入。"""

    if not texts:
        return []
    capability = vector_index_capability()
    if not capability.fastembed_available:
        raise RuntimeError("本地 FastEmbed 依赖未安装。")
    if not _embedding_marker_path().is_file() and not allow_download:
        raise LocalEmbeddingConfirmationRequired(
            "本地语义检索尚未准备 Embedding 模型，请先在客户可见页面确认下载。"
        )
    model = _load_embedding_model(allow_download=allow_download)
    # FastEmbed 的迭代器会产出 numpy.float32。Chroma 1.5 只接受 Python ``float`` 或 ndarray，
    # 不能把含 numpy 标量的嵌套 list 直接传入；在 Provider 边界归一化后，后续 Adapter 也可
    # 保持不依赖具体 Embedding 库的纯 ``Sequence[float]`` 契约。
    return [[float(component) for component in vector] for vector in model.embed(list(texts))]


def _load_embedding_model(*, allow_download: bool):
    """加载 FastEmbed 模型，并把联网权限显式传到底层依赖。"""

    try:
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - 已由 capability 覆盖。
        raise RuntimeError("本地 FastEmbed 依赖未安装。") from exc
    cache_dir = settings.knowledge_embedding_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(
        model_name=EMBEDDING_MODEL_NAME,
        cache_dir=str(cache_dir),
        local_files_only=not allow_download,
    )


def _embedding_marker_path() -> Path:
    return settings.knowledge_embedding_cache_dir / _EMBEDDING_MARKER_NAME


class ChromaGenerationIndex:
    """generation 隔离的 Chroma 写入与回读 Adapter。"""

    def __init__(self, *, knowledge_base_id: str, generation_number: int) -> None:
        if generation_number < 1:
            raise ValueError("知识库 generation 编号必须大于零。")
        self._knowledge_base_id = knowledge_base_id
        self._generation_number = generation_number
        self._directory = (
            settings.knowledge_vector_storage_dir
            / knowledge_base_id
            / f"generation_{generation_number}"
        ).resolve()
        self._client = None
        self._collection = None

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """批量写入并回读计数；调用方负责确保向量来自同一 Embedding profile。"""

        if not records:
            raise ValueError("向量索引不能写入空记录集。")
        if any(record.knowledge_base_id != self._knowledge_base_id for record in records):
            raise ValueError("向量记录不能跨越资料库范围。")
        collection = self._open_collection()
        collection.upsert(
            ids=[record.child_chunk_id for record in records],
            embeddings=[list(record.embedding) for record in records],
            metadatas=[
                {
                    "knowledge_base_id": record.knowledge_base_id,
                    "document_version_id": record.document_version_id,
                    "generation_number": self._generation_number,
                }
                for record in records
            ],
        )
        count = int(collection.count())
        if count < len(records):
            raise RuntimeError("Chroma 回读数量少于本次写入记录。")
        return count

    def verify(self, expected_child_chunk_ids: Sequence[str]) -> bool:
        """验证同一 generation 的指定子块可回读且元数据没有跨库。"""

        if not expected_child_chunk_ids:
            return False
        collection = self._open_collection()
        result = collection.get(
            ids=list(expected_child_chunk_ids),
            include=["metadatas"],
        )
        identifiers = set(result.get("ids", []))
        metadata = result.get("metadatas", [])
        return identifiers == set(expected_child_chunk_ids) and all(
            item
            and item.get("knowledge_base_id") == self._knowledge_base_id
            and item.get("generation_number") == self._generation_number
            for item in metadata
        )

    def read_embeddings(self, child_chunk_ids: Sequence[str]) -> dict[str, list[float]]:
        """只读回收本 generation 已验证的向量，不读取正文也不跨 generation 复用。

        K5.6 的增量索引只能把这份受控回执写入新的 generation；调用方仍必须在 SQLite
        再核对子块 ID、内容哈希与 Embedding Profile。Adapter 在这里额外校验元数据，是为了
        防止 Chroma 目录被误指向、部分写入或旧 collection 混入时把错误向量当作可复用缓存。
        """

        requested_ids = [child_chunk_id for child_chunk_id in child_chunk_ids if child_chunk_id]
        if not requested_ids:
            return {}
        # 读取旧 generation 不能调用 ``get_or_create_collection``：若目录被手工清理，K5.6
        # 必须回退到新嵌入，而不是悄悄重建一个空 collection 后误报“可复用”。
        if not self._directory.is_dir():
            return {}
        collection = self._open_existing_collection()
        result = collection.get(
            ids=requested_ids,
            include=["embeddings", "metadatas"],
        )
        identifiers = result.get("ids", [])
        embeddings = result.get("embeddings", [])
        metadatas = result.get("metadatas", [])
        reusable: dict[str, list[float]] = {}
        for child_chunk_id, embedding, metadata in zip(
            identifiers,
            embeddings,
            metadatas,
            strict=False,
        ):
            if not isinstance(metadata, dict):
                continue
            if (
                metadata.get("knowledge_base_id") != self._knowledge_base_id
                or metadata.get("generation_number") != self._generation_number
            ):
                continue
            try:
                normalized = [float(component) for component in embedding]
            except (TypeError, ValueError):
                continue
            if normalized:
                reusable[str(child_chunk_id)] = normalized
        return reusable

    def query(self, query_embedding: Sequence[float], *, limit: int) -> list[VectorSearchHit]:
        """按同一资料库 generation 查询有限向量候选，不自动创建 Embedding 或读取正文。"""

        if not query_embedding or limit < 1:
            return []
        collection = self._open_collection()
        result = collection.query(
            query_embeddings=[list(query_embedding)],
            n_results=max(1, min(limit, 20)),
            include=["distances", "metadatas"],
        )
        identifiers = result.get("ids", [[]])
        distances = result.get("distances", [[]])
        metadata_sets = result.get("metadatas", [[]])
        first_ids = identifiers[0] if identifiers else []
        first_distances = distances[0] if distances else []
        first_metadata = metadata_sets[0] if metadata_sets else []
        hits: list[VectorSearchHit] = []
        for child_chunk_id, distance, metadata in zip(first_ids, first_distances, first_metadata):
            if not isinstance(metadata, dict):
                continue
            if (
                metadata.get("knowledge_base_id") != self._knowledge_base_id
                or metadata.get("generation_number") != self._generation_number
            ):
                continue
            document_version_id = metadata.get("document_version_id")
            if not isinstance(document_version_id, str) or not document_version_id:
                continue
            try:
                numeric_distance = float(distance)
            except (TypeError, ValueError):
                continue
            hits.append(
                VectorSearchHit(
                    child_chunk_id=str(child_chunk_id),
                    distance=max(0.0, numeric_distance),
                    document_version_id=document_version_id,
                )
            )
        return hits

    def close(self) -> None:
        """显式关闭 Chroma，避免 Windows 在后续更新/删除时残留句柄。"""

        self._collection = None
        if self._client is not None:
            self._client.close()
        self._client = None
        gc.collect()

    def remove_generation_directory(self) -> None:
        """仅清理本 Adapter 的 generation 私有目录，调用方须先撤销活动指针。"""

        self.close()
        if self._directory.exists():
            shutil.rmtree(self._directory, ignore_errors=False)

    def _open_collection(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:  # pragma: no cover - requirements 已固定，保留运行期提示。
            raise RuntimeError("本地 Chroma 依赖未安装。") from exc
        self._directory.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(self._directory),
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name(),
            embedding_function=None,
        )
        return self._collection

    def _open_existing_collection(self):
        """只打开已存在的 generation collection，缺失时由调用方选择安全降级。"""

        if self._collection is not None:
            return self._collection
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:  # pragma: no cover - requirements 已固定，保留运行期提示。
            raise RuntimeError("本地 Chroma 依赖未安装。") from exc
        self._client = chromadb.PersistentClient(
            path=str(self._directory),
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_collection(
            name=self._collection_name(),
            embedding_function=None,
        )
        return self._collection

    def _collection_name(self) -> str:
        return "knowledge_" + sha256(
            f"{self._knowledge_base_id}:{self._generation_number}".encode("utf-8")
        ).hexdigest()[:20]
