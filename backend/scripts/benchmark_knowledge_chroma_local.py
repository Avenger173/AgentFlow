"""知识库 K0 的 Chroma PersistentClient 分批索引与检索基准。

输入与 Qdrant 基准保持一致：固定种子的合成向量、两个知识库 ID 和最小版本 payload。该脚本
只用于比较 Windows 本地持久化候选，不读客户资料、不调用 Embedding/LLM，也不写入正式数据。
"""

from __future__ import annotations

import argparse
import gc
from pathlib import Path
import shutil
import statistics
import tempfile
import time

import numpy as np


def _unit_vectors(random: np.random.Generator, count: int, dimension: int) -> np.ndarray:
    """生成固定种子的单位向量，使不同引擎的输入分布相同。"""

    vectors = random.standard_normal((count, dimension), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, np.finfo(np.float32).eps)


def _percentile(values: list[float], percentile: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def _directory_size_bytes(path: Path) -> int:
    """统计当前索引文件的实际占用，用于 K0 发行与磁盘预算而非客户展示。"""

    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> None:
    parser = argparse.ArgumentParser(description="运行知识库 K0 Chroma PersistentClient 基准。")
    parser.add_argument("--chunks", type=int, default=10_000, choices=(10_000, 100_000))
    parser.add_argument("--dimension", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=1_024)
    parser.add_argument("--queries", type=int, default=20)
    arguments = parser.parse_args()

    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError as exc:  # pragma: no cover - 由 K0 隔离环境决定候选依赖是否存在。
        raise RuntimeError(
            "未安装 chromadb；请使用 backend/.k0_chroma_probe 中的解释器运行本基准。"
        ) from exc

    if arguments.dimension <= 0 or arguments.batch_size <= 0 or arguments.queries <= 0:
        raise ValueError("dimension、batch-size 和 queries 必须大于 0。")

    temporary_dir = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_chroma_benchmark_"))
    random = np.random.default_rng(seed=20_260_820)
    client = None
    collection = None
    try:
        # 禁用匿名遥测，确保本地知识库基准不产生非预期出站请求。
        client = chromadb.PersistentClient(
            path=str(temporary_dir / "vector_store"),
            settings=Settings(anonymized_telemetry=False),
        )
        collection = client.create_collection(name="knowledge_k0_benchmark")
        vectors = _unit_vectors(random, arguments.chunks, arguments.dimension)
        started_at = time.perf_counter()
        for batch_start in range(0, arguments.chunks, arguments.batch_size):
            batch_end = min(arguments.chunks, batch_start + arguments.batch_size)
            collection.add(
                ids=[str(index + 1) for index in range(batch_start, batch_end)],
                embeddings=vectors[batch_start:batch_end].tolist(),
                metadatas=[
                    {
                        "knowledge_base_id": "kb-alpha" if index % 2 == 0 else "kb-beta",
                        "document_version_id": f"doc-{index // 8:06d}-v1",
                        "child_chunk_id": f"chunk-{index:07d}",
                    }
                    for index in range(batch_start, batch_end)
                ],
            )
        index_ms = (time.perf_counter() - started_at) * 1000

        query_latencies_ms: list[float] = []
        for _index in range(arguments.queries):
            query_vector = _unit_vectors(random, 1, arguments.dimension)[0]
            started_at = time.perf_counter()
            result = collection.query(
                query_embeddings=[query_vector.tolist()],
                n_results=8,
                where={"knowledge_base_id": "kb-alpha"},
                include=["metadatas"],
            )
            query_latencies_ms.append((time.perf_counter() - started_at) * 1000)
            metadatas = result.get("metadatas") or []
            candidates = metadatas[0] if metadatas else []
            assert candidates, "知识库范围过滤后没有返回任何合成候选。"
            assert all(item.get("knowledge_base_id") == "kb-alpha" for item in candidates), (
                "Chroma 基准发生了跨知识库候选泄漏。"
            )
        storage_bytes = _directory_size_bytes(temporary_dir / "vector_store")
    finally:
        # Chroma 1.5 的 Client 已提供 close。必须先关闭 SQLite/HNSW 句柄并释放 collection，
        # 否则 Windows 会让临时索引目录残留，长期运行会逐渐占满系统临时盘。
        collection = None
        if client is not None:
            client.close()
        client = None
        gc.collect()
        shutil.rmtree(temporary_dir, ignore_errors=True)
        if temporary_dir.exists():
            raise RuntimeError(f"Chroma 基准临时目录未能清理：{temporary_dir}")

    print(
        "Knowledge K0 Chroma benchmark passed: "
        f"chunks={arguments.chunks} dimension={arguments.dimension} batch_size={arguments.batch_size} "
        f"index_ms={index_ms:.0f} query_count={arguments.queries} "
        f"query_p50_ms={statistics.median(query_latencies_ms):.2f} "
        f"query_p95_ms={_percentile(query_latencies_ms, 95):.2f} "
        f"storage_mb={storage_bytes / 1_000_000:.2f} "
        "filter_isolation=true telemetry=false synthetic_data=true model_calls=0 network=false"
    )


if __name__ == "__main__":
    main()
