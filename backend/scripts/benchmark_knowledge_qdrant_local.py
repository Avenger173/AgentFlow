"""知识库 K0 的 Qdrant Local 分批索引与检索基准。

基准只生成可重复的随机单位向量和最小 payload，不嵌入真实文档、不请求模型，也不写入正式
数据目录。它用于估计 K1 的本地索引成本，并覆盖知识库范围过滤与关闭清理是否能在 Windows
环境稳定工作。默认 10k；100k 必须由开发者显式传参后单独记录设备和结果。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import statistics
import tempfile
import time

import numpy as np


def _unit_vectors(random: np.random.Generator, count: int, dimension: int) -> np.ndarray:
    """生成固定种子的单位向量，确保不同运行的输入分布一致。"""

    vectors = random.standard_normal((count, dimension), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, np.finfo(np.float32).eps)


def _percentile(values: list[float], percentile: float) -> float:
    """使用 NumPy 统一计算分位数，避免小样本时手写插值产生不可复验结果。"""

    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def main() -> None:
    parser = argparse.ArgumentParser(description="运行知识库 K0 Qdrant Local 基准。")
    parser.add_argument("--chunks", type=int, default=10_000, choices=(10_000, 100_000))
    parser.add_argument("--dimension", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=1_024)
    parser.add_argument("--queries", type=int, default=20)
    arguments = parser.parse_args()

    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:  # pragma: no cover - 由 K0 隔离环境决定候选依赖是否存在。
        raise RuntimeError(
            "未安装 qdrant-client；请使用 backend/.k0_qdrant_probe 中的解释器运行本基准。"
        ) from exc

    if arguments.dimension <= 0 or arguments.batch_size <= 0 or arguments.queries <= 0:
        raise ValueError("dimension、batch-size 和 queries 必须大于 0。")

    temporary_dir = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_qdrant_benchmark_"))
    client: QdrantClient | None = None
    random = np.random.default_rng(seed=20_260_820)
    try:
        collection_name = "knowledge_k0_benchmark"
        client = QdrantClient(path=str(temporary_dir / "vector_store"))
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(size=arguments.dimension, distance=models.Distance.COSINE),
        )
        # ``upload_collection`` 接收二维 NumPy 数组并在 Client 内部批量发送。它避免为每个子块
        # 创建 Python PointStruct/list 的额外开销，是 K1 后台 Index Job 的候选写入路径；100k
        # 基准前仍需评估整批向量占用的内存，不能在交互请求中构造如此大的数组。
        vectors = _unit_vectors(random, arguments.chunks, arguments.dimension)
        payload = [
            {
                # 用两个资料库交替模拟过滤，内容仍全部是合成 ID。
                "knowledge_base_id": "kb-alpha" if index % 2 == 0 else "kb-beta",
                "document_version_id": f"doc-{index // 8:06d}-v1",
                "child_chunk_id": f"chunk-{index:07d}",
            }
            for index in range(arguments.chunks)
        ]
        started_at = time.perf_counter()
        client.upload_collection(
            collection_name=collection_name,
            vectors=vectors,
            payload=payload,
            ids=range(1, arguments.chunks + 1),
            batch_size=arguments.batch_size,
            parallel=1,
            wait=True,
        )
        index_ms = (time.perf_counter() - started_at) * 1000

        filter_alpha = models.Filter(
            must=[
                models.FieldCondition(
                    key="knowledge_base_id",
                    match=models.MatchValue(value="kb-alpha"),
                )
            ]
        )
        query_latencies_ms: list[float] = []
        for _index in range(arguments.queries):
            query_vector = _unit_vectors(random, 1, arguments.dimension)[0]
            started_at = time.perf_counter()
            result = client.query_points(
                collection_name=collection_name,
                query=query_vector.tolist(),
                query_filter=filter_alpha,
                limit=8,
            ).points
            query_latencies_ms.append((time.perf_counter() - started_at) * 1000)
            assert result, "知识库范围过滤后没有返回任何合成候选。"
            assert all(point.payload.get("knowledge_base_id") == "kb-alpha" for point in result), (
                "Qdrant Local 基准发生了跨知识库候选泄漏。"
            )
    finally:
        if client is not None:
            client.close()
        # 只清理本轮生成的临时向量段文件；正式索引的生命周期由 K1 服务和版本表管理。
        shutil.rmtree(temporary_dir, ignore_errors=True)

    print(
        "Knowledge K0 Qdrant benchmark passed: "
        f"chunks={arguments.chunks} dimension={arguments.dimension} batch_size={arguments.batch_size} "
        f"index_ms={index_ms:.0f} query_count={arguments.queries} "
        f"query_p50_ms={statistics.median(query_latencies_ms):.2f} "
        f"query_p95_ms={_percentile(query_latencies_ms, 95):.2f} "
        "filter_isolation=true synthetic_data=true model_calls=0 network=false"
    )


if __name__ == "__main__":
    main()
