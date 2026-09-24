"""受控执行冻结的 MM-4 短媒体转写质量运行。

默认只验证质量集；``--execute`` 才会顺序提交每个夹具一次。每例在请求前均完成来源、
哈希和媒体流预检，运行中遇到失败、取消或结果未知会停止后续提交并把未提交夹具明确写为
``not_started``。它不重放请求、不读取客户媒体，也不在清单中写入 Key、媒体正文、转写正文
或 Provider 原始响应。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from media_transcription_quality import (
    RUN_TYPE,
    QualityContractError,
    FixtureRecord,
    create_self_test_bundle,
    evaluate_run,
    validate_suite,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
ROUTE_ID = "media_transcription"
_DURATION_TOLERANCE_MS = 2_000


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or run the frozen MM-4 ASR quality suite.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--suite", type=Path, help="冻结且人工标注完成的 quality suite.json")
    source.add_argument("--self-test", action="store_true", help="只验证执行器的离线契约，不调用模型")
    parser.add_argument("--execute", action="store_true", help="显式允许每个冻结夹具至多一次真实转写请求")
    parser.add_argument("--output-dir", type=Path, help="新建的 data/ 下忽略目录；仅 --execute 使用")
    parser.add_argument("--ffmpeg-path", type=Path, help="显式指定 ffmpeg.exe；仅 --execute 使用")
    parser.add_argument("--ffprobe-path", type=Path, help="显式指定 ffprobe.exe；仅 --execute 使用")
    args = parser.parse_args()

    if args.self_test:
        if args.execute or args.output_dir or args.ffmpeg_path or args.ffprobe_path:
            parser.error("--self-test cannot be combined with execution arguments")
        print(json.dumps(_run_self_test(), ensure_ascii=False))
        return
    if not args.execute:
        assert args.suite is not None
        try:
            report, _ = _preflight(args.suite.resolve(), verify_media_files=True)
        except (OSError, QualityContractError) as exc:
            _print_failure(exc)
            raise SystemExit(1) from exc
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "validate_only",
                    "suite": report,
                    "model_call_count": 0,
                    "network_call_count": 0,
                    "next_step": "pass --execute with explicit FFmpeg paths only after approving the fixed batch cost",
                },
                ensure_ascii=False,
            )
        )
        return
    if args.output_dir is None or args.ffmpeg_path is None or args.ffprobe_path is None:
        parser.error("--execute requires --output-dir, --ffmpeg-path and --ffprobe-path")

    assert args.suite is not None
    try:
        suite_path = args.suite.resolve()
        output_dir = args.output_dir.resolve()
        ffmpeg_path = _require_executable(args.ffmpeg_path, "ffmpeg")
        ffprobe_path = _require_executable(args.ffprobe_path, "ffprobe")
        suite_report, fixtures = _preflight(suite_path, verify_media_files=True)
        _validate_media_inputs(suite_path=suite_path, fixtures=fixtures, ffprobe_path=ffprobe_path)
        _require_ignored_output_directory(output_dir)
        if output_dir.exists():
            raise RuntimeError("quality run output directory already exists; never overwrite prior evidence")
    except (OSError, RuntimeError, QualityContractError) as exc:
        _print_failure(exc)
        raise SystemExit(1) from exc

    try:
        summary = asyncio.run(
            _execute_batch(
                suite_path=suite_path,
                suite_report=suite_report,
                fixtures=fixtures,
                output_dir=output_dir,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
            )
        )
    except (OSError, RuntimeError, QualityContractError) as exc:
        _print_failure(exc)
        raise SystemExit(1) from exc
    print(json.dumps(summary, ensure_ascii=False))
    if summary["run_state"] != "completed":
        raise SystemExit(1)


def _preflight(suite_path: Path, *, verify_media_files: bool) -> tuple[dict[str, object], dict[str, FixtureRecord]]:
    """只检查冻结质量契约；这里不能解析模型路由或读取 Provider 配置。"""

    return validate_suite(suite_path, verify_files=verify_media_files)


def _require_executable(path: Path, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".exe":
        raise RuntimeError(f"{name} executable is unavailable")
    return resolved


def _require_ignored_output_directory(output_dir: Path) -> None:
    data_root = (PROJECT_ROOT / "data").resolve()
    try:
        output_dir.relative_to(data_root)
    except ValueError as exc:
        raise RuntimeError("quality output must remain under the ignored project data directory") from exc
    if output_dir == data_root:
        raise RuntimeError("quality output must use a new child directory under project data")


def _validate_media_inputs(*, suite_path: Path, fixtures: dict[str, FixtureRecord], ffprobe_path: Path) -> None:
    """在任何付费请求前确认所有冻结源仍是带音视频流的对应文件。"""

    root = suite_path.parent.resolve()
    for fixture in fixtures.values():
        media_path = (root / fixture.media_file).resolve()
        try:
            media_path.relative_to(root)
        except ValueError as exc:
            raise QualityContractError(f"fixture {fixture.fixture_id} media path escapes the frozen suite") from exc
        try:
            completed = subprocess.run(
                [
                    str(ffprobe_path),
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(media_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30.0,
            )
            payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise QualityContractError(f"fixture {fixture.fixture_id} cannot be preflighted by ffprobe") from exc
        streams = payload.get("streams") if isinstance(payload, dict) else None
        format_payload = payload.get("format") if isinstance(payload, dict) else None
        if not isinstance(streams, list) or not isinstance(format_payload, dict):
            raise QualityContractError(f"fixture {fixture.fixture_id} has no readable media stream metadata")
        stream_types = {str(stream.get("codec_type") or "") for stream in streams if isinstance(stream, dict)}
        if not {"audio", "video"}.issubset(stream_types):
            raise QualityContractError(f"fixture {fixture.fixture_id} must retain both audio and video streams")
        try:
            actual_duration_ms = round(float(format_payload["duration"]) * 1000)
        except (KeyError, TypeError, ValueError) as exc:
            raise QualityContractError(f"fixture {fixture.fixture_id} has no usable media duration") from exc
        if actual_duration_ms <= 0 or abs(actual_duration_ms - fixture.duration_ms) > _DURATION_TOLERANCE_MS:
            raise QualityContractError(f"fixture {fixture.fixture_id} duration does not match its frozen record")


async def _execute_batch(
    *,
    suite_path: Path,
    suite_report: dict[str, object],
    fixtures: dict[str, FixtureRecord],
    output_dir: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> dict[str, object]:
    """按冻结顺序一次性运行；第一个未完成结果会停止批次而不是隐式重试。"""

    sys.path.insert(0, str(BACKEND_ROOT))
    # 在切换临时 Runtime 数据目录前读取已保存的模型配置；Key 仅保留在内存 runtime。
    from app.services.model_gateway import resolve_audio_model_runtime_for_route

    resolution = resolve_audio_model_runtime_for_route(ROUTE_ID, validate=True)
    runtime = resolution.runtime
    route_audit = resolution.audit_snapshot(stage=ROUTE_ID)

    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ["AGENTFLOW_DATA_DIR"] = str(output_dir / "runtime_data")
    os.environ["AGENTFLOW_OUTPUT_DIR"] = str(output_dir / "runtime_output")
    os.environ["AGENTFLOW_DATABASE_PATH"] = str(output_dir / "runtime_data" / "quality-run.db")

    from app.core.config import settings
    from app.schemas.media_source import MediaTranscriptionArtifactPayload, MediaTranscriptionRequest
    from app.services.media_source_preparation import (
        extract_primary_audio_for_transcription,
        import_media_source_bytes,
        probe_media_source,
    )
    from app.services.media_transcription_delivery import (
        create_media_transcription_queued_run,
        run_media_transcription_task,
    )
    from app.services.media_workspace import create_media_project

    started_at = datetime.now(UTC)
    run_payload = _new_run_payload(suite_report=suite_report, provider=runtime.provider, model=runtime.model, started_at=started_at)
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir()
    project = create_media_project(title="MM-4 ASR quality run")
    remaining = list(fixtures.values())
    for fixture_index, fixture in enumerate(remaining):
        case = await _execute_fixture(
            fixture=fixture,
            suite_root=suite_path.parent,
            project_id=project.project_id,
            runtime=runtime,
            route_audit=route_audit,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            artifacts_dir=artifacts_dir,
            runtime_data_dir=settings.data_dir,
            request_model=MediaTranscriptionRequest,
            artifact_model=MediaTranscriptionArtifactPayload,
            import_source=import_media_source_bytes,
            probe_source=probe_media_source,
            extract_audio=extract_primary_audio_for_transcription,
            create_run=create_media_transcription_queued_run,
            run_task=run_media_transcription_task,
        )
        run_payload["cases"].append(case)
        if case["status"] != "completed":
            for unstarted in remaining[fixture_index + 1 :]:
                run_payload["cases"].append(_not_started_case(unstarted.fixture_id))
            run_payload["run_state"] = "halted"
            run_payload["halted_after_fixture"] = fixture.fixture_id
            _write_run(output_dir, run_payload, completed_at=datetime.now(UTC))
            return _summary(output_dir, run_payload)
        _write_run(output_dir, run_payload, completed_at=None)
    run_payload["run_state"] = "completed"
    _write_run(output_dir, run_payload, completed_at=datetime.now(UTC))
    return _summary(output_dir, run_payload)


async def _execute_fixture(
    *,
    fixture: FixtureRecord,
    suite_root: Path,
    project_id: str,
    runtime: Any,
    route_audit: Any,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    artifacts_dir: Path,
    runtime_data_dir: Path,
    request_model: Any,
    artifact_model: Any,
    import_source: Any,
    probe_source: Any,
    extract_audio: Any,
    create_run: Any,
    run_task: Any,
) -> dict[str, object]:
    """完成预处理后才调用 Runtime；失败分类不携带 Provider 原文。"""

    try:
        media_path = (suite_root / fixture.media_file).resolve()
        source = import_source(project_scope=project_id, filename=media_path.name, content=media_path.read_bytes())
        probe_source(source_id=source.source_id, expected_project_scope=project_id, ffprobe_executable=ffprobe_path)
        audio = extract_audio(
            source_id=source.source_id,
            expected_project_scope=project_id,
            ffprobe_executable=ffprobe_path,
            ffmpeg_executable=ffmpeg_path,
        )
    except Exception:
        return _failed_case(fixture.fixture_id, "failed", "validation_failed", 0)

    task_id = f"task_media_transcription_{uuid4().hex[:12]}"
    request = request_model(source_id=source.source_id, audio_id=audio.audio_id, language_hints=[fixture.language])
    try:
        create_run(task_id=task_id, project_id=project_id, request=request)
        result = await run_task(
            task_id=task_id,
            project_id=project_id,
            request=request,
            runtime=runtime,
            route_audit=route_audit,
        )
    except Exception:
        # Runtime 已被触发但调用结果不能证明为未发送，保守按未知且不可重放处理。
        return _failed_case(fixture.fixture_id, "outcome_unknown", "provider_outcome_unknown", 1)

    if result.status != "completed" or result.artifact_id is None:
        return _result_failure_case(fixture.fixture_id, result)
    try:
        expected_artifact_id = f"artifact_media_transcription_{task_id.rsplit('_', maxsplit=1)[-1]}"
        if result.artifact_id != expected_artifact_id:
            raise ValueError("task result does not identify the verified transcript artifact")
        source_artifact = runtime_data_dir / "outputs" / "media_transcripts" / f"{task_id}.json"
        encoded = source_artifact.read_bytes()
        artifact = artifact_model.model_validate_json(encoded)
        if artifact.task_id != task_id or artifact.audio.source_sha256 != fixture.media_sha256:
            raise ValueError("artifact does not match frozen fixture")
        if artifact.provider != runtime.provider or artifact.model != runtime.model:
            raise ValueError("artifact route does not match fixed quality route")
        relative = f"artifacts/{fixture.fixture_id.lower()}.json"
        destination = artifacts_dir.parent / relative
        _atomic_write(destination, encoded)
    except Exception:
        return _failed_case(fixture.fixture_id, "failed", "delivery_verification_failed", 1)
    return {
        "fixture_id": fixture.fixture_id,
        "task_id": task_id,
        "status": "completed",
        "provider_call_count": 1,
        "artifact_file": relative,
        "artifact_sha256": sha256(encoded).hexdigest(),
    }


def _result_failure_case(fixture_id: str, result: Any) -> dict[str, object]:
    reason = str(getattr(result, "failure_reason", "") or "unexpected")
    if getattr(result, "status", "") == "cancelled" or reason == "cancelled":
        return _failed_case(fixture_id, "cancelled", "cancelled", 0)
    if reason == "provider_outcome_unknown":
        return _failed_case(fixture_id, "outcome_unknown", reason, 1)
    call_count = 0 if reason == "validation_failed" else 1
    return _failed_case(fixture_id, "failed", reason if reason in _failure_categories() else "unexpected", call_count)


def _failure_categories() -> set[str]:
    return {
        "validation_failed",
        "provider_rejected",
        "provider_outcome_unknown",
        "delivery_verification_failed",
        "cancelled",
        "unexpected",
    }


def _failed_case(fixture_id: str, status: str, failure_category: str, provider_call_count: int) -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "status": status,
        "failure_category": failure_category,
        "provider_call_count": provider_call_count,
    }


def _not_started_case(fixture_id: str) -> dict[str, object]:
    return _failed_case(fixture_id, "not_started", "batch_halted", 0)


def _new_run_payload(*, suite_report: dict[str, object], provider: str, model: str, started_at: datetime) -> dict[str, object]:
    return {
        "run_type": RUN_TYPE,
        "suite_sha256": suite_report["suite_sha256"],
        "route": ROUTE_ID,
        "provider": provider,
        "model": model,
        "execution_mode": "sequential_single_submit_no_retry",
        "case_provider_call_limit": 1,
        "billing_amount": "unknown",
        "started_at": started_at.isoformat(),
        "run_state": "running",
        "cases": [],
    }


def _write_run(output_dir: Path, run_payload: dict[str, object], *, completed_at: datetime | None) -> None:
    snapshot = dict(run_payload)
    snapshot["completed_at"] = completed_at.isoformat() if completed_at is not None else None
    snapshot["provider_call_count"] = sum(int(case["provider_call_count"]) for case in run_payload["cases"])
    _atomic_write(output_dir / "run.json", json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _summary(output_dir: Path, run_payload: dict[str, object]) -> dict[str, object]:
    return {
        "ok": True,
        "output_dir": str(output_dir),
        "run_state": run_payload["run_state"],
        "fixture_count": len(run_payload["cases"]),
        "provider_call_count": sum(int(case["provider_call_count"]) for case in run_payload["cases"]),
        "billing_amount": "unknown",
        "next_step": "score this exact run.json offline; do not retry incomplete fixtures in place",
    }


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_mm4_asr_quality_run_") as temporary:
        root = Path(temporary)
        suite_path, _ = create_self_test_bundle(root)
        suite_report, fixtures = _preflight(suite_path, verify_media_files=True)
        payload = _new_run_payload(
            suite_report=suite_report,
            provider="qwen_audio",
            model="qwen-audio-3.1-asr-flash",
            started_at=datetime.now(UTC),
        )
        first = next(iter(fixtures.values()))
        payload["cases"].append(_failed_case(first.fixture_id, "outcome_unknown", "provider_outcome_unknown", 1))
        for fixture in list(fixtures.values())[1:]:
            payload["cases"].append(_not_started_case(fixture.fixture_id))
        payload["run_state"] = "halted"
        _write_run(root, payload, completed_at=datetime.now(UTC))
        report = evaluate_run(suite_path, root / "run.json", verify_files=True)
        if report["quality_gate_passed"] is not False or len(report["incomplete_case_statuses"]) != 8:
            raise AssertionError("quality runner self-test hid an incomplete batch")
        _verify_fixture_execution(root=root, fixture=first)
        try:
            _require_ignored_output_directory(root / "trackable-output")
        except RuntimeError as exc:
            if "ignored project data" not in str(exc):
                raise
        else:
            raise AssertionError("quality runner accepted a trackable evidence directory")
    return {
        "ok": True,
        "self_test": True,
        "model_call_count": 0,
        "network_call_count": 0,
        "negative_contract_check": "unknown_outcome_stops_batch_and_trackable_output_is_rejected",
    }


def _verify_fixture_execution(*, root: Path, fixture: FixtureRecord) -> None:
    """以进程内替身覆盖执行器自己的导入、回读和失败分类，不触碰 Provider。"""

    runtime_data_dir = root / "runtime_data"
    artifacts_dir = root / "run_artifacts" / "artifacts"
    artifacts_dir.mkdir(parents=True)
    source = SimpleNamespace(source_id="ms_0000000000000001")
    audio = SimpleNamespace(audio_id="mda_0000000000000001")
    template = json.loads((root / "artifacts" / f"{fixture.fixture_id.lower()}.json").read_text(encoding="utf-8"))

    class ArtifactModel:
        @staticmethod
        def model_validate_json(value: bytes) -> SimpleNamespace:
            payload = json.loads(value.decode("utf-8"))
            return SimpleNamespace(
                task_id=payload["task_id"],
                audio=SimpleNamespace(source_sha256=payload["audio"]["source_sha256"]),
                provider=payload["provider"],
                model=payload["model"],
            )

    async def completed_task(**kwargs: Any) -> SimpleNamespace:
        task_id = str(kwargs["task_id"])
        artifact = dict(template)
        artifact["task_id"] = task_id
        artifact_path = runtime_data_dir / "outputs" / "media_transcripts" / f"{task_id}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            status="completed",
            artifact_id=f"artifact_media_transcription_{task_id.rsplit('_', maxsplit=1)[-1]}",
            failure_reason=None,
        )

    common: dict[str, Any] = {
        "fixture": fixture,
        "suite_root": root,
        "project_id": "mp_0000000000000001",
        "runtime": SimpleNamespace(provider="qwen_audio", model="qwen-audio-3.1-asr-flash"),
        "route_audit": object(),
        "ffmpeg_path": Path("fixture-ffmpeg.exe"),
        "ffprobe_path": Path("fixture-ffprobe.exe"),
        "artifacts_dir": artifacts_dir,
        "runtime_data_dir": runtime_data_dir,
        "request_model": lambda **_: object(),
        "artifact_model": ArtifactModel,
        "import_source": lambda **_: source,
        "probe_source": lambda **_: None,
        "extract_audio": lambda **_: audio,
        "create_run": lambda **_: None,
    }
    completed = asyncio.run(_execute_fixture(**common, run_task=completed_task))
    if completed["status"] != "completed" or not (artifacts_dir.parent / str(completed["artifact_file"])).is_file():
        raise AssertionError("quality runner did not copy a verified completed fixture Artifact")

    async def rejected_task(**_: Any) -> SimpleNamespace:
        return SimpleNamespace(status="failed", artifact_id=None, failure_reason="provider_rejected")

    rejected = asyncio.run(_execute_fixture(**common, run_task=rejected_task))
    if rejected != _failed_case(fixture.fixture_id, "failed", "provider_rejected", 1):
        raise AssertionError("quality runner did not retain the rejected Provider request count")


def _print_failure(exc: Exception) -> None:
    print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": _safe_error(exc)}, ensure_ascii=False))


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
