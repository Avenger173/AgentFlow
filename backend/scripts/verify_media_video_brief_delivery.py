"""MM-5B 离线视频讲解 HTML 的端到端回归。

使用本机 FFmpeg 生成合成视频、注入固定的结构化模型计划，不访问网络也不调用真实模型。
它覆盖句段证据绑定、关键帧回读、离线 Reveal 交付、项目隔离、API 下载和模型代码拒绝。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_video_brief_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_MEDIA_VIDEO_BRIEF_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "media_video_briefs")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-video-brief-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import media_agent as media_api  # noqa: E402
from app.database.task_repository import list_workflow_artifacts, list_workflow_tool_calls  # noqa: E402
from app.schemas.media_source import MediaTranscriptionSegmentInfo  # noqa: E402
from app.schemas.media_video_brief import (  # noqa: E402
    MediaVideoBriefModelChapter,
    MediaVideoBriefModelFact,
    MediaVideoBriefModelPlan,
    MediaVideoBriefRequest,
)
from app.services.media_source_preparation import import_media_source_bytes, probe_media_source  # noqa: E402
from app.services.media_video_brief_delivery import (  # noqa: E402
    create_media_video_brief_queued_run,
    get_media_video_brief_task_result,
    resolve_media_video_brief_download_path,
    run_media_video_brief_task,
)
from app.services.media_video_brief_planning import (  # noqa: E402
    MediaVideoBriefPlanningContext,
    MediaVideoBriefPlanningError,
    build_media_video_brief_plan,
    load_media_video_brief_planning_context,
    parse_media_video_brief_model_plan,
)
from app.services.media_workspace import create_media_project  # noqa: E402
from main import create_app  # noqa: E402


def _tool_paths() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found on PATH")
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    if not Path(ffprobe).is_file():
        raise RuntimeError("ffprobe was not found next to ffmpeg")
    return ffmpeg, ffprobe


def _make_fixture(path: Path, ffmpeg: str) -> None:
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=5",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0 or not path.is_file():
        raise RuntimeError("could not generate the synthetic video brief fixture")


def _context(*, project_id: str, source_id: str, source_sha256: str, request: MediaVideoBriefRequest) -> MediaVideoBriefPlanningContext:
    return MediaVideoBriefPlanningContext(
        project_id=project_id,
        request=request,
        source_id=source_id,
        source_sha256=source_sha256,
        segments=(
            MediaTranscriptionSegmentInfo(sentence_id=10, text="系统先展示问题背景。", begin_ms=200, end_ms=1_100),
            MediaTranscriptionSegmentInfo(sentence_id=20, text="随后介绍核心处理流程。", begin_ms=1_150, end_ms=2_400),
            MediaTranscriptionSegmentInfo(sentence_id=30, text="最后说明交付结果与后续动作。", begin_ms=2_500, end_ms=4_300),
        ),
    )


def _model_plan() -> MediaVideoBriefModelPlan:
    return MediaVideoBriefModelPlan(
        action="brief",
        title="产品演示讲解",
        chapters=[
            MediaVideoBriefModelChapter(
                title="问题与处理流程",
                sentence_ids=[10, 20],
                facts=[MediaVideoBriefModelFact(text="视频先说明背景，再介绍处理流程。", sentence_ids=[10, 20])],
                layout="chapter",
                animation="appear",
            ),
            MediaVideoBriefModelChapter(
                title="交付结果",
                sentence_ids=[30],
                facts=[MediaVideoBriefModelFact(text="视频最后说明交付结果与后续动作。", sentence_ids=[30])],
                layout="summary",
                animation="auto_animate",
            ),
        ],
    )


async def _fixed_planner(**_: object) -> MediaVideoBriefModelPlan:
    return _model_plan()


async def _run() -> dict[str, object]:
    ffmpeg, ffprobe = _tool_paths()
    fixture_path = VERIFY_ROOT / "fixture.mp4"
    _make_fixture(fixture_path, ffmpeg)
    project = create_media_project(title="Video brief fixture")
    source_bytes = fixture_path.read_bytes()
    source = import_media_source_bytes(project_scope=project.project_id, filename="fixture.mp4", content=source_bytes)
    probe = probe_media_source(source_id=source.source_id, expected_project_scope=project.project_id, ffprobe_executable=ffprobe)
    assert probe.video_streams and probe.duration_seconds and 4.5 <= probe.duration_seconds <= 5.5

    request = MediaVideoBriefRequest(
        transcription_task_id="task_media_transcription_0123456789ab",
        goal="把这段视频整理成有章节、关键画面和动效的离线讲解网页。",
    )
    context = _context(
        project_id=project.project_id,
        source_id=source.source_id,
        source_sha256=sha256(source_bytes).hexdigest(),
        request=request,
    )
    loaded_project_ids: list[str] = []

    def verified_transcript_loader(*, task_id: str, expected_project_id: str) -> SimpleNamespace:
        assert task_id == request.transcription_task_id
        loaded_project_ids.append(expected_project_id)
        return SimpleNamespace(
            project_id=project.project_id,
            request=SimpleNamespace(source_id=source.source_id),
            audio=SimpleNamespace(source_sha256=source.source_sha256),
            transcript=SimpleNamespace(segments=context.segments),
        )

    loaded_context = load_media_video_brief_planning_context(
        project_id=project.project_id,
        request=request,
        transcript_loader=verified_transcript_loader,
    )
    assert loaded_context.source_id == source.source_id and loaded_project_ids == [project.project_id]
    plan, clarification = build_media_video_brief_plan(context=context, model_plan=_model_plan())
    assert plan is not None and clarification is None and len(plan.chapters) == 2
    assert plan.chapters[0].begin_ms == 200 and plan.chapters[1].keyframe_timestamp_ms == 3400

    try:
        parse_media_video_brief_model_plan('{"action":"brief","title":"x","chapters":[],"javascript":"alert(1)"}')
    except MediaVideoBriefPlanningError:
        pass
    else:
        raise AssertionError("model-generated arbitrary field must be rejected")
    normalized_chapter_list = parse_media_video_brief_model_plan(
        '[{"title":"问题背景","sentence_ids":[10],"facts":[{"text":"背景说明","sentence_ids":[10]}],"layout":"chapter","animation":"appear"}]'
    )
    assert normalized_chapter_list.action == "brief"
    assert normalized_chapter_list.title == "问题背景"
    assert len(normalized_chapter_list.chapters) == 1
    wide_evidence = parse_media_video_brief_model_plan(
        '{"action":"brief","title":"长讲解","chapters":[{"title":"连续说明","sentence_ids":'
        + str(list(range(36)))
        + ',"facts":[{"text":"一条跨多句段的保守概括","sentence_ids":'
        + str(list(range(12)))
        + '}],"layout":"chapter","animation":"appear"}],"clarification_question":""}'
    )
    assert len(wide_evidence.chapters[0].sentence_ids) == 36
    assert len(wide_evidence.chapters[0].facts[0].sentence_ids) == 12
    try:
        parse_media_video_brief_model_plan('[{"title":"错误章节","sentence_ids":[10],"facts":[],"layout":"freeform"}]')
    except MediaVideoBriefPlanningError:
        pass
    else:
        raise AssertionError("a malformed chapter list must not be normalized")

    def context_loader(**kwargs: object) -> MediaVideoBriefPlanningContext:
        assert kwargs["project_id"] == project.project_id
        return context

    task_id = "task_media_video_brief_0123456789ab"
    create_media_video_brief_queued_run(task_id=task_id, project_id=project.project_id, request=request)
    result = await run_media_video_brief_task(
        task_id=task_id,
        project_id=project.project_id,
        request=request,
        runtime=object(),  # fake planner never reads ModelRuntime
        planner=_fixed_planner,
        context_loader=context_loader,
    )
    assert result.status == "completed" and result.plan is not None and result.delivery is not None, result
    assert len(result.delivery.keyframes) == 2 and result.delivery.reveal_version == "6.0.1"
    output_path, filename = resolve_media_video_brief_download_path(project_id=project.project_id, task_id=task_id)
    assert filename == "video-brief.html" and output_path.is_file()
    html = output_path.read_text(encoding="utf-8")
    assert html.count("data:image/jpeg;base64,") == 2
    assert "<script src=" not in html and "<link rel=" not in html and "Reveal.initialize(" in html
    artifacts = list_workflow_artifacts(task_id)
    calls = list_workflow_tool_calls(task_id)
    assert len(artifacts) == 1 and artifacts[0].mime_type == "text/html"
    assert artifacts[0].metadata["verification"]["offline"] is True
    assert len(calls) == 1 and calls[0].result["model_generated_code"] is False

    other_project = create_media_project(title="Video brief isolation fixture")
    try:
        resolve_media_video_brief_download_path(project_id=other_project.project_id, task_id=task_id)
    except Exception:
        pass
    else:
        raise AssertionError("cross-project HTML download must be rejected")

    original_runner = media_api.run_media_video_brief_task

    async def api_runner(**kwargs: object):  # type: ignore[no-untyped-def]
        return await run_media_video_brief_task(
            **kwargs,
            runtime=object(),
            planner=_fixed_planner,
            context_loader=context_loader,
        )

    try:
        media_api.run_media_video_brief_task = api_runner
        with TestClient(create_app()) as client:
            missing_material = client.post(
                f"/api/agents/media_agent/projects/{project.project_id}/video-briefs/start",
                json={"transcription_task_id": "task_media_transcription_0123456789ab", "goal": ""},
            )
            assert missing_material.status_code == 422
            started = client.post(
                f"/api/agents/media_agent/projects/{project.project_id}/video-briefs/start",
                json=request.model_dump(mode="json"),
            )
            assert started.status_code == 202, started.text
            api_task_id = started.json()["task_id"]
            api_result: dict[str, object] | None = None
            for _ in range(100):
                response = client.get(f"/api/agents/media_agent/video-briefs/{api_task_id}/result")
                assert response.status_code == 200, response.text
                candidate = response.json()
                if candidate["status"] == "completed":
                    api_result = candidate
                    break
                assert candidate["status"] in {"pending", "running"}, candidate
                time.sleep(0.08)
            assert api_result is not None and api_result["delivery"]["reveal_version"] == "6.0.1"
            download = client.get(
                f"/api/agents/media_agent/projects/{project.project_id}/video-briefs/{api_task_id}/download"
            )
            assert download.status_code == 200 and download.headers["content-type"].startswith("text/html")
            assert b"Reveal.initialize(" in download.content
    finally:
        media_api.run_media_video_brief_task = original_runner

    return {
        "ok": True,
        "network_used": False,
        "model_used": False,
        "chapter_count": len(plan.chapters),
        "cases": ["plan_evidence", "model_code_rejected", "offline_html", "project_isolation", "api_start_poll_download"],
    }


def main() -> None:
    try:
        print(_run_result())
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


def _run_result() -> dict[str, object]:
    return asyncio.run(_run())


if __name__ == "__main__":
    main()
