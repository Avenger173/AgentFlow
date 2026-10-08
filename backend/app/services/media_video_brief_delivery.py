"""从受限视频讲解计划生成离线 Reveal HTML 交付物。"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
import html
import os
from pathlib import Path
import re
from threading import RLock
from time import perf_counter

from app.core.config import settings
from app.database.task_repository import list_interrupted_runtime_task_ids, load_task_log_events, load_workflow_run, save_workflow_run
from app.schemas.events import TaskLogEvent
from app.schemas.media_video_brief import (
    MediaVideoBriefDeliveryInfo,
    MediaVideoBriefKeyframeInfo,
    MediaVideoBriefModelPlan,
    MediaVideoBriefPlanInfo,
    MediaVideoBriefRequest,
    MediaVideoBriefTaskResultResponse,
)
from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import RuntimeExecutionLimits, RuntimeExecutionMetrics, TaskControlResponse, WorkflowArtifact, WorkflowRun, WorkflowStepRun, WorkflowToolCall
from app.services.media_source_preparation import (
    MediaSourcePreparationError,
    MediaToolExecutionError,
    extract_media_video_keyframe,
)
from app.services.media_video_brief_planning import (
    MediaVideoBriefPlanningContext,
    MediaVideoBriefPlanningError,
    build_media_video_brief_plan,
    generate_media_video_brief_model_plan,
    load_media_video_brief_planning_context,
)
from app.services.model_gateway import ModelGatewayError, ModelRuntime, resolve_model_runtime_for_route
from app.services.task_event_stream import publish_live_task_event


MEDIA_VIDEO_BRIEF_STEP_ID = "media_video_brief"
MEDIA_VIDEO_BRIEF_TOOL_NAME = "media.create_video_brief"
MEDIA_AGENT_ID = "media_agent"
_TASK_TIMEOUT_MS = 150_000
_TOOL_TIMEOUT_MS = 130_000
_TASK_ID_PATTERN = re.compile(r"^task_media_video_brief_[0-9a-f]{12}$")
_TASK_LOCK = RLock()
_REVEAL_VERSION = "6.0.1"

Planner = Callable[..., Awaitable[MediaVideoBriefModelPlan]]
ContextLoader = Callable[..., MediaVideoBriefPlanningContext]


def create_media_video_brief_queued_run(
    *, task_id: str, project_id: str, request: MediaVideoBriefRequest
) -> WorkflowRun:
    run = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="pending",
        summary="视频讲解网页已受理，尚未向模型发送转写上下文。",
        message="正在等待校验已完成的转写交付和受控视频素材。",
        started_at=_now(),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "视频讲解网页已受理，尚未调用模型。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_media_video_brief_task(
    *,
    task_id: str,
    project_id: str,
    request: MediaVideoBriefRequest,
    runtime: ModelRuntime | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    planner: Planner = generate_media_video_brief_model_plan,
    context_loader: ContextLoader = load_media_video_brief_planning_context,
) -> MediaVideoBriefTaskResultResponse:
    """调用一次模型生成计划，再用固定模板交付一个离线 HTML。"""

    started_at = _now()
    started_clock = perf_counter()
    with _TASK_LOCK:
        current = load_workflow_run(task_id)
        if _is_cancelled_run(current):
            assert current is not None
            return _result_from_run(current)
        _save_running_run(task_id=task_id, project_id=project_id, request=request, started_at=started_at)

    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        message="正在校验已完成的转写交付和受控视频素材。",
    )
    try:
        context = await asyncio.to_thread(context_loader, project_id=project_id, request=request)
        active_runtime, active_audit = _resolve_runtime(runtime=runtime, route_audit=route_audit)
    except (MediaVideoBriefPlanningError, ModelGatewayError) as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            message=str(exc),
        )
    except Exception:  # pragma: no cover - unexpected loader defects must not orphan a queued task.
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            message="视频讲解前置交付校验发生未预期错误，未调用模型。",
        )

    with _TASK_LOCK:
        _save_running_run(
            task_id=task_id,
            project_id=project_id,
            request=request,
            started_at=started_at,
            route_audit=active_audit,
            model_requested=True,
        )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        message="正在从受限转写句段生成带来源绑定的讲解计划。",
    )
    try:
        model_plan = await planner(runtime=active_runtime, context=context)
        plan, clarification_question = build_media_video_brief_plan(context=context, model_plan=model_plan)
    except ModelGatewayError as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="provider_outcome_unknown",
            message=f"视频讲解模型调用未获得可验证结果：{exc}。为避免重复计费，任务不会自动重试。",
            route_audit=active_audit,
            model_requested=True,
        )
    except MediaVideoBriefPlanningError as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="contract_failed",
            message=str(exc),
            route_audit=active_audit,
            model_requested=True,
        )
    except Exception:  # pragma: no cover - Provider adapters may raise outside their declared error contract.
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="unexpected",
            message="视频讲解模型调用发生未预期错误，未生成可交付网页。",
            route_audit=active_audit,
            model_requested=True,
        )

    if plan is None:
        completed = _build_run(
            task_id=task_id,
            project_id=project_id,
            request=request,
            status="completed",
            summary="视频讲解需要补充目标，尚未生成网页。",
            message="模型请求澄清；没有提取关键帧或创建 HTML。",
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            clarification_question=clarification_question,
            route_audit=active_audit,
            model_requested=True,
        )
        with _TASK_LOCK:
            save_workflow_run(
                run=completed,
                events=[*_running_events(task_id), _event(task_id, 4, "task_completed", completed.steps[0].message)],
                plan=None,
                artifacts=[],
                tool_calls=[_tool_call(completed)],
            )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id=MEDIA_AGENT_ID,
            step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
            message=completed.steps[0].message,
        )
        return _result_from_run(completed)

    # 计划先持久化。若服务在后续确定性渲染时重启，可以复用计划而不会重复模型调用。
    with _TASK_LOCK:
        _save_running_run(
            task_id=task_id,
            project_id=project_id,
            request=request,
            started_at=started_at,
            route_audit=active_audit,
            model_requested=True,
            plan=plan,
        )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        message="正在提取受控关键帧并渲染离线动态讲解网页。",
    )
    output_path = _brief_path(task_id)
    try:
        delivery = await asyncio.to_thread(
            render_media_video_brief_html,
            plan=plan,
            expected_project_scope=project_id,
            output_path=output_path,
        )
        _validate_delivery(delivery=delivery, plan=plan, output_path=output_path)
    except (MediaSourcePreparationError, MediaToolExecutionError, OSError, ValueError) as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="delivery_verification_failed",
            message=f"视频讲解网页未完成：{exc}",
            route_audit=active_audit,
            model_requested=True,
            plan=plan,
        )

    artifact = _artifact_for_delivery(task_id=task_id, project_id=project_id, plan=plan, delivery=delivery, output_path=output_path)
    completed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="completed",
        summary="视频讲解网页已生成并通过离线资源回读校验。",
        message="已用固定 Reveal 模板生成离线 HTML；每章事实、关键帧和时间码均绑定至已验证转写。",
        started_at=started_at,
        duration_ms=_duration_ms(started_clock),
        plan=plan,
        delivery=delivery,
        artifact=artifact,
        route_audit=active_audit,
        model_requested=True,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=completed,
            events=[*_running_events(task_id), _event(task_id, 5, "task_completed", completed.steps[0].message)],
            plan=None,
            artifacts=[artifact],
            tool_calls=[_tool_call(completed)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_completed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        message=completed.steps[0].message,
    )
    return _result_from_run(completed)


def get_media_video_brief_task_result(task_id: str) -> MediaVideoBriefTaskResultResponse | None:
    run = load_workflow_run(task_id)
    return _result_from_run(run) if _is_media_video_brief_run(run) and run is not None else None


def resolve_media_video_brief_download_path(*, project_id: str, task_id: str) -> tuple[Path, str]:
    run = load_workflow_run(task_id)
    if not _is_media_video_brief_run(run) or run is None or run.status != "completed":
        raise MediaSourcePreparationError("未找到已验证的视频讲解网页。")
    output = _step(run).output
    if output.get("project_id") != project_id:
        raise MediaSourcePreparationError("当前视频讲解网页不属于此项目。")
    plan = _plan_from_output(output)
    delivery = _delivery_from_output(output)
    if plan is None or delivery is None:
        raise MediaSourcePreparationError("视频讲解网页交付记录不完整。")
    path = _brief_path(task_id)
    _validate_delivery(delivery=delivery, plan=plan, output_path=path)
    return path, "video-brief.html"


async def cancel_media_video_brief_task(task_id: str) -> TaskControlResponse | None:
    """只允许模型调用前取消，避免中断后不清楚 Provider 是否已计费。"""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_media_video_brief_run(run):
            return None
        assert run is not None
        if run.status != "pending":
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="视频讲解已经开始或结束，不能安全取消当前任务。",
                workflow_run=run,
            )
        output = _step(run).output
        request = _request_from_output(output)
        cancelled = _build_run(
            task_id=task_id,
            project_id=str(output["project_id"]),
            request=request,
            status="cancelled",
            summary="视频讲解在模型调用前已取消。",
            message="视频讲解已取消；没有向模型发送转写，也没有创建 HTML。",
            started_at=run.metrics.started_at or _now(),
        )
        events = list(load_task_log_events(task_id) or [])
        save_workflow_run(
            run=cancelled,
            events=[*events, _event(task_id, len(events) + 1, "task_cancelled", cancelled.steps[0].message, level="warning")],
            plan=None,
            artifacts=[],
            tool_calls=[],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        level="warning",
        message=cancelled.steps[0].message,
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message=cancelled.steps[0].message,
        workflow_run=cancelled,
    )


def recover_interrupted_media_video_brief_tasks() -> list[str]:
    """只复用已持久化计划做确定性回读或渲染，绝不重放模型请求。"""

    recovered: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        with _TASK_LOCK:
            run = load_workflow_run(task_id)
            if not _is_media_video_brief_run(run):
                continue
            assert run is not None
            output = _step(run).output
            request = _request_from_output(output)
            plan = _plan_from_output(output)
            project_id = str(output.get("project_id", ""))
            if plan is None:
                _persist_restart_failure(run, request=request, message="服务重启中断模型调用，未重放以避免重复计费。")
                recovered.append(task_id)
                continue
            try:
                output_path = _brief_path(task_id)
                delivery = _delivery_from_output(output)
                if delivery is not None:
                    _validate_delivery(delivery=delivery, plan=plan, output_path=output_path)
                else:
                    delivery = render_media_video_brief_html(
                        plan=plan,
                        expected_project_scope=project_id,
                        output_path=output_path,
                    )
                    _validate_delivery(delivery=delivery, plan=plan, output_path=output_path)
            except (MediaSourcePreparationError, MediaToolExecutionError, OSError, ValueError) as exc:
                _persist_restart_failure(run, request=request, message=f"服务重启后讲解网页无法通过回读：{exc}")
            else:
                artifact = _artifact_for_delivery(
                    task_id=task_id,
                    project_id=project_id,
                    plan=plan,
                    delivery=delivery,
                    output_path=output_path,
                )
                completed = _build_run(
                    task_id=task_id,
                    project_id=project_id,
                    request=request,
                    status="completed",
                    summary="服务重启后的视频讲解网页已通过回读校验。",
                    message="已复用先前持久化的计划完成确定性网页交付，没有重放模型调用。",
                    started_at=run.metrics.started_at or _now(),
                    duration_ms=run.metrics.duration_ms,
                    plan=plan,
                    delivery=delivery,
                    artifact=artifact,
                    route_audit=run.model_routes[0] if run.model_routes else None,
                    model_requested=bool(output.get("model_requested")),
                )
                events = list(load_task_log_events(task_id) or [])
                save_workflow_run(
                    run=completed,
                    events=[*events, _event(task_id, len(events) + 1, "task_reconciled_after_restart", completed.steps[0].message, level="warning")],
                    plan=None,
                    artifacts=[artifact],
                    tool_calls=[_tool_call(completed)],
                )
            recovered.append(task_id)
    return recovered


def render_media_video_brief_html(
    *, plan: MediaVideoBriefPlanInfo, expected_project_scope: str, output_path: Path
) -> MediaVideoBriefDeliveryInfo:
    """提取受控关键帧并填充本地固定 Reveal 模板，生成单文件离线 HTML。"""

    final_path = output_path.resolve()
    _validate_brief_output_path(final_path)
    frame_dir = final_path.parent / f".{final_path.stem}.frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    keyframes: list[MediaVideoBriefKeyframeInfo] = []
    encoded_frames: dict[str, str] = {}
    try:
        for chapter in plan.chapters:
            frame_path = frame_dir / f"{chapter.chapter_id}.jpg"
            frame = extract_media_video_keyframe(
                source_id=plan.source_id,
                expected_project_scope=expected_project_scope,
                timestamp_ms=chapter.keyframe_timestamp_ms,
                output_path=frame_path,
            )
            frame_bytes = frame_path.read_bytes()
            if _sha256_bytes(frame_bytes) != frame.sha256:
                raise ValueError("关键帧文件哈希与回读结果不一致。")
            encoded_frames[chapter.chapter_id] = base64.b64encode(frame_bytes).decode("ascii")
            keyframes.append(
                MediaVideoBriefKeyframeInfo(
                    chapter_id=chapter.chapter_id,
                    timestamp_ms=frame.timestamp_ms,
                    sha256=frame.sha256,
                    size_bytes=frame.size_bytes,
                    width=frame.width,
                    height=frame.height,
                )
            )
        document = _render_fixed_reveal_document(plan=plan, encoded_frames=encoded_frames)
        content = document.encode("utf-8")
        _verify_rendered_html_bytes(content=content, expected_chapter_count=len(plan.chapters))
        _atomic_write_bytes(final_path, content)
        return MediaVideoBriefDeliveryInfo(
            sha256=_sha256_bytes(content),
            size_bytes=len(content),
            keyframes=keyframes,
            created_at=_now(),
        )
    except Exception:
        final_path.unlink(missing_ok=True)
        raise
    finally:
        for frame_path in frame_dir.glob("*.jpg"):
            frame_path.unlink(missing_ok=True)
        try:
            frame_dir.rmdir()
        except OSError:
            pass


def _render_fixed_reveal_document(*, plan: MediaVideoBriefPlanInfo, encoded_frames: dict[str, str]) -> str:
    reveal_css = _vendor_file("reveal.css").read_text(encoding="utf-8")
    reveal_js = _vendor_file("reveal.js").read_text(encoding="utf-8")
    source_label = html.escape(plan.source_id)
    title = html.escape(plan.title)
    goal = html.escape(plan.goal)
    chapter_markup: list[str] = []
    for index, chapter in enumerate(plan.chapters, start=1):
        image = encoded_frames.get(chapter.chapter_id)
        if not image:
            raise ValueError("关键帧编码缺失。")
        transition = "fade" if chapter.animation == "fade" else "slide"
        auto_animate = ' data-auto-animate="true"' if chapter.animation == "auto_animate" else ""
        facts = "".join(
            f'<li class="fragment fade-up">{html.escape(fact.text)}<small>来源 { _format_time(fact.begin_ms) } - { _format_time(fact.end_ms) } · 句段 { ", ".join(str(item) for item in fact.sentence_ids) }</small></li>'
            for fact in chapter.facts
        )
        chapter_markup.append(
            f'<section data-transition="{transition}"{auto_animate}>'
            f'<div class="brief-slide brief-{chapter.layout}">'
            f'<div class="brief-index">{index:02d}</div>'
            f'<div class="brief-copy"><p class="eyebrow">VIDEO BRIEF · { _format_time(chapter.begin_ms) } - { _format_time(chapter.end_ms) }</p>'
            f'<h2>{html.escape(chapter.title)}</h2><ul>{facts}</ul>'
            f'<p class="evidence">视频来源：{source_label} · 关键帧：{_format_time(chapter.keyframe_timestamp_ms)} · 句段 {chapter.keyframe_sentence_id}</p>'
            f'</div><img class="brief-frame" alt="{html.escape(chapter.title)} 的源视频关键帧" src="data:image/jpeg;base64,{image}"></div></section>'
        )
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="AgentFlow VideoBriefPlan v1"><title>{title}</title>
<!-- reveal.js {_REVEAL_VERSION}, MIT License; vendored from https://github.com/hakimel/reveal.js -->
<style>{reveal_css}\n{_FIXED_BRIEF_CSS}</style></head><body>
<div class="reveal"><div class="slides">
<section data-transition="fade"><div class="brief-cover"><p class="eyebrow">AGENTFLOW · OFFLINE VIDEO BRIEF</p><h1>{title}</h1><p>{goal}</p><small>内容仅基于已验证转写与源视频关键帧；使用方向键或屏幕控件浏览。</small></div></section>
{''.join(chapter_markup)}
<section data-transition="fade"><div class="brief-cover"><p class="eyebrow">TRACEABLE DELIVERY</p><h2>章节、事实与关键帧均可回溯至原视频时间码</h2><p>源素材：{source_label}</p></div></section>
</div></div>
<script>{reveal_js}</script><script>{_FIXED_REVEAL_INIT}</script></body></html>'''


_FIXED_REVEAL_INIT = """Reveal.initialize({hash:true,controls:true,progress:true,center:true,transition:'slide',backgroundTransition:'fade',autoAnimateEasing:'ease-out',autoAnimateDuration:0.55});"""
_FIXED_BRIEF_CSS = """
:root { --brief-ink:#10213e; --brief-blue:#2573d7; --brief-cyan:#12b8a6; --brief-surface:#f4f8ff; }
.reveal { color:var(--brief-ink); font-family:"Microsoft YaHei","Segoe UI",sans-serif; }
.reveal .slides { text-align:left; }
.brief-cover,.brief-slide { min-height:550px; box-sizing:border-box; padding:56px 64px; background:var(--brief-surface); border:1px solid #c8d8ee; }
.brief-cover { display:flex; flex-direction:column; justify-content:center; background:#e9f2ff; }
.brief-cover h1,.brief-cover h2 { color:#123a72; line-height:1.15; }
.brief-cover p { max-width:980px; color:#40516c; }
.brief-slide { display:grid; grid-template-columns:minmax(0,1.05fr) minmax(320px,.95fr); gap:38px; align-items:center; position:relative; }
.brief-index { position:absolute; top:24px; right:32px; color:#7a9ac3; font-size:28px; font-weight:700; }
.brief-copy h2 { margin:0 0 24px; color:#123a72; font-size:1.45em; }
.brief-copy ul { margin:0; padding-left:1.15em; }
.brief-copy li { margin:0 0 16px; color:#263c5e; line-height:1.45; }
.brief-copy small { display:block; color:#6580a7; font-size:.45em; margin-top:6px; }
.brief-frame { width:100%; max-height:410px; object-fit:cover; border:5px solid #fff; box-shadow:0 12px 32px rgba(21,66,120,.18); }
.eyebrow { color:var(--brief-blue); font-size:.42em; font-weight:700; letter-spacing:0; margin:0 0 16px; }
.evidence { color:#6680a5; font-size:.42em; margin:26px 0 0; }
"""


def _vendor_file(name: str) -> Path:
    path = Path(__file__).resolve().parents[1] / "static" / "vendor" / f"revealjs-{_REVEAL_VERSION}" / name
    if not path.is_file() or path.stat().st_size < 100:
        raise MediaSourcePreparationError("离线动态网页模板资源不完整，请重新安装 reveal.js 运行时文件。")
    return path


def _verify_rendered_html_bytes(*, content: bytes, expected_chapter_count: int) -> None:
    if len(content) < 1_024 or len(content) > 32 * 1024 * 1024:
        raise ValueError("离线讲解网页大小不在允许范围内。")
    decoded = content.decode("utf-8")
    required = ("<div class=\"reveal\">", "Reveal.initialize(", "data:image/jpeg;base64,", "TRACEABLE DELIVERY")
    if any(marker not in decoded for marker in required):
        raise ValueError("离线讲解网页缺少固定模板或内嵌关键帧。")
    if decoded.count("data:image/jpeg;base64,") != expected_chapter_count:
        raise ValueError("离线讲解网页的关键帧数量与计划不一致。")
    if "<script src=" in decoded or "<link rel=" in decoded:
        raise ValueError("离线讲解网页不允许引用外部脚本或样式资源。")


def _validate_delivery(*, delivery: MediaVideoBriefDeliveryInfo, plan: MediaVideoBriefPlanInfo, output_path: Path) -> None:
    if len(delivery.keyframes) != len(plan.chapters):
        raise ValueError("讲解网页关键帧数量与计划不一致。")
    if not output_path.is_file() or output_path.stat().st_size != delivery.size_bytes:
        raise ValueError("视频讲解网页不存在或大小发生变化。")
    if _sha256_file(output_path) != delivery.sha256:
        raise ValueError("视频讲解网页哈希与已验证交付不一致。")
    _verify_rendered_html_bytes(content=output_path.read_bytes(), expected_chapter_count=len(plan.chapters))


def _artifact_for_delivery(
    *, task_id: str, project_id: str, plan: MediaVideoBriefPlanInfo, delivery: MediaVideoBriefDeliveryInfo, output_path: Path
) -> WorkflowArtifact:
    suffix = task_id.rsplit("_", maxsplit=1)[-1]
    return WorkflowArtifact(
        artifact_id=f"artifact_media_video_brief_{suffix}",
        task_id=task_id,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        kind="file",
        name="video-brief.html",
        summary=f"离线动态 HTML | {len(plan.chapters)} 章 | Reveal {_REVEAL_VERSION} | 已验证",
        uri=f"agentflow-output://media_video_brief/{project_id}/{task_id}/video-brief.html",
        mime_type="text/html",
        metadata={
            "runtime": True,
            "output_scope": "media_video_briefs",
            "output_path": str(output_path),
            "output_size_bytes": delivery.size_bytes,
            "project_id": project_id,
            "source_id": plan.source_id,
            "source_sha256": plan.source_sha256,
            "sha256": delivery.sha256,
            "chapter_count": len(plan.chapters),
            "keyframes": [item.model_dump(mode="json") for item in delivery.keyframes],
            "verification": {"passed": True, "offline": True, "reveal_version": _REVEAL_VERSION},
            "model_used": True,
            "network_used": True,
        },
        created_at=delivery.created_at,
    )


def _save_running_run(
    *, task_id: str, project_id: str, request: MediaVideoBriefRequest, started_at: str,
    route_audit: ModelRouteAuditSnapshot | None = None, model_requested: bool = False,
    plan: MediaVideoBriefPlanInfo | None = None,
) -> None:
    running = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="running",
        summary="正在生成带来源时间证据的视频讲解网页。",
        message="正在从受控转写句段生成计划并准备固定离线模板。",
        started_at=started_at,
        route_audit=route_audit,
        model_requested=model_requested,
        plan=plan,
    )
    save_workflow_run(
        run=running,
        events=_running_events(task_id),
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(running)] if model_requested else [],
    )


async def _persist_failed(
    *, task_id: str, project_id: str, request: MediaVideoBriefRequest, duration_ms: int,
    failure_reason: str, message: str, route_audit: ModelRouteAuditSnapshot | None = None,
    model_requested: bool = False, plan: MediaVideoBriefPlanInfo | None = None,
) -> MediaVideoBriefTaskResultResponse:
    failed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="failed",
        summary="视频讲解网页未完成，未登记未验证交付物。",
        message=message,
        started_at=_started_at(task_id),
        duration_ms=duration_ms,
        failure_reason=failure_reason,
        route_audit=route_audit,
        model_requested=model_requested,
        plan=plan,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=failed,
            events=[*_running_events(task_id), _event(task_id, 5, "task_failed", message, level="error")],
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(failed)] if model_requested else [],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        level="error",
        message=message,
    )
    return _result_from_run(failed)


def _persist_restart_failure(run: WorkflowRun, *, request: MediaVideoBriefRequest, message: str) -> None:
    output = _step(run).output
    failed = _build_run(
        task_id=run.task_id,
        project_id=str(output.get("project_id", "")),
        request=request,
        status="failed",
        summary="服务重启中断视频讲解，未登记未验证网页。",
        message=message,
        started_at=run.metrics.started_at or _now(),
        duration_ms=run.metrics.duration_ms,
        failure_reason="provider_outcome_unknown" if _plan_from_output(output) is None else "delivery_verification_failed",
        route_audit=run.model_routes[0] if run.model_routes else None,
        model_requested=bool(output.get("model_requested")),
        plan=_plan_from_output(output),
    )
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=failed,
        events=[*events, _event(run.task_id, len(events) + 1, "task_interrupted_by_restart", message, level="warning")],
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(failed)] if failed.metrics.tool_call_total else [],
    )


def _build_run(
    *, task_id: str, project_id: str, request: MediaVideoBriefRequest, status: str, summary: str,
    message: str, started_at: str, duration_ms: int = 0, failure_reason: str | None = None,
    plan: MediaVideoBriefPlanInfo | None = None, delivery: MediaVideoBriefDeliveryInfo | None = None,
    artifact: WorkflowArtifact | None = None, clarification_question: str | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None, model_requested: bool = False,
) -> WorkflowRun:
    output: dict[str, object] = {
        "project_id": project_id,
        "transcription_task_id": request.transcription_task_id,
        "goal": request.goal,
        "message": message,
        "failure_reason": failure_reason,
        "model_requested": model_requested,
        "write_scope": "output/media_video_briefs",
    }
    if plan is not None:
        output["plan"] = plan.model_dump(mode="json")
    if delivery is not None:
        output["delivery"] = delivery.model_dump(mode="json")
    if artifact is not None:
        output["artifact_id"] = artifact.artifact_id
    if clarification_question:
        output["clarification_question"] = clarification_question
    step_status = status if status in {"pending", "running", "completed", "failed", "cancelled"} else "failed"
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level="medium",
        steps=[WorkflowStepRun(
            step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
            agent=MEDIA_AGENT_ID,
            action=MEDIA_VIDEO_BRIEF_TOOL_NAME,
            status=step_status,  # type: ignore[arg-type]
            message=message,
            risk_level="medium",
            output=output,
        )],
        model_routes=[route_audit] if route_audit is not None else [],
        limits=RuntimeExecutionLimits(
            max_steps=1, max_tool_calls=2, max_retries_per_tool=0,
            tool_timeout_ms=_TOOL_TIMEOUT_MS, task_timeout_ms=_TASK_TIMEOUT_MS, token_budget=1_000,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now() if status in {"completed", "failed", "cancelled"} else "",
            duration_ms=duration_ms,
            step_total=1,
            step_completed=1 if status == "completed" else 0,
            step_failed=1 if status == "failed" else 0,
            tool_call_total=(2 if model_requested and delivery is not None else 1 if model_requested else 0),
            tool_call_failed=1 if status == "failed" and model_requested else 0,
            provider_model_request_total=1 if model_requested else 0,
        ),
    )


def _result_from_run(run: WorkflowRun) -> MediaVideoBriefTaskResultResponse:
    output = _step(run).output
    return MediaVideoBriefTaskResultResponse(
        task_id=run.task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", _step(run).message)),
        failure_reason=output.get("failure_reason"),
        plan=_plan_from_output(output),
        delivery=_delivery_from_output(output),
        artifact_id=output.get("artifact_id") if isinstance(output.get("artifact_id"), str) else None,
        clarification_question=output.get("clarification_question") if isinstance(output.get("clarification_question"), str) else None,
    )


def _tool_call(run: WorkflowRun) -> WorkflowToolCall:
    output = _step(run).output
    status = "completed" if run.status == "completed" else "failed" if run.status == "failed" else "running"
    plan = _plan_from_output(output)
    delivery = _delivery_from_output(output)
    result: dict[str, object] = {"offline_template": True, "model_generated_code": False}
    if plan is not None:
        result["chapter_count"] = len(plan.chapters)
    if delivery is not None:
        result.update({"sha256": delivery.sha256, "keyframe_count": len(delivery.keyframes), "reveal_version": delivery.reveal_version})
    if output.get("failure_reason"):
        result["failure_reason"] = output["failure_reason"]
    return WorkflowToolCall(
        call_id=f"call_media_video_brief_{run.task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=run.task_id,
        step_id=MEDIA_VIDEO_BRIEF_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_VIDEO_BRIEF_TOOL_NAME,
        status=status,  # type: ignore[arg-type]
        risk_level="medium",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=run.metrics.duration_ms,
        request={"project_id": output.get("project_id"), "transcription_task_id": output.get("transcription_task_id"), "write_scope": "output/media_video_briefs", "model_used": True, "network_used": True},
        result=result,
        error="" if run.status != "failed" else str(output.get("message", "")),
        finished_at=_now() if run.status in {"completed", "failed"} else "",
    )


def _plan_from_output(output: dict[str, object]) -> MediaVideoBriefPlanInfo | None:
    raw = output.get("plan")
    if not isinstance(raw, dict):
        return None
    try:
        return MediaVideoBriefPlanInfo.model_validate(raw)
    except ValueError:
        return None


def _delivery_from_output(output: dict[str, object]) -> MediaVideoBriefDeliveryInfo | None:
    raw = output.get("delivery")
    if not isinstance(raw, dict):
        return None
    try:
        return MediaVideoBriefDeliveryInfo.model_validate(raw)
    except ValueError:
        return None


def _request_from_output(output: dict[str, object]) -> MediaVideoBriefRequest:
    return MediaVideoBriefRequest.model_validate({"transcription_task_id": output.get("transcription_task_id"), "goal": output.get("goal")})


def _brief_path(task_id: str) -> Path:
    if _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError("视频讲解任务标识无效。")
    root = settings.media_video_brief_output_dir
    path = (root / f"{task_id}.html").resolve()
    _validate_brief_output_path(path)
    return path


def _validate_brief_output_path(path: Path) -> None:
    root = settings.media_video_brief_output_dir.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("视频讲解网页输出路径无效。") from exc


def _is_media_video_brief_run(run: WorkflowRun | None) -> bool:
    return bool(run and _TASK_ID_PATTERN.fullmatch(run.task_id) and any(
        step.step_id == MEDIA_VIDEO_BRIEF_STEP_ID and step.action == MEDIA_VIDEO_BRIEF_TOOL_NAME for step in run.steps
    ))


def _is_cancelled_run(run: WorkflowRun | None) -> bool:
    return bool(run and _is_media_video_brief_run(run) and run.status == "cancelled")


def _step(run: WorkflowRun) -> WorkflowStepRun:
    return next(step for step in run.steps if step.step_id == MEDIA_VIDEO_BRIEF_STEP_ID)


def _resolve_runtime(*, runtime: ModelRuntime | None, route_audit: ModelRouteAuditSnapshot | None) -> tuple[ModelRuntime, ModelRouteAuditSnapshot | None]:
    if runtime is not None:
        return runtime, route_audit
    resolution = resolve_model_runtime_for_route("media_planning")
    return resolution.runtime, resolution.audit_snapshot(stage=MEDIA_VIDEO_BRIEF_STEP_ID)


def _running_events(task_id: str) -> list[TaskLogEvent]:
    return [
        _event(task_id, 1, "task_queued", "视频讲解网页已受理，尚未调用模型。"),
        _event(task_id, 2, "task_started", "正在校验转写交付与受控视频素材。"),
        _event(task_id, 3, "tool_started", "正在生成受限讲解计划。"),
        _event(task_id, 4, "tool_started", "正在提取关键帧并渲染固定离线模板。"),
    ]


def _event(task_id: str, sequence: int, event: str, message: str, *, level: str = "info") -> TaskLogEvent:
    return TaskLogEvent(task_id=task_id, sequence=sequence, event=event, agent_id=MEDIA_AGENT_ID,
                        step_id=MEDIA_VIDEO_BRIEF_STEP_ID, level=level, message=message)  # type: ignore[arg-type]


def _started_at(task_id: str) -> str:
    run = load_workflow_run(task_id)
    return run.metrics.started_at if run is not None and run.metrics.started_at else _now()


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1_000))


def _format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1_000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
