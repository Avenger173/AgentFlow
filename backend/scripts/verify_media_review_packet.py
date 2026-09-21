"""Verify the integrity and completion state of an MM-0 independent review packet.

This is deliberately a verifier, not a scorer: it never inspects image quality and it
cannot establish that a reviewer is independent. It only freezes what was reviewed and
reports whether the human decision table is complete and unanimous enough for a later
gate decision.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image


_PACKET_TYPES = {
    "qwen_image_independent_review_v2",
    "agentflow_local_mask_independent_review_v2",
}
_DECISIONS = {"accept", "reject", "needs_revision"}
_REQUIRED_COLUMNS = {"case_id", "card_file", "reviewer_id", "decision", "note"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Treat blank human decisions as a non-zero incomplete result.",
    )
    args = parser.parse_args()
    try:
        report = _verify_packet(args.packet_dir.resolve())
    except (OSError, ValueError, csv.Error) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))
    if not report["integrity_valid"] or (args.require_complete and report["review_state"] != "complete"):
        raise SystemExit(1)


def _verify_packet(packet_dir: Path) -> dict[str, object]:
    manifest = _read_json(packet_dir / "manifest.json")
    packet_type = str(manifest.get("packet") or "")
    if packet_type not in _PACKET_TYPES:
        raise ValueError("unsupported or legacy review packet; regenerate a v2 packet")
    declared_cards = manifest.get("cards")
    if not isinstance(declared_cards, list) or not declared_cards:
        raise ValueError("review packet has no frozen card records")
    cards = _index_cards(declared_cards)
    if int(manifest.get("case_count") or 0) != len(cards):
        raise ValueError("review packet case_count does not match frozen card records")
    for case_id, record in cards.items():
        card_path = _safe_child(packet_dir, record["card_file"])
        if _sha256(card_path) != record["card_sha256"]:
            raise ValueError(f"review card hash mismatch: {case_id}")
        with Image.open(card_path) as card:
            if card.format != "PNG" or card.mode != "RGB":
                raise ValueError(f"review card cannot be read as RGB PNG: {case_id}")

    rows = _read_rows(packet_dir / "review.csv")
    row_ids = {row["case_id"] for row in rows}
    if len(rows) != len(cards) or row_ids != set(cards):
        raise ValueError("review.csv case IDs do not exactly match frozen card records")
    reviewer_ids: set[str] = set()
    decisions: dict[str, str] = {}
    incomplete_ids: list[str] = []
    for row in rows:
        case_id = row["case_id"]
        reviewer_id = row["reviewer_id"].strip()
        decision = row["decision"].strip().casefold()
        note = row["note"].strip()
        if not reviewer_id or not decision:
            incomplete_ids.append(case_id)
            continue
        if decision not in _DECISIONS:
            raise ValueError(f"review.csv has an unsupported decision for {case_id}")
        if decision != "accept" and not note:
            raise ValueError(f"review.csv requires a note for {decision}: {case_id}")
        reviewer_ids.add(reviewer_id)
        decisions[case_id] = decision
    decision_counts = {decision: sum(1 for value in decisions.values() if value == decision) for decision in sorted(_DECISIONS)}
    complete = not incomplete_ids
    return {
        "ok": True,
        "packet": packet_type,
        "packet_dir": str(packet_dir),
        "integrity_valid": True,
        "review_state": "complete" if complete else "incomplete",
        "case_count": len(cards),
        "incomplete_case_ids": sorted(incomplete_ids),
        "reviewer_ids": sorted(reviewer_ids),
        "decision_counts": decision_counts,
        "quality_gate_candidate": complete and decision_counts["accept"] == len(cards),
        "quality_claim": (
            "none; this verifies frozen evidence and CSV completion only. "
            "Reviewer independence and final G0 approval remain external decisions."
        ),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("review manifest is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("review manifest must be a JSON object")
    return value


def _index_cards(value: list[object]) -> dict[str, dict[str, str]]:
    cards: dict[str, dict[str, str]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("review manifest contains an invalid card record")
        case_id = str(raw.get("case_id") or "").strip()
        card_file = str(raw.get("card_file") or "").strip()
        card_hash = str(raw.get("card_sha256") or "").strip().lower()
        if not case_id or case_id in cards or not card_file or len(card_hash) != 64:
            raise ValueError("review manifest has a missing or duplicate card record")
        cards[case_id] = {"card_file": card_file, "card_sha256": card_hash}
    return cards


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise ValueError("review.csv is missing required columns")
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {column: str(raw.get(column) or "") for column in _REQUIRED_COLUMNS}
            if not row["case_id"].strip() or any(row["case_id"] == existing["case_id"] for existing in rows):
                raise ValueError("review.csv has a missing or duplicate case_id")
            rows.append(row)
    return rows


def _safe_child(parent: Path, name: str) -> Path:
    path = (parent / name).resolve()
    if path.parent != parent / "cards":
        raise ValueError("review card path escapes its cards directory")
    if not path.is_file():
        raise ValueError("review card is missing")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
