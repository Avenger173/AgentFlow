"""将已验证的视频讲解计划确定性转为可编辑 PPTX。

本模块只读取已经完成的 ``VideoBriefPlan``，复用 PPT Studio 的固定版式和回读器。
它不再调用 LLM、不联网，也不接受客户端提供的图片、时间码、PPTX 路径或页面正文。
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import re
import shutil
from threading import RLock
from time import perf_counter
from uuid import uuid4

from pptx import Presentation

from app.core.config import settings
from app.database.task_repository import (
    list_interrupted_runtime_task_ids,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.events import TaskLogEvent
from app.schemas.media_video_brief import (
    MediaVideoBriefDeliveryInfo,
    MediaVideoBriefPlanInfo,
    MediaVideoBriefPresentationDeliveryInfo,
    MediaVideoBriefPresentationRequest,
    MediaVideoBriefPresentationTaskResultResponse,
)
from app.schemas.presentation_studio import (
    PresentationStudioAssetPlan,
    PresentationStudioBrief,
    PresentationStudioDataPlan,
    PresentationStudioPlanResponse,
    PresentationStudioResearchPlan,
    PresentationStudioSlidePlan,
)
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.media_source_preparation import (
    MediaSourcePreparationError,
    extract_media_video_keyframe,
    get_media_source,
)
from app.services.media_video_brief_delivery import get_media_video_brief_task_result
from app.services.presentation_studio_delivery import (
    PresentationStudioDeliveryError,
    PresentationStudioEmbeddedImageAsset,
    write_verified_presentation_studio_plan,
)


MEDIA_VIDEO_BRIEF_PRESENTATION_STEP_ID = "media_video_brief_presentation"
MEDIA_VIDEO_BRIEF_PRESENTATION_TOOL_NAME = "presentation.render_video_brief"
MEDIA_AGENT_ID = "media_agent"
_TASK_ID_PATTERN = re.compile(r"^task_media_video_presentation_[0-9a-f]{12}$")
_TASK_LOCK = RLock()
_TASK_TIMEOUT_MS = 120_000


class MediaVideoBriefPresentationError(MediaSourcePreparationError):
    """视频讲解到 PPTX 适配中可安全展示给客户的错误。"""


def create_media_video_brief_presentation_queued_run(
    *, task_id: str, project_id: str, request: MediaVideoBriefPresentationRequest
) -> WorkflowRun:
    run = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="pending",
        summary="视频讲解 PPTX 已受理，尚未写入文件。",
        message="正在等待校验已验证的视频讲解计划与关键帧证据。",
        started_at=_now(),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "视频讲解 PPTX 已受理，等待开始受控导出。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


def run_media_video_brief_presentation_task(
    *, task_id: str, project_id: str, request: MediaVideoBriefPresentationRequest
) -> MediaVideoBriefPresentationTaskResultResponse:
    """一次确认后确定性写入 PPTX，不重新规划或调用模型。"""

    started_at = _now()
    started_clock = perf_counter()
    with _TASK_LOCK:
        _save_running_run(task_id=task_id, project_id=project_id, request=request, started_at=started_at)
    try:
        plan, upstream_delivery = _load_verified_video_brief(
            project_id=project_id,
            video_brief_task_id=request.video_brief_task_id,
        )
        studio_plan = _build_studio_plan(task_id=task_id, plan=plan)
        output_path = _presentation_path(task_id)
        assets, assets_by_slide_id = _extract_verified_keyframes(
            project_id=project_id,
            plan=plan,
            upstream_delivery=upstream_delivery,
            task_id=task_id,
        )
        verification, motion = write_verified_presentation_studio_plan(
            target=output_path,
            plan=studio_plan,
            assets=assets,
            assets_by_slide_id=assets_by_slide_id,
        )
        delivery = MediaVideoBriefPresentationDeliveryInfo(
            sha256=_sha256_file(output_path),
            size_bytes=output_path.stat().st_size,
            slide_count=verification.slide_count,
            source_slide_count=verification.source_slide_count,
            embedded_keyframe_count=len(assets),
            created_at=_now(),
        )
        artifact = _artifact_for_delivery(
            task_id=task_id,
            project_id=project_id,
            request=request,
            plan=plan,
            delivery=delivery,
            output_path=output_path,
            motion_enabled=motion.enabled,
        )
    except (MediaVideoBriefPresentationError, PresentationStudioDeliveryError, MediaSourcePreparationError) as exc:
        return _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed" if isinstance(exc, MediaVideoBriefPresentationError) else "delivery_verification_failed",
            message=str(exc),
        )
    except Exception:
        return _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="unexpected",
            message="视频讲解 PPTX 生成发生未预期错误，未登记交付物。",
        )

    completed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="completed",
        summary="视频讲解已转换为可编辑 PPTX，并通过回读验证。",
        message=(
            f"已复用 {len(plan.chapters)} 个讲解章节和 {len(assets)} 张受控关键帧生成 "
            f"{delivery.slide_count} 页可编辑 PPTX；未重新调用模型。"
        ),
        started_at=started_at,
        duration_ms=_duration_ms(started_clock),
        source_id=plan.source_id,
        delivery=delivery,
        artifact=artifact,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=completed,
            events=[
                _event(task_id, 1, "task_queued", "视频讲解 PPTX 已受理，等待开始受控导出。"),
                _event(task_id, 2, "task_started", "正在复核已验证的视频讲解计划与源视频关键帧。"),
                _event(task_id, 3, "presentation_render_verified", completed.steps[0].message),
                _event(task_id, 4, "artifact_saved", "可编辑 PPTX 已通过回读并登记为交付物。"),
            ],
            plan=None,
            artifacts=[artifact],
            tool_calls=[_tool_call(completed)],
        )
    return _result_from_run(completed)


def get_media_video_brief_presentation_task_result(
    task_id: str,
) -> MediaVideoBriefPresentationTaskResultResponse | None:
    run = load_workflow_run(task_id)
    return _result_from_run(run) if _is_presentation_run(run) and run is not None else None


def resolve_media_video_brief_presentation_download_path(*, project_id: str, task_id: str) -> tuple[Path, str]:
    run = load_workflow_run(task_id)
    if not _is_presentation_run(run) or run is None or run.status != "completed":
        raise MediaVideoBriefPresentationError("未找到已验证的视频讲解 PPTX。")
    output = _step(run).output
    if output.get("project_id") != project_id:
        raise MediaVideoBriefPresentationError("当前项目无权读取该视频讲解 PPTX。")
    delivery = _delivery_from_output(output)
    if delivery is None:
        raise MediaVideoBriefPresentationError("视频讲解 PPTX 缺少已验证交付信息。")
    path = _presentation_path(task_id)
    if not path.is_file() or path.stat().st_size != delivery.size_bytes or _sha256_file(path) != delivery.sha256:
        raise MediaVideoBriefPresentationError("视频讲解 PPTX 文件回读不一致，已停止下载。")
    opened = Presentation(path)
    if len(opened.slides) != delivery.slide_count:
        raise MediaVideoBriefPresentationError("视频讲解 PPTX 页数回读不一致，已停止下载。")
    return path, "video-brief.pptx"


def recover_interrupted_media_video_brief_presentation_tasks() -> list[str]:
    """重启后显式终结中断的本地导出，避免把未知半成品当成可下载文件。"""

    recovered: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        run = load_workflow_run(task_id)
        if not _is_presentation_run(run) or run is None:
            continue
        output = _step(run).output
        try:
            request = MediaVideoBriefPresentationRequest.model_validate(
                {
                    "video_brief_task_id": output.get("video_brief_task_id"),
                    "confirmed": output.get("confirmed"),
                }
            )
        except ValueError:
            continue
        failed = _build_run(
            task_id=task_id,
            project_id=str(output.get("project_id", "")),
            request=request,
            status="failed",
            summary="服务重启中断视频讲解 PPTX，未登记未验证交付物。",
            message="服务重启时 PPTX 尚未完成回读验证，请重新确认后导出。",
            started_at=run.metrics.started_at or _now(),
            duration_ms=run.metrics.duration_ms,
            failure_reason="delivery_verification_failed",
        )
        events = list(load_task_log_events(task_id) or [])
        save_workflow_run(
            run=failed,
            events=[*events, _event(task_id, len(events) + 1, "task_interrupted_by_restart", failed.steps[0].message, level="warning")],
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(failed)],
        )
        _presentation_path(task_id).unlink(missing_ok=True)
        recovered.append(task_id)
    return recovered


def _load_verified_video_brief(
    *, project_id: str, video_brief_task_id: str
) -> tuple[MediaVideoBriefPlanInfo, MediaVideoBriefDeliveryInfo]:
    result = get_media_video_brief_task_result(video_brief_task_id)
    source_run = load_workflow_run(video_brief_task_id)
    if result is None or source_run is None or result.status != "completed" or result.plan is None or result.delivery is None:
        raise MediaVideoBriefPresentationError("请先完成并保留一份已验证的视频讲解网页。")
    source_output = next(
        (step.output for step in source_run.steps if step.step_id == "media_video_brief"),
        {},
    )
    if source_output.get("project_id") != project_id:
        raise MediaVideoBriefPresentationError("讲解网页与当前视频项目不一致，不能交接为 PPTX。")
    source = get_media_source(result.plan.source_id, expected_project_scope=project_id)
    if source.source_sha256 != result.plan.source_sha256:
        raise MediaVideoBriefPresentationError("讲解计划与当前受控视频哈希不一致，不能生成 PPTX。")
    if len(result.delivery.keyframes) != len(result.plan.chapters):
        raise MediaVideoBriefPresentationError("讲解网页的关键帧证据不完整，不能生成 PPTX。")
    return result.plan, result.delivery


def _extract_verified_keyframes(
    *,
    project_id: str,
    plan: MediaVideoBriefPlanInfo,
    upstream_delivery: MediaVideoBriefDeliveryInfo,
    task_id: str,
) -> tuple[tuple[PresentationStudioEmbeddedImageAsset, ...], dict[str, PresentationStudioEmbeddedImageAsset]]:
    expected = {item.chapter_id: item for item in upstream_delivery.keyframes}
    root = settings.media_video_presentation_output_dir
    root.mkdir(parents=True, exist_ok=True)
    frame_dir = root / f".{task_id}.frames"
    frame_dir.mkdir(parents=True, exist_ok=False)
    assets: list[PresentationStudioEmbeddedImageAsset] = []
    by_slide_id: dict[str, PresentationStudioEmbeddedImageAsset] = {}
    try:
        for index, chapter in enumerate(plan.chapters, start=1):
            recorded = expected.get(chapter.chapter_id)
            if recorded is None or recorded.timestamp_ms != chapter.keyframe_timestamp_ms:
                raise MediaVideoBriefPresentationError("讲解网页的关键帧时间证据与章节计划不一致。")
            frame_path = frame_dir / f"{chapter.chapter_id}.jpg"
            frame = extract_media_video_keyframe(
                source_id=plan.source_id,
                expected_project_scope=project_id,
                timestamp_ms=chapter.keyframe_timestamp_ms,
                output_path=frame_path,
            )
            image_bytes = frame_path.read_bytes()
            if frame.sha256 != recorded.sha256 or sha256(image_bytes).hexdigest() != recorded.sha256:
                raise MediaVideoBriefPresentationError("重新提取的关键帧与已验证讲解网页不一致，已停止生成 PPTX。")
            asset = PresentationStudioEmbeddedImageAsset(
                asset_id=f"{task_id}:{chapter.chapter_id}",
                image_bytes=image_bytes,
                credit_text=f"源视频关键帧 · {_format_time(chapter.keyframe_timestamp_ms)}",
                audit_source={
                    "provider": "controlled_video_keyframe",
                    "video_source_id": plan.source_id,
                    "video_source_sha256": plan.source_sha256,
                    "chapter_id": chapter.chapter_id,
                    "timestamp_ms": chapter.keyframe_timestamp_ms,
                    "sha256": recorded.sha256,
                    "upstream_delivery": "video_brief_html",
                },
            )
            assets.append(asset)
            by_slide_id[f"chapter_{index}"] = asset
            if index == 1:
                by_slide_id["cover"] = asset
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)
    if not assets:
        raise MediaVideoBriefPresentationError("视频讲解计划没有可嵌入的关键帧。")
    return tuple(assets), by_slide_id


def _build_studio_plan(*, task_id: str, plan: MediaVideoBriefPlanInfo) -> PresentationStudioPlanResponse:
    slides: list[PresentationStudioSlidePlan] = [
        PresentationStudioSlidePlan(
            slide_id="cover",
            role="cover",
            title=plan.title,
            bullets=[],
            layout="cover",
            visual_direction="封面使用已验证的源视频关键帧。",
        ),
        PresentationStudioSlidePlan(
            slide_id="agenda",
            role="agenda",
            title="讲解结构",
            bullets=[chapter.title for chapter in plan.chapters],
            layout="agenda",
            visual_direction="章节顺序由源视频时间线确定。",
        ),
    ]
    for index, chapter in enumerate(plan.chapters, start=1):
        evidence = (
            f"来源时间 {_format_time(chapter.begin_ms)} - {_format_time(chapter.end_ms)}"
            f"，关键帧 {_format_time(chapter.keyframe_timestamp_ms)}，句段 "
            + ", ".join(str(item) for item in chapter.sentence_ids)
        )
        slides.append(
            PresentationStudioSlidePlan(
                slide_id=f"chapter_{index}",
                role="content",
                title=chapter.title,
                bullets=[*(fact.text for fact in chapter.facts), evidence],
                layout="image_statement",
                visual_direction="本页关键帧与文字均指向已验证的视频时间证据。",
            )
        )
    slides.extend(
        (
            PresentationStudioSlidePlan(
                slide_id="summary",
                role="summary",
                title="讲解回顾",
                bullets=[
                    f"本演示复用 {len(plan.chapters)} 个已验证视频章节，不重新调用模型。",
                    "每页内容来自固定讲解计划，关键帧由同一受控视频重新提取并核对哈希。",
                    "可编辑 PPTX 仅补充版式与原生动效，不改变原视频或转写内容。",
                ],
                layout="summary",
                visual_direction="总结页保留来源边界，不添加外部事实。",
            ),
            PresentationStudioSlidePlan(
                slide_id="sources",
                role="sources",
                title="来源与时间证据",
                bullets=[
                    f"受控源视频：{plan.source_id} · SHA-256 {plan.source_sha256[:16]}…",
                    *(
                        f"{chapter.title}：{_format_time(chapter.begin_ms)} - {_format_time(chapter.end_ms)}；"
                        f"关键帧 {_format_time(chapter.keyframe_timestamp_ms)}；句段 "
                        + ", ".join(str(item) for item in chapter.sentence_ids)
                        for chapter in plan.chapters
                    ),
                    "内容只基于已验证转写与源视频关键帧，不包含联网补充或未核验事实。",
                ],
                layout="sources",
                visual_direction="来源页列出每章可回溯的视频时间范围。",
            ),
        )
    )
    return PresentationStudioPlanResponse(
        task_id=task_id,
        plan_id=_plan_id(task_id=task_id, plan=plan),
        mode="fallback",
        brief=PresentationStudioBrief(
            title=plan.title,
            purpose="将已验证的视频讲解转换为带关键帧和来源时间的可编辑 PPTX。",
            audience="需要复核视频内容、章节结论和原视频时间证据的观看者。",
            core_message="每一页内容均复用已验证的视频讲解计划，关键帧可回溯至源视频时间。",
            theme="technology_emerald",
            theme_reason="视频讲解采用清晰的技术信息层级，突出章节、关键帧与来源时间。",
            fact_check_notice="内容只来自已验证转写与受控源视频关键帧；不调用模型、不联网补充事实。",
        ),
        slides=slides,
        asset_plan=PresentationStudioAssetPlan(
            state="not_requested",
            notice="本次只嵌入已验证的源视频关键帧，不请求外部视觉素材。",
        ),
        research_plan=PresentationStudioResearchPlan(
            notice="本次不读取公开资料；来源页只列出受控视频的时间证据。",
        ),
        data_plan=PresentationStudioDataPlan(
            notice="本次不生成数据图表，避免将视频口播误写为未经核验的数值。",
        ),
        warnings=["此 PPTX 由已验证视频讲解计划确定性生成，未执行新的模型规划。"],
    )


def _artifact_for_delivery(
    *,
    task_id: str,
    project_id: str,
    request: MediaVideoBriefPresentationRequest,
    plan: MediaVideoBriefPlanInfo,
    delivery: MediaVideoBriefPresentationDeliveryInfo,
    output_path: Path,
    motion_enabled: bool,
) -> WorkflowArtifact:
    suffix = task_id.rsplit("_", maxsplit=1)[-1]
    return WorkflowArtifact(
        artifact_id=f"artifact_media_video_presentation_{suffix}",
        task_id=task_id,
        step_id=MEDIA_VIDEO_BRIEF_PRESENTATION_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        kind="file",
        name="video-brief.pptx",
        summary=(
            f"可编辑 PPTX | {delivery.slide_count} 页 | {delivery.embedded_keyframe_count} 张受控关键帧"
            f" | {'已写入原生动效' if motion_enabled else '无原生动效'} | 已验证"
        ),
        uri=f"agentflow-output://media_video_presentations/{project_id}/{task_id}/video-brief.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        metadata={
            "runtime": True,
            "output_scope": "media_video_presentations",
            "output_path": str(output_path),
            "output_size_bytes": delivery.size_bytes,
            "project_id": project_id,
            "video_brief_task_id": request.video_brief_task_id,
            "source_id": plan.source_id,
            "source_sha256": plan.source_sha256,
            "sha256": delivery.sha256,
            "slide_count": delivery.slide_count,
            "source_slide_count": delivery.source_slide_count,
            "embedded_keyframe_count": delivery.embedded_keyframe_count,
            "model_used": False,
            "network_used": False,
            "verification": {"passed": True, "native_motion": motion_enabled, "reused_video_brief": True},
        },
        created_at=delivery.created_at,
    )


def _save_running_run(
    *, task_id: str, project_id: str, request: MediaVideoBriefPresentationRequest, started_at: str
) -> None:
    running = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="running",
        summary="正在将已验证的视频讲解转为可编辑 PPTX。",
        message="正在复核讲解计划、关键帧哈希与受控输出位置。",
        started_at=started_at,
    )
    save_workflow_run(
        run=running,
        events=[
            _event(task_id, 1, "task_queued", "视频讲解 PPTX 已受理，等待开始受控导出。"),
            _event(task_id, 2, "task_started", running.steps[0].message),
        ],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )


def _persist_failed(
    *,
    task_id: str,
    project_id: str,
    request: MediaVideoBriefPresentationRequest,
    duration_ms: int,
    failure_reason: str,
    message: str,
) -> MediaVideoBriefPresentationTaskResultResponse:
    failed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="failed",
        summary="视频讲解 PPTX 未完成，未登记未验证交付物。",
        message=message,
        started_at=_started_at(task_id),
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=failed,
            events=[
                _event(task_id, 1, "task_queued", "视频讲解 PPTX 已受理，等待开始受控导出。"),
                _event(task_id, 2, "task_started", "正在复核讲解计划、关键帧哈希与受控输出位置。"),
                _event(task_id, 3, "task_failed", message, level="error"),
            ],
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(failed)],
        )
    _presentation_path(task_id).unlink(missing_ok=True)
    return _result_from_run(failed)


def _build_run(
    *,
    task_id: str,
    project_id: str,
    request: MediaVideoBriefPresentationRequest,
    status: str,
    summary: str,
    message: str,
    started_at: str,
    duration_ms: int = 0,
    failure_reason: str | None = None,
    source_id: str | None = None,
    delivery: MediaVideoBriefPresentationDeliveryInfo | None = None,
    artifact: WorkflowArtifact | None = None,
) -> WorkflowRun:
    output: dict[str, object] = {
        "project_id": project_id,
        "video_brief_task_id": request.video_brief_task_id,
        "confirmed": request.confirmed,
        "source_id": source_id,
        "message": message,
        "failure_reason": failure_reason,
        "write_scope": "output/media_video_presentations",
        "model_used": False,
        "network_used": False,
    }
    if delivery is not None:
        output["delivery"] = delivery.model_dump(mode="json")
    if artifact is not None:
        output["artifact_id"] = artifact.artifact_id
    step_status = status if status in {"pending", "running", "completed", "failed", "cancelled"} else "failed"
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level="medium",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_VIDEO_BRIEF_PRESENTATION_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_VIDEO_BRIEF_PRESENTATION_TOOL_NAME,
                status=step_status,  # type: ignore[arg-type]
                message=message,
                risk_level="medium",
                output=output,
            )
        ],
        limits=RuntimeExecutionLimits(
            max_steps=1,
            max_tool_calls=1,
            max_retries_per_tool=0,
            tool_timeout_ms=_TASK_TIMEOUT_MS,
            task_timeout_ms=_TASK_TIMEOUT_MS,
            token_budget=0,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now() if status in {"completed", "failed", "cancelled"} else "",
            duration_ms=duration_ms,
            step_total=1,
            step_completed=1 if status == "completed" else 0,
            step_failed=1 if status == "failed" else 0,
            tool_call_total=1 if status in {"completed", "failed"} else 0,
            tool_call_failed=1 if status == "failed" else 0,
        ),
    )


def _result_from_run(run: WorkflowRun) -> MediaVideoBriefPresentationTaskResultResponse:
    output = _step(run).output
    return MediaVideoBriefPresentationTaskResultResponse(
        task_id=run.task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", _step(run).message)),
        failure_reason=output.get("failure_reason"),
        video_brief_task_id=output.get("video_brief_task_id") if isinstance(output.get("video_brief_task_id"), str) else None,
        source_id=output.get("source_id") if isinstance(output.get("source_id"), str) else None,
        delivery=_delivery_from_output(output),
        artifact_id=output.get("artifact_id") if isinstance(output.get("artifact_id"), str) else None,
    )


def _tool_call(run: WorkflowRun) -> WorkflowToolCall:
    output = _step(run).output
    delivery = _delivery_from_output(output)
    result: dict[str, object] = {"model_used": False, "network_used": False, "reused_video_brief": True}
    if delivery is not None:
        result.update(
            {
                "slide_count": delivery.slide_count,
                "embedded_keyframe_count": delivery.embedded_keyframe_count,
                "sha256": delivery.sha256,
            }
        )
    if output.get("failure_reason"):
        result["failure_reason"] = output["failure_reason"]
    return WorkflowToolCall(
        call_id=f"call_media_video_presentation_{run.task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=run.task_id,
        step_id=MEDIA_VIDEO_BRIEF_PRESENTATION_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_VIDEO_BRIEF_PRESENTATION_TOOL_NAME,
        status="completed" if run.status == "completed" else "failed" if run.status == "failed" else "running",
        risk_level="medium",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TASK_TIMEOUT_MS,
        duration_ms=run.metrics.duration_ms,
        request={
            "project_id": output.get("project_id"),
            "video_brief_task_id": output.get("video_brief_task_id"),
            "write_scope": "output/media_video_presentations",
            "model_used": False,
            "network_used": False,
        },
        result=result,
        error="" if run.status != "failed" else str(output.get("message", "")),
        finished_at=_now() if run.status in {"completed", "failed"} else "",
    )


def _delivery_from_output(output: dict[str, object]) -> MediaVideoBriefPresentationDeliveryInfo | None:
    raw = output.get("delivery")
    if not isinstance(raw, dict):
        return None
    try:
        return MediaVideoBriefPresentationDeliveryInfo.model_validate(raw)
    except ValueError:
        return None


def _presentation_path(task_id: str) -> Path:
    if _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise MediaVideoBriefPresentationError("视频讲解 PPTX 任务标识无效。")
    root = settings.media_video_presentation_output_dir
    root.mkdir(parents=True, exist_ok=True)
    path = (root / f"{task_id}.pptx").resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise MediaVideoBriefPresentationError("视频讲解 PPTX 输出路径无效。") from exc
    return path


def _is_presentation_run(run: WorkflowRun | None) -> bool:
    return bool(run and _TASK_ID_PATTERN.fullmatch(run.task_id) and any(
        step.step_id == MEDIA_VIDEO_BRIEF_PRESENTATION_STEP_ID
        and step.action == MEDIA_VIDEO_BRIEF_PRESENTATION_TOOL_NAME
        for step in run.steps
    ))


def _step(run: WorkflowRun) -> WorkflowStepRun:
    return next(step for step in run.steps if step.step_id == MEDIA_VIDEO_BRIEF_PRESENTATION_STEP_ID)


def _started_at(task_id: str) -> str:
    run = load_workflow_run(task_id)
    return run.metrics.started_at if run is not None and run.metrics.started_at else _now()


def _event(task_id: str, sequence: int, event: str, message: str, *, level: str = "info") -> TaskLogEvent:
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event,
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_VIDEO_BRIEF_PRESENTATION_STEP_ID,
        level=level,  # type: ignore[arg-type]
        message=message,
    )


def _format_time(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds // 1_000)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def _plan_id(*, task_id: str, plan: MediaVideoBriefPlanInfo) -> str:
    payload = f"video-brief-presentation-v1:{task_id}:{plan.source_sha256}:{plan.transcription_task_id}".encode("utf-8")
    return sha256(payload).hexdigest()[:48]


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1_000))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
