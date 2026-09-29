"""Offline regression for the constrained single-source MP4 EDL delivery path.

The script makes its own synthetic video with local FFmpeg. It does not read user media,
call a Provider, or use network access. It covers successful read-back, source bounds,
project isolation, queued cancellation, and restart reconciliation without rerendering.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_edl_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_MEDIA_EDL_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "media_edl")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-edl-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.database.task_repository import list_workflow_artifacts, list_workflow_tool_calls, load_workflow_run  # noqa: E402
from app.schemas.media_edl import MediaEditDecisionList  # noqa: E402
from app.services.media_edl_delivery import (  # noqa: E402
    _render_path,
    cancel_media_edl_task,
    create_media_edl_queued_run,
    get_media_edl_task_result,
    recover_interrupted_media_edl_tasks,
    run_media_edl_task,
)
from app.services.media_source_preparation import (  # noqa: E402
    import_media_source_bytes,
    probe_media_source,
    render_media_edl,
    verify_media_edl_render,
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
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25:duration=8",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:sample_rate=48000:duration=8",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0 or not path.is_file():
        raise RuntimeError("could not generate the synthetic EDL fixture")


async def _run() -> dict[str, object]:
    ffmpeg, ffprobe = _tool_paths()
    fixture_path = VERIFY_ROOT / "fixture.mp4"
    _make_fixture(fixture_path, ffmpeg)

    project = create_media_project(title="EDL delivery fixture")
    source = import_media_source_bytes(
        project_scope=project.project_id,
        filename="fixture.mp4",
        content=fixture_path.read_bytes(),
    )
    probe = probe_media_source(
        source_id=source.source_id,
        expected_project_scope=project.project_id,
        ffprobe_executable=ffprobe,
    )
    assert probe.duration_seconds and 7.5 <= probe.duration_seconds <= 8.5

    edl = MediaEditDecisionList.model_validate(
        {
            "source_id": source.source_id,
            "clips": [{"begin_ms": 1000, "end_ms": 3000}, {"begin_ms": 5000, "end_ms": 7000}],
        }
    )
    success_task_id = "task_media_edl_0123456789ab"
    create_media_edl_queued_run(task_id=success_task_id, project_id=project.project_id, edl=edl)
    success = await run_media_edl_task(task_id=success_task_id, project_id=project.project_id, edl=edl)
    assert success.status == "completed" and success.render is not None, success
    assert success.render.requested_duration_ms == 4000
    assert abs(success.render.rendered_duration_ms - 4000) <= 500
    artifacts = list_workflow_artifacts(success_task_id)
    calls = list_workflow_tool_calls(success_task_id)
    assert len(artifacts) == 1 and artifacts[0].mime_type == "video/mp4"
    assert artifacts[0].metadata["verification"]["passed"] is True
    assert len(calls) == 1 and calls[0].result["verification_passed"] is True

    invalid_task_id = "task_media_edl_123456789abc"
    invalid_edl = MediaEditDecisionList.model_validate(
        {"source_id": source.source_id, "clips": [{"begin_ms": 0, "end_ms": 9000}]}
    )
    create_media_edl_queued_run(task_id=invalid_task_id, project_id=project.project_id, edl=invalid_edl)
    invalid = await run_media_edl_task(task_id=invalid_task_id, project_id=project.project_id, edl=invalid_edl)
    assert invalid.status == "failed" and invalid.failure_reason == "validation_failed"

    other_project = create_media_project(title="EDL isolation fixture")
    isolation_task_id = "task_media_edl_23456789abcd"
    create_media_edl_queued_run(task_id=isolation_task_id, project_id=other_project.project_id, edl=edl)
    isolated = await run_media_edl_task(task_id=isolation_task_id, project_id=other_project.project_id, edl=edl)
    assert isolated.status == "failed" and isolated.failure_reason == "validation_failed"

    cancelled_task_id = "task_media_edl_3456789abcde"
    create_media_edl_queued_run(task_id=cancelled_task_id, project_id=project.project_id, edl=edl)
    cancelled = await cancel_media_edl_task(cancelled_task_id)
    assert cancelled is not None and cancelled.accepted
    after_cancel = await run_media_edl_task(task_id=cancelled_task_id, project_id=project.project_id, edl=edl)
    assert after_cancel.status == "cancelled"

    recovery_task_id = "task_media_edl_456789abcdef"
    create_media_edl_queued_run(task_id=recovery_task_id, project_id=project.project_id, edl=edl)
    render_media_edl(
        edl=edl,
        expected_project_scope=project.project_id,
        output_path=_render_path(recovery_task_id),
        ffprobe_executable=ffprobe,
        ffmpeg_executable=ffmpeg,
    )
    verifier_calls = 0

    def tracking_verifier(**kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal verifier_calls
        verifier_calls += 1
        return verify_media_edl_render(**kwargs)  # type: ignore[arg-type]

    recovered = recover_interrupted_media_edl_tasks(verifier=tracking_verifier)
    assert recovery_task_id in recovered and verifier_calls == 1
    recovered_result = get_media_edl_task_result(recovery_task_id)
    assert recovered_result is not None and recovered_result.status == "completed"
    recovery_run = load_workflow_run(recovery_task_id)
    assert recovery_run is not None and recovery_run.metrics.tool_call_total == 1

    with TestClient(create_app()) as client:
        result_response = client.get(f"/api/agents/media_agent/edl-renders/{success_task_id}/result")
        assert result_response.status_code == 200 and result_response.json()["status"] == "completed"
        download_response = client.get(
            f"/api/agents/media_agent/projects/{project.project_id}/edl-renders/{success_task_id}/download"
        )
        assert download_response.status_code == 200 and download_response.headers["content-type"].startswith("video/mp4")
        assert len(download_response.content) == success.render.size_bytes

        # Qt 客户端在用户确认后走同一份受限 EDL 的“受理 -> 轮询 -> 下载”协议；
        # 此处不复用已完成任务，确保 POST 路由和异步交付也确实可用。
        started = client.post(
            f"/api/agents/media_agent/projects/{project.project_id}/edl-renders/start",
            json=edl.model_dump(mode="json"),
        )
        assert started.status_code == 202, started.text
        api_task_id = started.json()["task_id"]
        api_result: dict[str, object] | None = None
        for _ in range(160):
            response = client.get(f"/api/agents/media_agent/edl-renders/{api_task_id}/result")
            assert response.status_code == 200, response.text
            candidate = response.json()
            if candidate["status"] == "completed":
                api_result = candidate
                break
            assert candidate["status"] in {"pending", "running"}, candidate
            time.sleep(0.1)
        assert api_result is not None and api_result["render"]["source_id"] == source.source_id
        api_download = client.get(
            f"/api/agents/media_agent/projects/{project.project_id}/edl-renders/{api_task_id}/download"
        )
        assert api_download.status_code == 200 and api_download.headers["content-type"].startswith("video/mp4")
        assert len(api_download.content) == api_result["render"]["size_bytes"]

    return {
        "ok": True,
        "network_used": False,
        "model_used": False,
        "source_duration_ms": int(round(probe.duration_seconds * 1000)),
        "rendered_duration_ms": success.render.rendered_duration_ms,
        "clip_count": success.render.clip_count,
        "cases": [
            "success",
            "source_bounds",
            "project_isolation",
            "queued_cancel",
            "restart_reconcile",
            "api_download",
            "api_start_poll_download",
        ],
    }


def main() -> None:
    try:
        print(json.dumps(asyncio.run(_run()), ensure_ascii=False, sort_keys=True))
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
