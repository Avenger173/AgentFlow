"""知识库 K0 本地 Embedding 候选的可复验探针。

本脚本仅使用项目内脱敏夹具，验证 FastEmbed 的中文本地模型是否能完成最小语义召回。
模型文件只缓存到 K0 隔离目录，不能安装到正式 ``backend/.venv``，更不能读取客户资料、
API Key 或调用云端 LLM。它不是 K1 的索引实现，也不替代后续更大规模的质量/性能基准。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import time

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = SCRIPT_DIR / "fixtures" / "knowledge_k0"
QUESTIONS_PATH = FIXTURE_DIR / "question_set.json"
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_CACHE_DIR = SCRIPT_DIR.parent / ".k0_qdrant_probe" / "fastembed_cache"


def _load_documents() -> list[tuple[str, str]]:
    """读取固定夹具，不允许调用方传入任意本机路径或客户资料。"""

    documents = [(path.stem, path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_DIR.glob("*.md"))]
    if not documents:
        raise RuntimeError("知识库 K0 夹具为空。")
    return documents


def _cosine_ranking(query: np.ndarray, documents: np.ndarray) -> list[int]:
    """用明确的余弦相似度排序，避免不同向量引擎的分数尺度混入 K0 质量判断。"""

    query_norm = float(np.linalg.norm(query))
    document_norms = np.linalg.norm(documents, axis=1)
    if query_norm == 0.0 or np.any(document_norms == 0.0):
        raise RuntimeError("Embedding 探针得到零向量，不能用于语义召回评估。")
    scores = (documents @ query) / (document_norms * query_norm)
    return [int(index) for index in np.argsort(-scores)]


def _semantic_cases() -> list[dict[str, object]]:
    """只挑选已标注的单文档语义改写题，跨文档题留给 Hybrid/RRF 阶段评估。"""

    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    cases = [item for item in questions if item.get("retrieval_kind") == "semantic"]
    if not cases:
        raise RuntimeError("知识库 K0 题集缺少语义召回标注。")
    return cases


def _verify_qdrant_retrieval(
    *,
    document_ids: list[str],
    document_vectors: np.ndarray,
    cases: list[dict[str, object]],
    query_vectors: np.ndarray,
) -> None:
    """把同一批真实向量写入临时 Qdrant Local，验证最终引擎不会改变来源归属。"""

    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:  # pragma: no cover - 由 K0 隔离环境决定候选依赖是否存在。
        raise RuntimeError("--with-qdrant 需要 qdrant-client。") from exc

    temporary_dir = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_embedding_qdrant_"))
    client: QdrantClient | None = None
    try:
        collection_name = "knowledge_k0_embedding_probe"
        client = QdrantClient(path=str(temporary_dir / "vector_store"))
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=int(document_vectors.shape[1]),
                distance=models.Distance.COSINE,
            ),
        )
        client.upsert(
            collection_name=collection_name,
            wait=True,
            points=[
                models.PointStruct(
                    id=index + 1,
                    vector=vector.tolist(),
                    payload={
                        "knowledge_base_id": "kb-k0-alpha",
                        "document_id": document_id,
                    },
                )
                for index, (document_id, vector) in enumerate(zip(document_ids, document_vectors, strict=True))
            ],
        )
        knowledge_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="knowledge_base_id",
                    match=models.MatchValue(value="kb-k0-alpha"),
                )
            ]
        )
        for case, query_vector in zip(cases, query_vectors, strict=True):
            points = client.query_points(
                collection_name=collection_name,
                query=query_vector.tolist(),
                query_filter=knowledge_filter,
                limit=1,
            ).points
            assert points, f"Qdrant Local 没有返回语义题 {case['id']} 的候选。"
            document_id = str(points[0].payload.get("document_id") or "")
            expected_ids = {str(item) for item in case["expected_document_ids"]}
            assert document_id in expected_ids, (
                f"Qdrant Local 语义题 {case['id']} 未在 Top 1 命中标注资料；"
                f"expected={sorted(expected_ids)} actual={document_id}"
            )
    finally:
        if client is not None:
            client.close()
        # 只清理系统临时目录中的脱敏向量；关闭 client 在 Windows 上是清理成功的前提。
        shutil.rmtree(temporary_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="运行知识库 K0 FastEmbed 中文语义召回探针。")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--with-qdrant", action="store_true")
    arguments = parser.parse_args()

    try:
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - 由 K0 隔离环境决定候选依赖是否存在。
        raise RuntimeError(
            "未安装 fastembed；请使用 backend/.k0_qdrant_probe 中的解释器运行本探针。"
        ) from exc

    documents = _load_documents()
    cases = _semantic_cases()
    cache_dir = Path(arguments.model_cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_had_files = any(cache_dir.iterdir())

    # 模型初始化可能首次下载公开的 ONNX 权重；K1 产品侧会在客户确认本地模型包后显式显示
    # 下载状态。本探针不伪造“纯离线首次加载”，但二次运行应命中隔离缓存。
    started_at = time.perf_counter()
    try:
        model = TextEmbedding(model_name=arguments.model, cache_dir=str(cache_dir))
    except Exception as exc:
        raise RuntimeError(f"本地 Embedding 模型初始化失败：{type(exc).__name__}: {exc}") from exc
    initialization_ms = (time.perf_counter() - started_at) * 1000

    document_ids = [document_id for document_id, _text in documents]
    document_texts = [text for _document_id, text in documents]
    started_at = time.perf_counter()
    document_vectors = np.asarray(list(model.embed(document_texts)), dtype=np.float32)
    query_vectors = np.asarray(list(model.query_embed([str(case["question"]) for case in cases])), dtype=np.float32)
    embedding_ms = (time.perf_counter() - started_at) * 1000

    if document_vectors.ndim != 2 or query_vectors.ndim != 2:
        raise RuntimeError("Embedding 模型没有返回二维向量矩阵。")
    if document_vectors.shape[1] != query_vectors.shape[1]:
        raise RuntimeError("文档与问题向量维度不一致。")

    passed_cases = 0
    for case, query_vector in zip(cases, query_vectors, strict=True):
        ranked_indices = _cosine_ranking(query_vector, document_vectors)
        top_document_id = document_ids[ranked_indices[0]]
        expected_ids = {str(item) for item in case["expected_document_ids"]}
        assert top_document_id in expected_ids, (
            f"语义题 {case['id']} 未在 Top 1 命中标注资料；"
            f"expected={sorted(expected_ids)} actual={top_document_id}"
        )
        passed_cases += 1

    if arguments.with_qdrant:
        _verify_qdrant_retrieval(
            document_ids=document_ids,
            document_vectors=document_vectors,
            cases=cases,
            query_vectors=query_vectors,
        )

    # 输出不含夹具正文、问题文本、嵌入向量或本机绝对路径，适合持续集成日志。
    cache_state = "warm" if cache_had_files else "cold"
    print(
        "Knowledge K0 embedding probe passed: "
        f"model={arguments.model} dimension={document_vectors.shape[1]} "
        f"semantic_top_1={passed_cases}/{len(cases)} documents={len(documents)} "
        f"cache={cache_state} init_ms={initialization_ms:.0f} embed_ms={embedding_ms:.0f} "
        f"qdrant_local={str(arguments.with_qdrant).lower()} customer_data=false llm_calls=0"
    )


if __name__ == "__main__":
    main()
