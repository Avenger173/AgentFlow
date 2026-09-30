"""Offline regression for follow-up EDL candidates and deterministic SRT delivery.

The fixture creates two immutable candidate versions from one verified transcript.
The second version must bind to the first candidate but must not invoke ASR, FFmpeg,
or a second source import.  Full and edited SRT deliveries are read back, registered,
and served through the public download route.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_edl_followup_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_MEDIA_EDL_SUBTITLE_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "media_subtitles")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-edl-followup-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.database.task_repository import list_workflow_artifacts, load_workflow_run  # noqa: E402
from app.schemas.media_edl import MediaEdlCandidateRequest, MediaEdlModelCandidate  # noqa: E402
from app.schemas.media_source import (  # noqa: E402
    MediaTranscriptInfo,
    MediaTranscriptionArtifactPayload,
    MediaTranscriptionAudioInfo,
    MediaTranscriptionRequest,
    MediaTranscriptionSegmentInfo,
)
from app.services import media_edl_subtitle_delivery  # noqa: E402
from app.services.media_edl_candidate_delivery import (  # noqa: E402
    create_media_edl_candidate_queued_run,
    run_media_edl_candidate_task,
)
from app.services.media_edl_planning import load_media_edl_planning_context  # noqa: E402
from app.services.media_edl_subtitle_delivery import (  # noqa: E402
    MediaEdlSubtitleDeliveryError,
    resolve_media_edl_subtitle_download,
)
from app.services.media_source_preparation import import_media_source_bytes  # noqa: E402
from app.services.media_workspace import create_media_project  # noqa: E402
from main import create_app  # noqa: E402


def _payload(*, project_id: str, source_id: str, source_sha256: str) -> MediaTranscriptionArtifactPayload:
    return MediaTranscriptionArtifactPayload(
        task_id="task_media_transcription_0123456789ab",
        project_id=project_id,
        request=MediaTranscriptionRequest(
            source_id=source_id,
            audio_id="mda_0123456789abcdef",
            language_hints=["zh"],
        ),
        audio=MediaTranscriptionAudioInfo(
            audio_id="mda_0123456789abcdef",
            source_id=source_id,
            source_sha256=source_sha256,
            source_stream_index=0,
            sha256="a" * 64,
            size_bytes=320,
            duration_seconds=5.6,
            created_at=datetime.now(UTC).isoformat(),
        ),
        transcript=MediaTranscriptInfo(
            text="开场概述。核心功能。使用步骤。产品价值。",
            segments=[
                MediaTranscriptionSegmentInfo(sentence_id=10, text="开场概述。", begin_ms=0, end_ms=1_000),
                MediaTranscriptionSegmentInfo(sentence_id=20, text="核心功能。", begin_ms=1_100, end_ms=2_400),
                MediaTranscriptionSegmentInfo(sentence_id=30, text="使用步骤。", begin_ms=2_600, end_ms=4_000),
                MediaTranscriptionSegmentInfo(sentence_id=40, text="产品价值。", begin_ms=4_200, end_ms=5_600),
            ],
        ),
        provider="fixture",
        model="fixture-model",
        provider_request_id_sha256="b" * 64,
        provider_usage={},
        created_at=datetime.now(UTC).isoformat(),
    )


async def _run() -> None:
    project = create_media_project(title="follow-up EDL fixture")
    source_bytes = b"follow-up-edl-source"
    source = import_media_source_bytes(
        project_scope=project.project_id,
        filename="fixture.mp4",
        content=source_bytes,
    )
    payload = _payload(
        project_id=project.project_id,
        source_id=source.source_id,
        source_sha256=sha256(source_bytes).hexdigest(),
    )
    verified_transcript_reads = 0

    def transcript_loader(**kwargs: object) -> MediaTranscriptionArtifactPayload:
        nonlocal verified_transcript_reads
        verified_transcript_reads += 1
        assert kwargs["task_id"] == payload.task_id
        assert kwargs["expected_project_id"] == project.project_id
        return payload

    initial_request = MediaEdlCandidateRequest(
        transcription_task_id=payload.task_id,
        goal="保留核心功能、使用步骤和产品价值。",
    )
    initial_context = load_media_edl_planning_context(
        project_id=project.project_id,
        request=initial_request,
        transcript_loader=transcript_loader,
    )
    initial_task_id = "task_media_edl_plan_0123456789ab"
    create_media_edl_candidate_queued_run(
        task_id=initial_task_id,
        project_id=project.project_id,
        request=initial_request,
    )

    async def initial_planner(**kwargs: object) -> MediaEdlModelCandidate:
        assert kwargs["context"] == initial_context
        return MediaEdlModelCandidate.model_validate(
            {
                "action": "candidate",
                "selections": [
                    {"start_sentence_id": 20, "end_sentence_id": 40, "reason": "保留完整功能讲解。"},
                ],
            }
        )

    initial = await run_media_edl_candidate_task(
        task_id=initial_task_id,
        project_id=project.project_id,
        request=initial_request,
        runtime=object(),  # type: ignore[arg-type]
        planner=initial_planner,
        context_loader=lambda **_: initial_context,
    )
    assert initial.status == "completed" and initial.candidate is not None
    assert initial.candidate.parent_candidate_task_id is None

    followup_request = MediaEdlCandidateRequest(
        transcription_task_id=payload.task_id,
        parent_candidate_task_id=initial_task_id,
        goal="只保留使用步骤，删去开场、功能概述和结尾。",
    )
    followup_context = load_media_edl_planning_context(
        project_id=project.project_id,
        request=followup_request,
        transcript_loader=transcript_loader,
    )
    assert followup_context.parent_candidate is not None
    assert followup_context.parent_candidate.edl.clips[0].begin_ms == 1_100

    followup_task_id = "task_media_edl_plan_abcdef012345"
    create_media_edl_candidate_queued_run(
        task_id=followup_task_id,
        project_id=project.project_id,
        request=followup_request,
    )

    async def followup_planner(**kwargs: object) -> MediaEdlModelCandidate:
        context = kwargs["context"]
        assert context == followup_context
        assert context.parent_candidate is not None
        return MediaEdlModelCandidate.model_validate(
            {
                "action": "candidate",
                "selections": [
                    {"start_sentence_id": 30, "end_sentence_id": 30, "reason": "只保留操作步骤。"},
                ],
            }
        )

    followup = await run_media_edl_candidate_task(
        task_id=followup_task_id,
        project_id=project.project_id,
        request=followup_request,
        runtime=object(),  # type: ignore[arg-type]
        planner=followup_planner,
        context_loader=lambda **_: followup_context,
    )
    assert followup.status == "completed" and followup.candidate is not None
    assert followup.candidate.parent_candidate_task_id == initial_task_id
    assert [(clip.begin_ms, clip.end_ms) for clip in followup.candidate.edl.clips] == [(2_600, 4_000)]

    # Subtitle delivery uses the existing verified payload.  Replacing this loader with
    # a fixture makes any accidental ASR/provider call impossible in this regression.
    original_loader = media_edl_subtitle_delivery.load_verified_media_transcription_payload
    media_edl_subtitle_delivery.load_verified_media_transcription_payload = transcript_loader
    try:
        full_path, full_name = resolve_media_edl_subtitle_download(
            project_id=project.project_id,
            candidate_task_id=followup_task_id,
            subtitle_kind="full",
        )
        cut_path, cut_name = resolve_media_edl_subtitle_download(
            project_id=project.project_id,
            candidate_task_id=followup_task_id,
            subtitle_kind="cut",
        )
        assert full_name == "full_transcript.srt" and cut_name == "edited_subtitles.srt"
        full_content = full_path.read_text(encoding="utf-8")
        cut_content = cut_path.read_text(encoding="utf-8")
        assert "1\n00:00:00,000 --> 00:00:01,000\n开场概述。" in full_content
        assert "4\n00:00:04,200 --> 00:00:05,600\n产品价值。" in full_content
        assert cut_content == "1\n00:00:00,000 --> 00:00:01,400\n使用步骤。\n"

        repeat_path, _ = resolve_media_edl_subtitle_download(
            project_id=project.project_id,
            candidate_task_id=followup_task_id,
            subtitle_kind="cut",
        )
        assert repeat_path == cut_path

        with TestClient(create_app()) as client:
            response = client.get(
                f"/api/agents/media_agent/projects/{project.project_id}/edl-candidates/"
                f"{followup_task_id}/subtitles/cut/download"
            )
            assert response.status_code == 200, response.text
            assert response.content == cut_content.encode("utf-8")
            assert "edited_subtitles.srt" in response.headers.get("content-disposition", "")

        other_project = create_media_project(title="subtitle isolation fixture")
        try:
            resolve_media_edl_subtitle_download(
                project_id=other_project.project_id,
                candidate_task_id=followup_task_id,
                subtitle_kind="cut",
            )
        except MediaEdlSubtitleDeliveryError:
            pass
        else:
            raise AssertionError("subtitle download accepted another project's candidate")
    finally:
        media_edl_subtitle_delivery.load_verified_media_transcription_payload = original_loader

    artifacts = list_workflow_artifacts(followup_task_id)
    assert len(artifacts) == 2
    assert {artifact.metadata["subtitle_kind"] for artifact in artifacts} == {"full", "cut"}
    assert all(artifact.metadata["verification"]["passed"] for artifact in artifacts)
    assert not list_workflow_artifacts(initial_task_id)
    assert (load_workflow_run(initial_task_id) is not None and load_workflow_run(followup_task_id) is not None)
    assert not (VERIFY_ROOT / "output" / "media_edl").exists()
    assert verified_transcript_reads >= 4


def main() -> None:
    try:
        asyncio.run(_run())
        print("Media EDL follow-up and subtitle delivery verification passed.")
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
