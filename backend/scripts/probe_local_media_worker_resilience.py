"""Measure isolated local-media worker lifecycle behavior for MM-0.

This supervisor deliberately does not import PyTorch, SAM, or BiRefNet. It launches the
existing candidate probes in their dedicated virtual environment, records peak RSS, verifies
cold-versus-warm inference records, checks missing-weight failure, and terminates a worker
before artifact commit. It is selection evidence only, not a production task runner.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
ISOLATED_PYTHON = BACKEND_ROOT / ".mm0_birefnet_matting_probe" / "Scripts" / "python.exe"
FIXTURE_CASE_ID = "MM0-PERSON-01"


@dataclass(frozen=True)
class _WorkerDefinition:
    worker_id: str
    script_name: str
    model_path: Path
    source_dir: Path
    normal_timeout_seconds: float
    cancellation_timeout_seconds: float


_WORKERS = {
    "birefnet_lite_matting": _WorkerDefinition(
        worker_id="birefnet_lite_matting",
        script_name="probe_birefnet_lite_matting.py",
        model_path=PROJECT_ROOT
        / "data"
        / "media_model_cache"
        / "birefnet_lite_matting"
        / "BiRefNet_lite-matting-epoch_110.pth",
        source_dir=PROJECT_ROOT / "data" / "media_model_cache" / "birefnet_v1_source",
        normal_timeout_seconds=180.0,
        cancellation_timeout_seconds=240.0,
    ),
    "sam2_hiera_tiny": _WorkerDefinition(
        worker_id="sam2_hiera_tiny",
        script_name="probe_sam2_hiera_tiny.py",
        model_path=PROJECT_ROOT
        / "data"
        / "media_model_cache"
        / "sam2_hiera_tiny"
        / "sam2.1_hiera_tiny.pt",
        source_dir=PROJECT_ROOT / "data" / "media_model_cache" / "sam2_source",
        normal_timeout_seconds=90.0,
        cancellation_timeout_seconds=150.0,
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True, help="已冻结的 MM-0 公开图片夹具目录")
    parser.add_argument(
        "--worker",
        action="append",
        choices=sorted(_WORKERS),
        help="只验证指定 worker；默认依次验证全部候选",
    )
    parser.add_argument("--execute", action="store_true", help="明确启动隔离本地模型 worker")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to run isolated local-media worker resilience probes.")
        return

    fixture_dir = args.fixture_dir.resolve()
    _validate_fixture_dir(fixture_dir)
    _require_supervisor_dependencies()
    if not ISOLATED_PYTHON.is_file():
        raise SystemExit(f"isolated worker Python is missing: {ISOLATED_PYTHON}")

    selected_ids = args.worker or list(_WORKERS)
    output_dir = PROJECT_ROOT / "data" / "media_evaluations" / (
        "local_media_worker_resilience_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    results = [_run_worker_suite(_WORKERS[worker_id], fixture_dir, output_dir) for worker_id in selected_ids]
    passed = all(bool(result.get("passed")) for result in results)
    manifest = {
        "probe": "local_media_worker_resilience_v1",
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fixture_case_id": FIXTURE_CASE_ID,
        "fixture_dir": fixture_dir.name,
        "isolated_python": str(ISOLATED_PYTHON),
        "results": results,
        "passed": passed,
        "quality_claim": "none; this verifies lifecycle and artifact-commit behavior, not visual quality",
    }
    _write_json(output_dir / "manifest.json", manifest)
    print(json.dumps({"ok": passed, "output_dir": str(output_dir), "worker_count": len(results)}))
    if not passed:
        raise SystemExit(1)


def _run_worker_suite(
    definition: _WorkerDefinition,
    fixture_dir: Path,
    root_dir: Path,
) -> dict[str, object]:
    worker_dir = root_dir / definition.worker_id
    worker_dir.mkdir()
    checks: dict[str, dict[str, object]] = {}
    checks["cold_and_warm"] = _run_cold_and_warm(definition, fixture_dir, worker_dir)
    checks["missing_weight"] = _run_missing_weight(definition, fixture_dir, worker_dir)
    checks["cancel_before_commit"] = _run_cancel_before_commit(definition, fixture_dir, worker_dir)
    return {
        "worker_id": definition.worker_id,
        "passed": all(bool(check.get("passed")) for check in checks.values()),
        "checks": checks,
    }


def _run_cold_and_warm(
    definition: _WorkerDefinition,
    fixture_dir: Path,
    worker_dir: Path,
) -> dict[str, object]:
    output_dir = worker_dir / "cold_and_warm_output"
    command = _worker_command(
        definition,
        fixture_dir,
        "--case-id",
        FIXTURE_CASE_ID,
        "--repeat-count",
        "2",
        "--output-dir",
        str(output_dir),
    )
    process = _start_process(command)
    observed = _observe_process(process, timeout_seconds=definition.normal_timeout_seconds)
    manifest_path = output_dir / "manifest.json"
    passed = observed["exit_code"] == 0 and manifest_path.is_file()
    details: dict[str, object] = {
        "passed": passed,
        "command": _redacted_command(command),
        "output_dir": output_dir.name,
        **observed,
    }
    if not passed:
        return details
    try:
        manifest = _read_json(manifest_path)
        cases = manifest.get("cases")
        if not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], dict):
            raise ValueError("worker manifest does not contain exactly one selected case")
        case = cases[0]
        repeats = case.get("repeat_elapsed_ms")
        load_ms = manifest.get("model_load_ms")
        if (
            not isinstance(load_ms, (int, float))
            or load_ms < 0
            or not isinstance(repeats, list)
            or len(repeats) != 2
            or not all(isinstance(value, (int, float)) and value >= 0 for value in repeats)
        ):
            raise ValueError("worker manifest lacks valid cold/warm timing records")
        _verify_worker_artifacts(output_dir, case)
        details.update(
            {
                "model_load_ms": load_ms,
                "first_inference_ms": repeats[0],
                "warm_inference_ms": repeats[1],
                "manifest_file": manifest_path.name,
            }
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        details["passed"] = False
        details["error"] = _safe_error(exc)
    return details


def _run_missing_weight(
    definition: _WorkerDefinition,
    fixture_dir: Path,
    worker_dir: Path,
) -> dict[str, object]:
    output_dir = worker_dir / "missing_weight_output"
    missing_path = worker_dir / "does_not_exist_model_weight.pth"
    command = _worker_command(
        definition,
        fixture_dir,
        "--case-id",
        FIXTURE_CASE_ID,
        "--output-dir",
        str(output_dir),
        "--model-path",
        str(missing_path),
    )
    observed = _observe_process(_start_process(command), timeout_seconds=30.0)
    passed = observed["exit_code"] not in (0, None) and not output_dir.exists()
    return {
        "passed": passed,
        "command": _redacted_command(command),
        "output_dir_created": output_dir.exists(),
        **observed,
    }


def _run_cancel_before_commit(
    definition: _WorkerDefinition,
    fixture_dir: Path,
    worker_dir: Path,
) -> dict[str, object]:
    output_dir = worker_dir / "cancel_before_commit_output"
    stage_file = worker_dir / "cancel_before_commit_stage.json"
    command = _worker_command(
        definition,
        fixture_dir,
        "--case-id",
        FIXTURE_CASE_ID,
        "--repeat-count",
        "1",
        "--output-dir",
        str(output_dir),
        "--test-ready-file",
        str(stage_file),
        "--test-pause-before-commit-seconds",
        "120",
    )
    process = _start_process(command)
    peak_rss_bytes = 0
    deadline = time.monotonic() + definition.cancellation_timeout_seconds
    observed_stage: dict[str, object] | None = None
    timed_out = False
    while time.monotonic() < deadline:
        peak_rss_bytes = max(peak_rss_bytes, _tree_rss_bytes(process.pid))
        if stage_file.is_file():
            try:
                candidate = _read_json(stage_file)
            except (OSError, ValueError, json.JSONDecodeError):
                candidate = {}
            if candidate.get("stage") == "result_ready_before_commit":
                observed_stage = candidate
                break
        if process.poll() is not None:
            break
        time.sleep(0.05)
    else:
        timed_out = True

    if observed_stage is None and process.poll() is None:
        _terminate_process(process)
    elif observed_stage is not None:
        _terminate_process(process)
    stdout, stderr = _collect_process_output(process)
    exit_code = process.returncode
    committed_files = _committed_artifact_names(output_dir)
    passed = (
        observed_stage is not None
        and not timed_out
        and exit_code not in (0, None)
        and not committed_files
        and not (output_dir / "manifest.json").exists()
    )
    return {
        "passed": passed,
        "command": _redacted_command(command),
        "exit_code": exit_code,
        "elapsed_ms": None,
        "peak_rss_bytes": peak_rss_bytes,
        "stage": observed_stage.get("stage") if observed_stage else "",
        "timed_out_waiting_for_stage": timed_out,
        "output_dir_created": output_dir.exists(),
        "committed_artifacts": committed_files,
        "stdout": _truncate_output(stdout),
        "stderr": _truncate_output(stderr),
    }


def _worker_command(definition: _WorkerDefinition, fixture_dir: Path, *extra: str) -> list[str]:
    script_path = BACKEND_ROOT / "scripts" / definition.script_name
    return [
        str(ISOLATED_PYTHON),
        str(script_path),
        "--fixture-dir",
        str(fixture_dir),
        "--source-dir",
        str(definition.source_dir),
        *extra,
        "--execute",
    ]


def _start_process(command: list[str]) -> subprocess.Popen[str]:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    return subprocess.Popen(
        command,
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _observe_process(process: subprocess.Popen[str], *, timeout_seconds: float) -> dict[str, object]:
    started = time.perf_counter()
    peak_rss_bytes = 0
    timed_out = False
    while process.poll() is None:
        peak_rss_bytes = max(peak_rss_bytes, _tree_rss_bytes(process.pid))
        if time.perf_counter() - started > timeout_seconds:
            timed_out = True
            _terminate_process(process)
            break
        time.sleep(0.05)
    stdout, stderr = _collect_process_output(process)
    peak_rss_bytes = max(peak_rss_bytes, _tree_rss_bytes(process.pid))
    return {
        "exit_code": process.returncode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "peak_rss_bytes": peak_rss_bytes,
        "timed_out": timed_out,
        "stdout": _truncate_output(stdout),
        "stderr": _truncate_output(stderr),
    }


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _collect_process_output(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - guarded by _terminate_process.
        process.kill()
        return process.communicate(timeout=10)


def _tree_rss_bytes(pid: int) -> int:
    psutil = _load_psutil()
    try:
        process = psutil.Process(pid)
        processes = [process, *process.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for item in processes:
        try:
            total += int(item.memory_info().rss)
        except psutil.Error:
            continue
    return total


def _verify_worker_artifacts(output_dir: Path, case: dict[str, object]) -> None:
    artifact_keys = ("alpha_file", "cutout_file") if "alpha_file" in case else ("mask_file", "selected_file")
    for key in artifact_keys:
        name = str(case.get(key) or "")
        artifact_path = (output_dir / name).resolve()
        if not name or artifact_path.parent != output_dir or not artifact_path.is_file():
            raise ValueError(f"worker artifact is missing or unsafe: {key}")


def _committed_artifact_names(output_dir: Path) -> list[str]:
    if not output_dir.is_dir():
        return []
    return sorted(path.name for path in output_dir.iterdir() if path.is_file())


def _validate_fixture_dir(fixture_dir: Path) -> None:
    manifest_path = fixture_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("fixture-dir must contain a frozen fixture manifest")
    manifest = _read_json(manifest_path)
    if manifest.get("fixture_set") != "agentflow-mm0-public-image-fixtures-v2":
        raise SystemExit("fixture-dir is not the frozen MM-0 public fixture set v2")


def _require_supervisor_dependencies() -> None:
    try:
        _load_psutil()
    except ImportError as exc:
        raise SystemExit("Install psutil in the main backend environment to run worker resilience probes.") from exc


def _load_psutil() -> Any:
    import psutil

    return psutil


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _redacted_command(command: list[str]) -> list[str]:
    return ["<path>" if item.lower().endswith((".pth", ".pt", ".py", ".exe")) else item for item in command]


def _truncate_output(value: str, maximum: int = 1000) -> str:
    return " ".join(value.split())[:maximum]


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
