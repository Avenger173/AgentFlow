"""Offline regression for confirmation-only, transcript-bound EDL candidates.

The script uses a fake model result and an imported byte fixture. It verifies that the
planner can select only known transcript sentence IDs, never invokes FFmpeg, creates no
output artifact, and requires a separate render request after the candidate is returned.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_edl_candidate_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_MEDIA_EDL_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "media_edl")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-edl-candidate-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.database.task_repository import list_workflow_artifacts, list_workflow_tool_calls, load_workflow_run  # noqa: E402
from app.schemas.media_edl import MediaEdlCandidateRequest, MediaEdlModelCandidate  # noqa: E402
from app.schemas.media_source import (  # noqa: E402
    MediaTranscriptInfo,
    MediaTranscriptionArtifactPayload,
    MediaTranscriptionAudioInfo,
    MediaTranscriptionRequest,
    MediaTranscriptionSegmentInfo,
)
from app.services.media_edl_candidate_delivery import (  # noqa: E402
    create_media_edl_candidate_queued_run,
    get_media_edl_candidate_task_result,
    recover_interrupted_media_edl_candidate_tasks,
    run_media_edl_candidate_task,
)
from app.services.media_edl_planning import (  # noqa: E402
    MediaEdlPlanningContext,
    MediaEdlPlanningError,
    build_media_edl_candidate,
    load_media_edl_planning_context,
    parse_media_edl_duration_constraint,
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
            duration_seconds=4.0,
            created_at=datetime.now(UTC).isoformat(),
        ),
        transcript=MediaTranscriptInfo(
            text="欢迎使用产品。它可以自动整理数据。最后给出结论。",
            segments=[
                MediaTranscriptionSegmentInfo(sentence_id=10, text="欢迎使用产品。", begin_ms=0, end_ms=1_000),
                MediaTranscriptionSegmentInfo(sentence_id=20, text="它可以自动整理数据。", begin_ms=1_100, end_ms=2_300),
                MediaTranscriptionSegmentInfo(sentence_id=30, text="最后给出结论。", begin_ms=2_500, end_ms=3_600),
            ],
        ),
        provider="fixture",
        model="fixture-model",
        provider_request_id_sha256="b" * 64,
        provider_usage={},
        created_at=datetime.now(UTC).isoformat(),
    )


async def _run() -> dict[str, object]:
    project = create_media_project(title="EDL candidate fixture")
    raw_media = b"candidate-planning-source"
    source = import_media_source_bytes(
        project_scope=project.project_id,
        filename="fixture.mp4",
        content=raw_media,
    )
    payload = _payload(
        project_id=project.project_id,
        source_id=source.source_id,
        source_sha256=sha256(raw_media).hexdigest(),
    )

    def transcript_loader(**kwargs: object) -> MediaTranscriptionArtifactPayload:
        assert kwargs["task_id"] == payload.task_id
        assert kwargs["expected_project_id"] == project.project_id
        return payload

    request = MediaEdlCandidateRequest(
        transcription_task_id=payload.task_id,
        goal="保留产品能力介绍和最后结论。",
    )
    context = load_media_edl_planning_context(
        project_id=project.project_id,
        request=request,
        transcript_loader=transcript_loader,
    )
    assert context.source_id == source.source_id and len(context.segments) == 3

    # 历史 Provider 可能把逐步增长的识别快照当作稳定句段；候选规划必须只读纠正，
    # 不能让每个后续片段重新从零秒开始，也不要求客户重做一次转写。
    cumulative_payload = payload.model_copy(
        update={
            "transcript": MediaTranscriptInfo(
                text="Intro product conclusion",
                segments=[
                    MediaTranscriptionSegmentInfo(sentence_id=10, text="Intro", begin_ms=240, end_ms=1_000),
                    MediaTranscriptionSegmentInfo(sentence_id=20, text="Intro product", begin_ms=240, end_ms=2_300),
                    MediaTranscriptionSegmentInfo(sentence_id=30, text="Intro product conclusion", begin_ms=240, end_ms=3_600),
                ],
            )
        }
    )
    cumulative_context = load_media_edl_planning_context(
        project_id=project.project_id,
        request=request,
        transcript_loader=lambda **_: cumulative_payload,
    )
    assert [(item.sentence_id, item.text, item.begin_ms, item.end_ms) for item in cumulative_context.segments] == [
        (10, "Intro", 240, 1_000),
        (20, "product", 1_000, 2_300),
        (30, "conclusion", 2_300, 3_600),
    ]

    duration_request = MediaEdlCandidateRequest(
        transcription_task_id=payload.task_id,
        goal="保留核心讲解，控制在 60 到 90 秒。",
    )
    duration_constraint = parse_media_edl_duration_constraint(duration_request.goal)
    assert (duration_constraint.minimum_duration_ms, duration_constraint.maximum_duration_ms) == (60_000, 90_000)
    duration_context = MediaEdlPlanningContext(
        project_id=project.project_id,
        request=duration_request,
        source_id=source.source_id,
        source_sha256=source.source_sha256,
        segments=(
            MediaTranscriptionSegmentInfo(sentence_id=100, text="第一段", begin_ms=0, end_ms=30_000),
            MediaTranscriptionSegmentInfo(sentence_id=200, text="第二段", begin_ms=30_000, end_ms=60_000),
            MediaTranscriptionSegmentInfo(sentence_id=300, text="第三段", begin_ms=60_000, end_ms=90_000),
            MediaTranscriptionSegmentInfo(sentence_id=400, text="第四段", begin_ms=90_000, end_ms=120_000),
        ),
        duration_constraint=duration_constraint,
    )
    in_range_candidate, _ = build_media_edl_candidate(
        context=duration_context,
        model_candidate=MediaEdlModelCandidate.model_validate(
            {"action": "candidate", "selections": [{"start_sentence_id": 100, "end_sentence_id": 300, "reason": "保留核心。"}]}
        ),
    )
    assert in_range_candidate is not None and in_range_candidate.edl.requested_duration_ms == 90_000
    adjusted_candidate, _ = build_media_edl_candidate(
        context=duration_context,
        model_candidate=MediaEdlModelCandidate.model_validate(
            {
                "action": "candidate",
                "selections": [
                    {"start_sentence_id": 100, "end_sentence_id": 200, "reason": "保留前半段。"},
                    {"start_sentence_id": 300, "end_sentence_id": 400, "reason": "超出时长。"},
                ],
            }
        ),
    )
    assert adjusted_candidate is not None
    assert adjusted_candidate.duration_adjusted is True
    assert adjusted_candidate.edl.requested_duration_ms == 90_000
    assert [(clip.begin_ms, clip.end_ms) for clip in adjusted_candidate.edl.clips] == [(0, 60_000), (60_000, 90_000)]
    unfit_context = MediaEdlPlanningContext(
        project_id=project.project_id,
        request=duration_request,
        source_id=source.source_id,
        source_sha256=source.source_sha256,
        segments=(MediaTranscriptionSegmentInfo(sentence_id=500, text="过长单句", begin_ms=0, end_ms=120_000),),
        duration_constraint=duration_constraint,
    )
    try:
        build_media_edl_candidate(
            context=unfit_context,
            model_candidate=MediaEdlModelCandidate.model_validate(
                {"action": "candidate", "selections": [{"start_sentence_id": 500, "end_sentence_id": 500, "reason": "无法在句段边界截断。"}]}
            ),
        )
    except MediaEdlPlanningError as exc:
        assert "60 秒 到 90 秒" in str(exc)
    else:
        raise AssertionError("candidate without a valid sentence-boundary fit was accepted")

    cross_project = create_media_project(title="EDL candidate isolation fixture")
    try:
        load_media_edl_planning_context(
            project_id=cross_project.project_id,
            request=request,
            transcript_loader=lambda **_: payload,
        )
    except MediaEdlPlanningError:
        pass
    else:
        raise AssertionError("candidate context accepted a transcript from another project")

    plan_calls = 0

    async def valid_planner(**kwargs: object) -> MediaEdlModelCandidate:
        nonlocal plan_calls
        plan_calls += 1
        assert kwargs["context"] == context
        return MediaEdlModelCandidate.model_validate(
            {
                "action": "candidate",
                "selections": [
                    {"start_sentence_id": 10, "end_sentence_id": 20, "reason": "介绍产品能力。"},
                    {"start_sentence_id": 30, "end_sentence_id": 30, "reason": "保留结论。"},
                ],
            }
        )

    success_task_id = "task_media_edl_plan_0123456789ab"
    create_media_edl_candidate_queued_run(task_id=success_task_id, project_id=project.project_id, request=request)
    success = await run_media_edl_candidate_task(
        task_id=success_task_id,
        project_id=project.project_id,
        request=request,
        runtime=object(),  # type: ignore[arg-type]
        planner=valid_planner,
        context_loader=lambda **_: context,
    )
    assert success.status == "completed" and success.candidate is not None
    assert success.candidate.requires_confirmation is True
    assert [clip.model_dump() for clip in success.candidate.edl.clips] == [
        {"begin_ms": 0, "end_ms": 2_300},
        {"begin_ms": 2_500, "end_ms": 3_600},
    ]
    assert plan_calls == 1
    assert not list_workflow_artifacts(success_task_id)
    calls = list_workflow_tool_calls(success_task_id)
    assert len(calls) == 1 and calls[0].result["candidate_generated"] is True
    assert "goal" not in calls[0].request
    assert not (VERIFY_ROOT / "output" / "media_edl").exists()

    normalized_task_id = "task_media_edl_plan_abcdef012345"
    create_media_edl_candidate_queued_run(
        task_id=normalized_task_id,
        project_id=project.project_id,
        request=request,
    )

    async def relevance_ordered_planner(**_: object) -> MediaEdlModelCandidate:
        # 模型可能按“认为重要”的顺序返回范围；Harness 必须按源时间顺序展示，且不能重复
        # 交叠的句段内容。
        return MediaEdlModelCandidate.model_validate(
            {
                "action": "candidate",
                "selections": [
                    {"start_sentence_id": 20, "end_sentence_id": 30, "reason": "保留产品能力。"},
                    {"start_sentence_id": 10, "end_sentence_id": 20, "reason": "补足开场说明。"},
                ],
            }
        )

    normalized = await run_media_edl_candidate_task(
        task_id=normalized_task_id,
        project_id=project.project_id,
        request=request,
        runtime=object(),  # type: ignore[arg-type]
        planner=relevance_ordered_planner,
        context_loader=lambda **_: context,
    )
    assert normalized.status == "completed" and normalized.candidate is not None
    assert [clip.model_dump() for clip in normalized.candidate.edl.clips] == [{"begin_ms": 0, "end_ms": 3_600}]
    assert normalized.candidate.selections[0].reason == "补足开场说明。；保留产品能力。"

    invalid_task_id = "task_media_edl_plan_123456789abc"
    create_media_edl_candidate_queued_run(task_id=invalid_task_id, project_id=project.project_id, request=request)

    async def invalid_planner(**_: object) -> MediaEdlModelCandidate:
        return MediaEdlModelCandidate.model_validate(
            {
                "action": "candidate",
                "selections": [{"start_sentence_id": 999, "end_sentence_id": 999, "reason": "不存在。"}],
            }
        )

    invalid = await run_media_edl_candidate_task(
        task_id=invalid_task_id,
        project_id=project.project_id,
        request=request,
        runtime=object(),  # type: ignore[arg-type]
        planner=invalid_planner,
        context_loader=lambda **_: context,
    )
    assert invalid.status == "failed" and invalid.failure_reason == "contract_failed"

    with TestClient(create_app()) as client:
        result_response = client.get(f"/api/agents/media_agent/edl-candidates/{success_task_id}/result")
        assert result_response.status_code == 200
        assert result_response.json()["candidate"]["requires_confirmation"] is True

        cancelled_task_id = "task_media_edl_plan_23456789abcd"
        create_media_edl_candidate_queued_run(
            task_id=cancelled_task_id,
            project_id=project.project_id,
            request=request,
        )
        cancel_response = client.post(f"/api/tasks/{cancelled_task_id}/cancel")
        assert cancel_response.status_code == 200 and cancel_response.json()["accepted"] is True
        cancelled = await run_media_edl_candidate_task(
            task_id=cancelled_task_id,
            project_id=project.project_id,
            request=request,
            runtime=object(),  # type: ignore[arg-type]
            planner=valid_planner,
            context_loader=lambda **_: context,
        )
        assert cancelled.status == "cancelled" and plan_calls == 1

    recovery_task_id = "task_media_edl_plan_3456789abcde"
    create_media_edl_candidate_queued_run(task_id=recovery_task_id, project_id=project.project_id, request=request)
    recovered = recover_interrupted_media_edl_candidate_tasks()
    assert recovery_task_id in recovered
    recovery_run = load_workflow_run(recovery_task_id)
    assert recovery_run is not None and recovery_run.status == "failed"
    assert get_media_edl_candidate_task_result(recovery_task_id).failure_reason == "provider_outcome_unknown"  # type: ignore[union-attr]

    return {
        "ok": True,
        "model_used": False,
        "network_used": False,
        "cases": ["source_binding", "cross_project", "candidate", "relevance_order_normalized", "invalid_sentence", "api_result", "queued_cancel", "restart_no_replay"],
        "candidate_clip_count": 2,
        "output_files_created": 0,
    }


def main() -> None:
    try:
        print(json.dumps(asyncio.run(_run()), ensure_ascii=False, sort_keys=True))
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
