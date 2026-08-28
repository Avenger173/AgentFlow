"""K5.8 知识库性能事实与本机资源建议。

本模块仅聚合已保存的索引时长，以及本进程内不含查询文本的检索/深度任务耗时。它不扫描
资料正文、不保存文件名或问题、不读取设备身份，也不建立跨资料库内容画像。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from json import JSONDecodeError
from os import cpu_count
from pathlib import Path
from shutil import disk_usage
from statistics import median
from threading import RLock
from time import monotonic

from app.core.config import settings
from app.database.sqlite import get_connection
from app.schemas.knowledge import (
    KnowledgePerformanceObservation,
    KnowledgePerformanceProfileResponse,
    KnowledgePerformanceState,
    KnowledgePerformanceTier,
    KnowledgeStorageState,
)
from app.schemas.workflow import WorkflowRun


_OBSERVATION_LIMIT = 48
_INDEX_OBSERVATION_LIMIT = 24
_SLOW_RETRIEVAL_P95_MS = 1_000
_SLOW_INDEX_P95_MS = 120_000
_SLOW_DEEP_TASK_P95_MS = 1_800_000


@dataclass(frozen=True)
class _RuntimeDurationSample:
    elapsed_ms: int


_runtime_observations_lock = RLock()
_retrieval_samples: deque[_RuntimeDurationSample] = deque(maxlen=_OBSERVATION_LIMIT)
_deep_task_samples: deque[_RuntimeDurationSample] = deque(maxlen=_OBSERVATION_LIMIT)


def record_knowledge_retrieval_elapsed_ms(elapsed_ms: int) -> None:
    """记录一次真实本地检索耗时，不记录 query、结果、资料库 ID 或状态文本。"""

    _record_runtime_sample(_retrieval_samples, elapsed_ms)


def record_knowledge_deep_task_elapsed_ms(elapsed_ms: int) -> None:
    """记录一次实际获得运行槽位后的 K4 耗时，不计排队等待。"""

    _record_runtime_sample(_deep_task_samples, elapsed_ms)


def build_knowledge_performance_profile() -> KnowledgePerformanceProfileResponse:
    """构造 K5.8 的无正文性能建议；队列快照由异步 API 层附加。"""

    logical_cpu_count = max(1, cpu_count() or 1)
    free_gib, storage_state = _storage_status()
    resource_tier = _resource_tier(
        logical_cpu_count=logical_cpu_count,
        free_gib=free_gib,
        storage_state=storage_state,
    )
    index_observation = _load_index_observation()
    retrieval_observation = _runtime_observation(_retrieval_samples)
    deep_task_observation = _combine_deep_task_observations()
    performance_state = _performance_state(
        index_observation=index_observation,
        retrieval_observation=retrieval_observation,
        deep_task_observation=deep_task_observation,
    )
    recommendations = _recommendations(
        resource_tier=resource_tier,
        storage_state=storage_state,
        performance_state=performance_state,
        index_observation=index_observation,
        retrieval_observation=retrieval_observation,
        deep_task_observation=deep_task_observation,
    )
    # queue 字段由 knowledge API 在同一个请求中读取真实异步快照后替换；这里的默认值确保
    # 性能计算本身可在同步离线夹具中独立验证。
    from app.schemas.knowledge import KnowledgeRuntimeQueueSnapshot

    return KnowledgePerformanceProfileResponse(
        resource_tier=resource_tier,
        performance_state=performance_state,
        logical_cpu_count=logical_cpu_count,
        data_storage_free_gib=free_gib,
        data_storage_state=storage_state,
        index_observation=index_observation,
        retrieval_observation=retrieval_observation,
        deep_task_observation=deep_task_observation,
        runtime_queue=KnowledgeRuntimeQueueSnapshot(message="正在读取当前进程的知识库运行队列。"),
        recommendations=recommendations,
        privacy_notice=(
            "性能建议只聚合阶段耗时、任务数量、逻辑核数和数据目录可用空间；"
            "不会保存资料正文、查询、文件名、路径或设备身份。"
        ),
    )


def knowledge_runtime_queue_max_active_work_kinds() -> int:
    """返回当前资源层允许同时占用的重任务通道数，不写入或标识设备身份。"""

    logical_cpu_count = max(1, cpu_count() or 1)
    free_gib, storage_state = _storage_status()
    tier = _resource_tier(
        logical_cpu_count=logical_cpu_count,
        free_gib=free_gib,
        storage_state=storage_state,
    )
    # 低配设备把索引和深度任务也串成一条全局重负载通道；中高配才允许一条索引和一条深度
    # 链同时存在。无论哪种情况，同类工作都始终单并发，避免多个 PDF/向量写入并发占句柄。
    return 1 if tier == "low" else 2


def _record_runtime_sample(samples: deque[_RuntimeDurationSample], elapsed_ms: int) -> None:
    with _runtime_observations_lock:
        samples.append(_RuntimeDurationSample(elapsed_ms=max(0, int(elapsed_ms))))


def _storage_status() -> tuple[float, KnowledgeStorageState]:
    """读取数据目录所在卷的可用空间；不递归扫描目录，也不写入磁盘。"""

    path = _nearest_existing_path(settings.data_dir)
    free_gib = max(0.0, disk_usage(path).free / (1024**3))
    if free_gib < 4.0:
        return round(free_gib, 1), "low"
    if free_gib < 12.0:
        return round(free_gib, 1), "attention"
    return round(free_gib, 1), "sufficient"


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _resource_tier(
    *,
    logical_cpu_count: int,
    free_gib: float,
    storage_state: KnowledgeStorageState,
) -> KnowledgePerformanceTier:
    """用粗粒度资源事实分级，不采集型号、序列号、进程列表或机器身份。"""

    if storage_state == "low" or logical_cpu_count <= 4 or free_gib < 4.0:
        return "low"
    if storage_state == "attention" or logical_cpu_count <= 8 or free_gib < 12.0:
        return "medium"
    return "high"


def _load_index_observation() -> KnowledgePerformanceObservation:
    """读取最近完成索引的阶段总耗时；SQL 不返回资料库 ID、文件名或内容。"""

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT total_elapsed_ms
            FROM knowledge_index_jobs
            WHERE status = 'completed' AND total_elapsed_ms > 0
            ORDER BY updated_at DESC, index_job_id DESC
            LIMIT ?
            """,
            (_INDEX_OBSERVATION_LIMIT,),
        ).fetchall()
    return _observation_from_elapsed_ms(
        [int(row["total_elapsed_ms"]) for row in rows],
        source="persisted_index_jobs",
    )


def _combine_deep_task_observations() -> KnowledgePerformanceObservation:
    """合并当前进程真实执行耗时与历史无正文 task metrics，不读取 steps/output。"""

    with _runtime_observations_lock:
        elapsed_values = [sample.elapsed_ms for sample in _deep_task_samples]
    has_process_samples = bool(elapsed_values)
    # 早期 K4 任务没有 duration_ms 时不会伪造历史数据；K5.8 开始的 checkpoint 会逐步补齐。
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT run_json
            FROM workflow_runs
            WHERE task_id LIKE 'task_k4_%'
            ORDER BY updated_at DESC, task_id DESC
            LIMIT ?
            """,
            (_INDEX_OBSERVATION_LIMIT,),
        ).fetchall()
    persisted_sample_count = 0
    for row in rows:
        try:
            run = WorkflowRun.model_validate_json(str(row["run_json"]))
        except (JSONDecodeError, ValueError):
            continue
        if run.metrics.duration_ms > 0:
            elapsed_values.append(run.metrics.duration_ms)
            persisted_sample_count += 1
    # 同一完成任务可能恰好同时出现在当前进程采样与 SQLite。K5.8 的建议只需粗粒度趋势，
    # 保持最新 48 项即可，不能为去重保存 task ID 或内容关联键。
    source = (
        "mixed_runtime_metrics"
        if has_process_samples and persisted_sample_count
        else "process_local_runtime_samples"
        if has_process_samples
        else "persisted_task_metrics"
        if persisted_sample_count
        else "not_available"
    )
    return _observation_from_elapsed_ms(elapsed_values[-_OBSERVATION_LIMIT:], source=source)


def _runtime_observation(samples: deque[_RuntimeDurationSample]) -> KnowledgePerformanceObservation:
    with _runtime_observations_lock:
        elapsed_values = [sample.elapsed_ms for sample in samples]
    return _observation_from_elapsed_ms(elapsed_values, source="process_local_runtime_samples")


def _observation_from_elapsed_ms(
    elapsed_values: list[int],
    *,
    source: str,
) -> KnowledgePerformanceObservation:
    if not elapsed_values:
        return KnowledgePerformanceObservation(sample_count=0, source="not_available")
    ordered = sorted(max(0, value) for value in elapsed_values)
    p95_index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * 0.95)))
    return KnowledgePerformanceObservation(
        sample_count=len(ordered),
        median_elapsed_ms=round(median(ordered)),
        p95_elapsed_ms=ordered[p95_index],
        source=source,  # type: ignore[arg-type]
    )


def _performance_state(
    *,
    index_observation: KnowledgePerformanceObservation,
    retrieval_observation: KnowledgePerformanceObservation,
    deep_task_observation: KnowledgePerformanceObservation,
) -> KnowledgePerformanceState:
    if not any(
        observation.sample_count
        for observation in (index_observation, retrieval_observation, deep_task_observation)
    ):
        return "insufficient_data"
    if (
        (index_observation.p95_elapsed_ms or 0) > _SLOW_INDEX_P95_MS
        or (retrieval_observation.p95_elapsed_ms or 0) > _SLOW_RETRIEVAL_P95_MS
        or (deep_task_observation.p95_elapsed_ms or 0) > _SLOW_DEEP_TASK_P95_MS
    ):
        return "attention"
    return "stable"


def _recommendations(
    *,
    resource_tier: KnowledgePerformanceTier,
    storage_state: KnowledgeStorageState,
    performance_state: KnowledgePerformanceState,
    index_observation: KnowledgePerformanceObservation,
    retrieval_observation: KnowledgePerformanceObservation,
    deep_task_observation: KnowledgePerformanceObservation,
) -> list[str]:
    """输出可操作但不夸大自动调参能力的建议。"""

    recommendations = [
        "索引和深度任务各自最多同时运行 1 条；普通检索与问答保持即时，不进入重任务队列。"
    ]
    if resource_tier == "low":
        recommendations.append("当前低配策略会把索引和深度任务全局串行；请优先完成一项重任务后，再发起下一项，本地语义模型按需启用。")
    elif resource_tier == "medium":
        recommendations.append("当前资源可稳定使用关键词检索；启用本地语义模型前请保留足够磁盘空间，并避免同时导入大量资料。")
    else:
        recommendations.append("当前资源适合按需启用本地语义模型；运行队列仍保持受控，避免用并发数量换取不可解释的桌面卡顿。")
    if storage_state != "sufficient":
        recommendations.append("数据目录可用空间偏低，请在导入大批资料或下载本地 Embedding 模型前清理空间；现有资料不会被自动删除。")
    if performance_state == "attention":
        recommendations.append("近期阶段耗时出现偏高值；请先等待当前索引/深度任务完成，再检查是否存在大文件、磁盘空间不足或本地语义模型首次加载。")
    elif performance_state == "insufficient_data":
        recommendations.append("尚未积累足够的真实耗时样本；建议完成一次索引或检索后再查看更有针对性的提示。")
    if index_observation.sample_count and index_observation.p95_elapsed_ms and index_observation.p95_elapsed_ms > _SLOW_INDEX_P95_MS:
        recommendations.append("索引 P95 耗时偏高，建议分批导入材料，或暂不启用本地语义向量构建。")
    if retrieval_observation.sample_count and retrieval_observation.p95_elapsed_ms and retrieval_observation.p95_elapsed_ms > _SLOW_RETRIEVAL_P95_MS:
        recommendations.append("检索 P95 耗时偏高，建议先缩小资料库范围；不要通过提高 top_k 来掩盖定位问题。")
    if deep_task_observation.sample_count and deep_task_observation.p95_elapsed_ms and deep_task_observation.p95_elapsed_ms > _SLOW_DEEP_TASK_P95_MS:
        recommendations.append("深度任务耗时偏高，建议优先让当前任务完成或暂停；检查点会保留，避免重复启动相同任务。")
    return recommendations[:6]
