"""Verify integrity and human-review completion for an MM-2 G2 review packet."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image


_PACKET_TYPE = "agentflow-mm2-g2-independent-review-v1"
_COLUMNS = {
    "case_id",
    "card_file",
    "reviewer_id",
    "instruction_adherence",
    "target_protection",
    "edge_naturalness",
    "decision",
    "note",
}
_DECISIONS = {"accept", "reject", "needs_revision"}
_SCORE_FIELDS = ("instruction_adherence", "target_protection", "edge_naturalness")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
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
    if manifest.get("packet") != _PACKET_TYPE:
        raise ValueError("unsupported G2 review packet type")
    cards = _index_cards(manifest.get("cards"), packet_dir)
    if int(manifest.get("case_count") or 0) != len(cards):
        raise ValueError("packet case_count does not match cards")
    reviewer_a = _read_rows(packet_dir / "reviewer_a.csv", cards)
    reviewer_b = _read_rows(packet_dir / "reviewer_b.csv", cards)
    a_complete, a_ids = _reviewer_completion(reviewer_a)
    b_complete, b_ids = _reviewer_completion(reviewer_b)
    complete = a_complete and b_complete and len(a_ids) == 1 and len(b_ids) == 1 and a_ids != b_ids
    return {
        "ok": True,
        "packet": _PACKET_TYPE,
        "packet_dir": str(packet_dir),
        "integrity_valid": True,
        "case_count": len(cards),
        "review_state": "complete" if complete else "incomplete",
        "reviewer_a_ids": sorted(a_ids),
        "reviewer_b_ids": sorted(b_ids),
        "quality_claim": "none; reviewer independence and G2 gate approval remain external decisions",
    }


def _index_cards(value: object, packet_dir: Path) -> dict[str, dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("review packet has no cards")
    cards: dict[str, dict[str, str]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("review card record is invalid")
        case_id = str(raw.get("case_id") or "").strip()
        card_file = str(raw.get("card_file") or "").strip()
        card_sha256 = str(raw.get("card_sha256") or "").strip().lower()
        if not case_id or case_id in cards or not card_file or len(card_sha256) != 64:
            raise ValueError("review card record is missing or duplicate")
        card_path = (packet_dir / card_file).resolve()
        if card_path.parent != packet_dir / "cards" or not card_path.is_file():
            raise ValueError(f"review card path is invalid: {case_id}")
        if _sha256_file(card_path) != card_sha256:
            raise ValueError(f"review card hash mismatch: {case_id}")
        with Image.open(card_path) as card:
            if card.format != "PNG" or card.mode != "RGB" or card.width < 1024 or card.height < 512:
                raise ValueError(f"review card is not a valid side-by-side PNG: {case_id}")
        cards[case_id] = {"card_file": card_file, "card_sha256": card_sha256}
    return cards


def _read_rows(path: Path, cards: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not _COLUMNS.issubset(reader.fieldnames):
            raise ValueError(f"review CSV has missing columns: {path.name}")
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {field: str(raw.get(field) or "").strip() for field in _COLUMNS}
            case_id = row["case_id"]
            if not case_id or case_id in {entry["case_id"] for entry in rows}:
                raise ValueError(f"review CSV has duplicate or missing case ID: {path.name}")
            if case_id not in cards or row["card_file"] != cards[case_id]["card_file"]:
                raise ValueError(f"review CSV does not match frozen card: {path.name}")
            rows.append(row)
    if len(rows) != len(cards) or {row["case_id"] for row in rows} != set(cards):
        raise ValueError(f"review CSV does not cover every card: {path.name}")
    return rows


def _reviewer_completion(rows: list[dict[str, str]]) -> tuple[bool, set[str]]:
    reviewer_ids: set[str] = set()
    for row in rows:
        if not row["reviewer_id"]:
            return False, reviewer_ids
        reviewer_ids.add(row["reviewer_id"])
        decision = row["decision"].casefold()
        if decision not in _DECISIONS:
            return False, reviewer_ids
        if decision != "accept" and not row["note"]:
            return False, reviewer_ids
        for field in _SCORE_FIELDS:
            try:
                score = int(row[field])
            except ValueError:
                return False, reviewer_ids
            if score < 1 or score > 5:
                return False, reviewer_ids
    return True, reviewer_ids


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("review manifest is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("review manifest must be an object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
