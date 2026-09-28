"""Run one controlled real-model probe for confirmation-only EDL planning.

The fixture is a program-generated Chinese transcript.  The resulting manifest keeps
only route facts, measured usage, and selected sentence IDs; it never stores fixture
text, raw provider output, keys, media, or an MP4.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.schemas.media_edl import MediaEdlCandidateRequest  # noqa: E402
from app.schemas.media_source import MediaTranscriptionSegmentInfo  # noqa: E402
from app.services.media_edl_planning import (  # noqa: E402
    MediaEdlPlanningContext,
    MediaEdlPlanningError,
    build_media_edl_candidate,
    build_media_edl_planning_system_prompt,
    parse_media_edl_model_candidate,
)
from app.services.model_gateway import (  # noqa: E402
    ModelConversationMessage,
    ModelGatewayError,
    resolve_model_runtime_for_route,
)


ROUTE_ID = "media_planning"
MAXIMUM_TOKENS = 720


def _fixture_context() -> MediaEdlPlanningContext:
    """Return a bounded program fixture without writing its text to disk."""

    request = MediaEdlCandidateRequest(
        transcription_task_id="task_media_transcription_0123456789ab",
        goal="保留产品能力介绍和最后结论，控制在一分钟内。",
    )
    return MediaEdlPlanningContext(
        project_id="probe_project",
        request=request,
        source_id="ms_0123456789abcdef",
        source_sha256="0" * 64,
        segments=(
            MediaTranscriptionSegmentInfo(
                sentence_id=10,
                text="欢迎使用产品。",
                begin_ms=0,
                end_ms=1_200,
            ),
            MediaTranscriptionSegmentInfo(
                sentence_id=20,
                text="它可以自动整理业务数据并生成摘要。",
                begin_ms=1_500,
                end_ms=4_800,
            ),
            MediaTranscriptionSegmentInfo(
                sentence_id=30,
                text="还可以追踪来源和任务进度。",
                begin_ms=5_100,
                end_ms=8_100,
            ),
            MediaTranscriptionSegmentInfo(
                sentence_id=40,
                text="最后，团队可以根据结果继续协作。",
                begin_ms=8_500,
                end_ms=11_600,
            ),
        ),
    )


def _request_payload(context: MediaEdlPlanningContext) -> str:
    return json.dumps(
        {
            "goal": context.request.goal,
            "source_id": context.source_id,
            "segments": [
                {
                    "sentence_id": segment.sentence_id,
                    "begin_ms": segment.begin_ms,
                    "end_ms": segment.end_ms,
                    "text": segment.text,
                }
                for segment in context.segments
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def _run_probe() -> dict[str, object]:
    context = _fixture_context()
    resolution = resolve_model_runtime_for_route(ROUTE_ID, validate=True)
    bounded_tokens = max(128, min(MAXIMUM_TOKENS, resolution.runtime.max_tokens, 1024))
    runtime = replace(resolution.runtime, max_tokens=bounded_tokens)
    started_at = datetime.now(UTC)
    started_clock = perf_counter()
    turn = await runtime.tool_turn(
        system_prompt=build_media_edl_planning_system_prompt(),
        messages=[ModelConversationMessage(role="user", content=_request_payload(context))],
        tools=[],
    )
    duration_ms = max(0, int((perf_counter() - started_clock) * 1000))
    candidate = parse_media_edl_model_candidate(turn.content)
    info, clarification_question = build_media_edl_candidate(
        context=context,
        model_candidate=candidate,
    )
    if clarification_question or info is None:
        raise MediaEdlPlanningError("固定且明确的剪辑目标不应返回澄清请求。")
    return {
        "probe": "media_edl_candidate_v1",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fixture_kind": "program_generated_chinese_transcript",
        "route": ROUTE_ID,
        "provider": runtime.provider,
        "model": runtime.model,
        "temperature": runtime.temperature,
        "maximum_tokens": bounded_tokens,
        "request_count": 1,
        "duration_ms": duration_ms,
        "provider_usage": asdict(turn.usage),
        "raw_provider_output_persisted": False,
        "fixture_text_persisted": False,
        "media_imported": False,
        "ffmpeg_invoked": False,
        "output_files_created": 0,
        "candidate": {
            "requires_confirmation": info.requires_confirmation,
            "selection_count": len(info.selections),
            "sentence_id_ranges": [
                [selection.start_sentence_id, selection.end_sentence_id]
                for selection in info.selections
            ],
            "edl_duration_ms": sum(clip.end_ms - clip.begin_ms for clip in info.edl.clips),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="候选 EDL 单次真实模型探针")
    parser.add_argument("--execute", action="store_true", help="明确允许一次程序夹具的真实模型请求")
    parser.add_argument("--output-dir", type=Path, help="忽略的本地证据目录")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to submit one program-generated transcript to media_planning.")
        return

    output_dir = args.output_dir or (
        settings.data_dir / "media_evaluations" / f"media_edl_candidate_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(_run_probe())
    except (MediaEdlPlanningError, ModelGatewayError, ValueError):
        failure = {
            "probe": "media_edl_candidate_v1",
            "route": ROUTE_ID,
            "request_count": "unknown_before_verified_result",
            "passed": False,
            "failure_category": "provider_or_contract_failure",
            "raw_provider_output_persisted": False,
            "fixture_text_persisted": False,
        }
        (output_dir / "run_manifest.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"ok": False, "output_dir": str(output_dir), **failure}, ensure_ascii=False))
        raise SystemExit(1)

    result["passed"] = True
    (output_dir / "run_manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "output_dir": str(output_dir), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
