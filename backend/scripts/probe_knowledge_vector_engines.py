"""知识库 K0 向量引擎的隔离能力探针。

本脚本不负责选择 Embedding 模型，也不处理客户文档；固定向量只用于验证向量引擎的产品
边界：本地磁盘持久化、knowledge_base_id 过滤、重启回读和关闭后清理。运行时应使用专门
的 K0 虚拟环境，正式 ``backend/.venv`` 不安装候选依赖。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import tempfile


def _run_qdrant_local() -> None:
    """验证 Qdrant Python Local Mode 的最小产品所需能力。"""

    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:  # pragma: no cover - 由隔离环境决定是否安装候选依赖。
        raise RuntimeError(
            "未安装 qdrant-client；请使用 backend/.k0_qdrant_probe 中的解释器运行本探针。"
        ) from exc

    temporary_dir = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_qdrant_"))
    storage_path = temporary_dir / "vector_store"
    collection_name = "knowledge_k0_probe"
    client: QdrantClient | None = None
    reopened_client: QdrantClient | None = None
    try:
        # 显式 path 不经过 Docker 或网络服务，符合本地默认不出站的知识库边界。
        client = QdrantClient(path=str(storage_path))
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        client.upsert(
            collection_name=collection_name,
            wait=True,
            points=[
                models.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "knowledge_base_id": "kb-alpha",
                        "document_version_id": "doc-alpha-v1",
                        "child_chunk_id": "chunk-alpha-1",
                    },
                ),
                models.PointStruct(
                    id=2,
                    vector=[0.98, 0.02, 0.0],
                    payload={
                        "knowledge_base_id": "kb-beta",
                        "document_version_id": "doc-beta-v1",
                        "child_chunk_id": "chunk-beta-1",
                    },
                ),
            ],
        )
        alpha_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="knowledge_base_id",
                    match=models.MatchValue(value="kb-alpha"),
                )
            ]
        )
        filtered_points = client.query_points(
            collection_name=collection_name,
            query=[0.99, 0.01, 0.0],
            query_filter=alpha_filter,
            limit=3,
        ).points
        assert [point.id for point in filtered_points] == [1], "知识库范围过滤没有隔离候选向量。"
        client.close()
        client = None

        # 重新打开同一目录，确认 K1 可用内容哈希/版本表驱动持久索引，而非只依赖内存状态。
        reopened_client = QdrantClient(path=str(storage_path))
        persisted_points = reopened_client.query_points(
            collection_name=collection_name,
            query=[1.0, 0.0, 0.0],
            query_filter=alpha_filter,
            limit=1,
        ).points
        assert len(persisted_points) == 1 and persisted_points[0].id == 1, "重启后未能读回本地向量。"
    finally:
        if client is not None:
            client.close()
        if reopened_client is not None:
            reopened_client.close()
        # Qdrant Local 的 close 必须先完成，Windows 才能删除临时段文件；失败时保留目录也不影响
        # 真实资料，因为本探针只写入系统临时目录。
        shutil.rmtree(temporary_dir, ignore_errors=True)

    print(
        "Knowledge K0 vector probe passed: engine=qdrant_local persistence=true "
        "knowledge_base_filter=true restart_readback=true network=false embedding_model=none"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行知识库 K0 向量引擎能力探针。")
    parser.add_argument("--engine", choices=("qdrant",), default="qdrant")
    arguments = parser.parse_args()
    if arguments.engine == "qdrant":
        _run_qdrant_local()


if __name__ == "__main__":
    main()
