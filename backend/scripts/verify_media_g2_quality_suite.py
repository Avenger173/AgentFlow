"""Validate the frozen MM-2 image-quality suite before any Provider calls.

The validator is intentionally offline. It verifies the source-level split, fixture
provenance, task coverage, and review protocol required by G2; it does not score a
model result or approve the G2 gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_SUITE_TYPE = "agentflow-mm2-image-quality-suite-v1"
_SPLITS = {"development", "holdout"}
_CATEGORIES = {"background_replace", "object_removal", "text_edit"}
_REVIEW_DIMENSIONS = {
    "instruction_adherence",
    "target_protection",
    "edge_naturalness",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--suite", type=Path, help="Path to a frozen suite.json file.")
    source.add_argument("--self-test", action="store_true", help="Run the offline contract self-test.")
    parser.add_argument(
        "--verify-files",
        action="store_true",
        help="Also require every fixture file and SHA-256 declared by suite.json.",
    )
    args = parser.parse_args()

    try:
        if args.self_test:
            report = _run_self_test()
        else:
            assert args.suite is not None
            report = _verify_suite(args.suite.resolve(), verify_files=args.verify_files)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc

    print(json.dumps(report, ensure_ascii=False))


def _verify_suite(suite_path: Path, *, verify_files: bool) -> dict[str, object]:
    payload = _read_json(suite_path)
    if payload.get("suite_type") != _SUITE_TYPE:
        raise ValueError(f"suite_type must be {_SUITE_TYPE}")

    fixtures = _index_fixtures(payload.get("fixtures"), suite_path.parent, verify_files)
    fixture_splits = Counter(record["split"] for record in fixtures.values())
    if fixture_splits != Counter({"development": 8, "holdout": 4}):
        raise ValueError("fixtures must contain exactly 8 development and 4 holdout sources")

    task_summary = _verify_tasks(payload.get("tasks"), fixtures)
    _verify_review_protocol(payload.get("review_protocol"))
    return {
        "ok": True,
        "suite_type": _SUITE_TYPE,
        "suite_path": str(suite_path),
        "suite_sha256": _canonical_sha256(payload),
        "fixture_count": len(fixtures),
        "fixture_split_counts": dict(sorted(fixture_splits.items())),
        **task_summary,
        "quality_claim": "none; this only validates G2 fixture and review prerequisites",
    }


def _index_fixtures(value: object, root: Path, verify_files: bool) -> dict[str, dict[str, str]]:
    if not isinstance(value, list) or len(value) != 12:
        raise ValueError("fixtures must contain exactly 12 source records")

    fixtures: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"fixture {index} must be an object")
        fixture_id = _required_string(raw, "fixture_id", f"fixture {index}")
        if fixture_id in fixtures:
            raise ValueError(f"duplicate fixture_id: {fixture_id}")
        split = _required_string(raw, "split", fixture_id)
        if split not in _SPLITS:
            raise ValueError(f"fixture {fixture_id} has unsupported split")
        relative_file = _required_string(raw, "file", fixture_id)
        _validate_relative_file(relative_file, fixture_id)
        sha256 = _required_string(raw, "sha256", fixture_id).lower()
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError(f"fixture {fixture_id} has an invalid SHA-256")
        source_page = _required_string(raw, "source_page", fixture_id)
        license_name = _required_string(raw, "license", fixture_id)
        license_url = _required_string(raw, "license_url", fixture_id)
        if not source_page.startswith("https://") or not license_url.startswith("https://"):
            raise ValueError(f"fixture {fixture_id} must provide HTTPS provenance URLs")
        if raw.get("rights_reviewed") is not True:
            raise ValueError(f"fixture {fixture_id} must have rights_reviewed=true")
        if verify_files:
            path = root / relative_file
            if not path.is_file():
                raise ValueError(f"fixture file is missing: {fixture_id}")
            if _sha256_file(path) != sha256:
                raise ValueError(f"fixture file hash mismatch: {fixture_id}")
        fixtures[fixture_id] = {
            "split": split,
            "source_page": source_page,
            "license": license_name,
            "license_url": license_url,
        }
    return fixtures


def _verify_tasks(value: object, fixtures: dict[str, dict[str, str]]) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != 36:
        raise ValueError("tasks must contain exactly 36 records")

    task_ids: set[str] = set()
    tasks_by_fixture: dict[str, list[dict[str, str]]] = defaultdict(list)
    category_counts: Counter[str] = Counter()
    category_split_counts: Counter[tuple[str, str]] = Counter()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"task {index} must be an object")
        task_id = _required_string(raw, "task_id", f"task {index}")
        if task_id in task_ids:
            raise ValueError(f"duplicate task_id: {task_id}")
        task_ids.add(task_id)
        fixture_id = _required_string(raw, "fixture_id", task_id)
        if fixture_id not in fixtures:
            raise ValueError(f"task {task_id} references an unknown fixture")
        category = _required_string(raw, "category", task_id)
        if category not in _CATEGORIES:
            raise ValueError(f"task {task_id} has an unsupported category")
        for field in ("instruction", "target", "preserve", "expected_result"):
            _required_string(raw, field, task_id)
        if raw.get("max_provider_calls") != 1:
            raise ValueError(f"task {task_id} must cap the evaluation path at one Provider call")
        record = {"task_id": task_id, "category": category}
        tasks_by_fixture[fixture_id].append(record)
        category_counts[category] += 1
        category_split_counts[(fixtures[fixture_id]["split"], category)] += 1

    for fixture_id, tasks in tasks_by_fixture.items():
        categories = {task["category"] for task in tasks}
        if len(tasks) != 3 or categories != _CATEGORIES:
            raise ValueError(f"fixture {fixture_id} must have one task in each required category")
    if set(tasks_by_fixture) != set(fixtures):
        raise ValueError("every fixture must be represented by exactly three tasks")
    if category_counts != Counter({category: 12 for category in _CATEGORIES}):
        raise ValueError("each task category must contain exactly 12 tasks")
    for category in _CATEGORIES:
        if category_split_counts[("development", category)] != 8:
            raise ValueError(f"development split must contain 8 {category} tasks")
        if category_split_counts[("holdout", category)] != 4:
            raise ValueError(f"holdout split must contain 4 {category} tasks")
    return {
        "task_count": len(task_ids),
        "task_category_counts": dict(sorted(category_counts.items())),
        "task_split_category_counts": {
            f"{split}:{category}": category_split_counts[(split, category)]
            for split in sorted(_SPLITS)
            for category in sorted(_CATEGORIES)
        },
    }


def _verify_review_protocol(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("review_protocol must be an object")
    if value.get("reviewer_count") != 2:
        raise ValueError("review_protocol must require exactly two reviewers")
    if value.get("blind") is not True or value.get("non_implementer_required") is not True:
        raise ValueError("review_protocol must require blind review with a non-implementer")
    if set(value.get("dimensions") or []) != _REVIEW_DIMENSIONS:
        raise ValueError("review_protocol must include the three fixed quality dimensions")
    if value.get("score_min") != 1 or value.get("score_max") != 5 or value.get("pass_score") != 4:
        raise ValueError("review_protocol must use the fixed 1-5 scale and pass score 4")
    if value.get("disagreement_recheck_gap") != 2:
        raise ValueError("review_protocol must require recheck for a score gap of two")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("suite.json is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("suite.json must be a JSON object")
    return value


def _required_string(record: dict[str, object], field: str, location: str) -> str:
    value = str(record.get(field) or "").strip()
    if not value:
        raise ValueError(f"{location} is missing {field}")
    return value


def _validate_relative_file(value: str, fixture_id: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"fixture {fixture_id} file must stay within the suite directory")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_g2_suite_") as temporary:
        root = Path(temporary)
        fixtures_dir = root / "fixtures"
        fixtures_dir.mkdir()
        fixtures: list[dict[str, object]] = []
        tasks: list[dict[str, object]] = []
        for number in range(1, 13):
            fixture_id = f"G2-IMAGE-{number:02d}"
            relative_file = f"fixtures/{fixture_id.lower()}.bin"
            content = f"fixture-{number}".encode("ascii")
            (root / relative_file).write_bytes(content)
            fixtures.append(
                {
                    "fixture_id": fixture_id,
                    "split": "development" if number <= 8 else "holdout",
                    "file": relative_file,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "source_page": f"https://example.invalid/{fixture_id}",
                    "license": "test-license",
                    "license_url": "https://example.invalid/license",
                    "rights_reviewed": True,
                }
            )
            for category in sorted(_CATEGORIES):
                tasks.append(
                    {
                        "task_id": f"{fixture_id}-{category}",
                        "fixture_id": fixture_id,
                        "category": category,
                        "instruction": "synthetic contract fixture",
                        "target": "synthetic target",
                        "preserve": "synthetic preservation requirement",
                        "expected_result": "synthetic expected result",
                        "max_provider_calls": 1,
                    }
                )
        payload = {
            "suite_type": _SUITE_TYPE,
            "fixtures": fixtures,
            "tasks": tasks,
            "review_protocol": {
                "reviewer_count": 2,
                "blind": True,
                "non_implementer_required": True,
                "dimensions": sorted(_REVIEW_DIMENSIONS),
                "score_min": 1,
                "score_max": 5,
                "pass_score": 4,
                "disagreement_recheck_gap": 2,
            },
        }
        suite_path = root / "suite.json"
        suite_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        report = _verify_suite(suite_path, verify_files=True)
        invalid_payload = dict(payload)
        invalid_payload["fixtures"] = payload["fixtures"][:-1]
        invalid_path = root / "invalid-suite.json"
        invalid_path.write_text(json.dumps(invalid_payload, indent=2), encoding="utf-8")
        try:
            _verify_suite(invalid_path, verify_files=False)
        except ValueError as exc:
            if "exactly 12 source records" not in str(exc):
                raise
        else:
            raise AssertionError("G2 suite validator accepted a missing source fixture")
    report["self_test"] = True
    report["negative_contract_check"] = "missing_fixture_rejected"
    return report


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
