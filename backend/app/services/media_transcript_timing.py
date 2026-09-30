"""Normalize stable-looking ASR snapshots into monotonic transcript spans.

Some streaming ASR providers emit a growing transcript snapshot for every completed
event.  Those snapshots share their initial timestamp, so treating each snapshot as
an independent sentence makes later EDL selections incorrectly start at time zero.
This module only recognizes the unambiguous strict-prefix form; all other provider
segments remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class _TimestampedText(Protocol):
    text: str
    begin_ms: int
    end_ms: int


@dataclass(frozen=True)
class NormalizedTranscriptSpan:
    """One usable transcript span, optionally derived from a growing snapshot."""

    source_index: int
    text: str
    begin_ms: int
    end_ms: int
    previous_snapshot_end_ms: int | None = None


def normalize_cumulative_transcript_spans(
    segments: Sequence[_TimestampedText],
) -> tuple[NormalizedTranscriptSpan, ...]:
    """Convert only strict cumulative snapshots into their newly stabilized suffixes.

    A real sentence may overlap another one, so overlap alone is never rewritten.  A
    rewrite requires the same begin timestamp, an advancing end timestamp, and exact
    text-prefix growth.  This keeps ordinary diarized or overlapping ASR output intact.
    """

    normalized: list[NormalizedTranscriptSpan] = []
    # A provider may briefly emit an older, shorter snapshot out of order.  Keep the
    # longest advancing snapshot for each source start instead of trusting adjacency.
    snapshots_by_begin: dict[int, _TimestampedText] = {}
    for index, segment in enumerate(segments):
        text = segment.text.strip()
        if not text or segment.end_ms < segment.begin_ms:
            continue
        previous = snapshots_by_begin.get(segment.begin_ms)
        if (
            previous is not None
            and segment.end_ms > previous.end_ms
            and text.startswith(previous.text.strip())
        ):
            suffix = text[len(previous.text.strip()) :].strip()
            if suffix:
                normalized.append(
                    NormalizedTranscriptSpan(
                        source_index=index,
                        text=suffix,
                        begin_ms=previous.end_ms,
                        end_ms=segment.end_ms,
                        previous_snapshot_end_ms=previous.end_ms,
                    )
                )
            snapshots_by_begin[segment.begin_ms] = segment
            continue
        if previous is not None and segment.end_ms >= previous.end_ms:
            if text.startswith(previous.text.strip()):
                # A duplicated final snapshot adds no usable time range.  Retain the
                # longer text as the future prefix anchor, but never create a zero-
                # duration subtitle/EDL segment.
                if len(text) > len(previous.text.strip()):
                    snapshots_by_begin[segment.begin_ms] = segment
                continue
            if len(text) < len(previous.text.strip()):
                # This is a stale shorter snapshot from an already advancing stream.
                # It cannot describe a new sentence beginning at the same old time.
                continue
        normalized.append(
            NormalizedTranscriptSpan(
                source_index=index,
                text=text,
                begin_ms=segment.begin_ms,
                end_ms=segment.end_ms,
            )
        )
        snapshots_by_begin[segment.begin_ms] = segment
    return tuple(normalized)
