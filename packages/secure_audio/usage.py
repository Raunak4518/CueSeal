import csv
from dataclasses import dataclass
from io import StringIO
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from scipy.optimize import linear_sum_assignment

from .crypto import EvidenceError, digest, verify_evidence
from .fingerprint import Segment


USAGE_REPORT_TYPE = "music_usage_audit"
MAX_CUE_SHEET_BYTES = 256 * 1024
MAX_TIME_MS = 2**53 - 1
CUE_COLUMNS = ("cue_id", "recording_id", "start_ms", "end_ms")
REVIEW_COLUMNS = ("status", "reason", "cue_id", "recording_id", "declared_start_ms", "declared_end_ms",
                  "detected_start_ms", "detected_end_ms")
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
TimeMs = Annotated[int, Field(ge=0, le=MAX_TIME_MS)]


class DeclaredCue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cue_id: Identifier
    recording_id: Identifier
    start_ms: TimeMs
    end_ms: TimeMs

    @model_validator(mode="after")
    def check_interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("Cue end must be after its start")
        return self


class UsageAuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    programme_id: Identifier
    origin_ms: Annotated[int, Field(ge=0, le=MAX_TIME_MS - 60_000)]
    tolerance_ms: Annotated[int, Field(ge=0, le=60_000)]
    cue_sheet_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    cues: tuple[DeclaredCue, ...]

    @model_validator(mode="after")
    def check_cue_ids(self):
        if len({cue.cue_id for cue in self.cues}) != len(self.cues):
            raise ValueError("Duplicate cue ID")
        return self

    @classmethod
    def from_csv(cls, data: bytes, programme_id: str, origin_ms: int, tolerance_ms: int):
        if len(data) > MAX_CUE_SHEET_BYTES:
            raise ValueError("Cue sheet exceeds the 256 KiB limit")
        reader = csv.reader(StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
        try:
            if tuple(next(reader, ())) != CUE_COLUMNS:
                raise ValueError(f"Cue sheet header must be {','.join(CUE_COLUMNS)}")
            cues = []
            for row in reader:
                if not row:
                    continue
                if len(row) != len(CUE_COLUMNS):
                    raise ValueError(f"Invalid cue sheet row ending at line {reader.line_num}")
                if any(not value.isascii() or not value.isdecimal() for value in row[2:]):
                    raise ValueError(f"Cue times must be whole milliseconds on line {reader.line_num}")
                cues.append(DeclaredCue.model_validate(dict(zip(CUE_COLUMNS, row, strict=True))))
        except csv.Error as exc:
            raise ValueError(f"Malformed cue sheet near line {reader.line_num}") from exc
        return cls(programme_id=programme_id, origin_ms=origin_ms, tolerance_ms=tolerance_ms,
                   cue_sheet_sha256=digest(data), cues=tuple(cues))


@dataclass(frozen=True)
class DetectedCue:
    recording_id: str
    start_ms: int
    end_ms: int
    evidence: dict


def _detections(segments: tuple[Segment, ...], origin: int, end: int) -> list[DetectedCue]:
    detected = []
    for segment in segments:
        evidence = segment.payload()
        start_ms = origin + evidence["query_start_ms"]
        end_ms = min(end, origin + evidence["query_end_ms"])
        if start_ms < end_ms:
            detected.append(DetectedCue(segment.reference_id, start_ms, end_ms, evidence))
    return detected


def _overlap(first: DeclaredCue | DetectedCue, second: DetectedCue) -> int:
    if first.recording_id != second.recording_id:
        return 0
    return max(0, min(first.end_ms, second.end_ms) - max(first.start_ms, second.start_ms))


def _assign(cues: tuple[DeclaredCue, ...], detections: list[DetectedCue]) -> dict[int, int]:
    if not cues or not detections:
        return {}
    overlap = [[_overlap(cue, detection) for detection in detections] for cue in cues]
    rows, columns = linear_sum_assignment(overlap, maximize=True)
    return {row: column for row, column in zip(rows.tolist(), columns.tolist(), strict=True) if overlap[row][column]}


def _finding(status: str, reason: str, cue: DeclaredCue | None = None, detection: DetectedCue | None = None) -> dict:
    return {
        "status": status, "reason": reason, "cue_id": cue.cue_id if cue else None,
        "recording_id": cue.recording_id if cue else detection.recording_id,
        "declared_start_ms": cue.start_ms if cue else None, "declared_end_ms": cue.end_ms if cue else None,
        "detected_start_ms": detection.start_ms if detection else None,
        "detected_end_ms": detection.end_ms if detection else None,
        "match_evidence": detection.evidence if detection else None,
    }


def _declared_finding(cue: DeclaredCue, detection: DetectedCue | None, uncertain: DetectedCue | None,
                      in_catalog: bool, overlaps_assigned: bool, origin: int, end: int, tolerance: int) -> dict:
    if cue.end_ms <= origin or cue.start_ms >= end:
        return _finding("needs_review", "outside_excerpt", cue)
    if not in_catalog:
        return _finding("needs_review", "recording_not_in_catalog", cue)
    if cue.start_ms < origin or cue.end_ms > end:
        return _finding("needs_review", "partial_excerpt_coverage", cue, detection or uncertain)
    if detection is not None:
        error = max(abs(cue.start_ms - detection.start_ms), abs(cue.end_ms - detection.end_ms))
        if error > tolerance:
            return _finding("timing_mismatch", "boundary_outside_tolerance", cue, detection)
        return _finding("confirmed", "within_timing_tolerance", cue, detection)
    if overlaps_assigned:
        return _finding("needs_review", "overlapping_declaration", cue)
    if uncertain is not None:
        return _finding("needs_review", "ambiguous_match", cue, uncertain)
    return _finding("declared_without_evidence", "no_reliable_match", cue)


def reconcile_usage(audit: UsageAuditInput, references: set[str], duration_ms: int,
                    confirmed: tuple[Segment, ...], uncertain: tuple[Segment, ...]) -> list[dict]:
    end = audit.origin_ms + duration_ms
    detections = _detections(confirmed, audit.origin_ms, end)
    possible = _detections(uncertain, audit.origin_ms, end)
    paired = _assign(audit.cues, detections)
    assigned = set(paired.values())
    findings = []
    for i, cue in enumerate(audit.cues):
        detection = detections[paired[i]] if i in paired else None
        alternatives = [candidate for candidate in possible if _overlap(cue, candidate)]
        uncertain_match = max(alternatives, key=lambda candidate: _overlap(cue, candidate), default=None)
        overlaps_assigned = detection is None and any(_overlap(cue, detections[j]) for j in assigned)
        findings.append(_declared_finding(cue, detection, uncertain_match, cue.recording_id in references,
                                          overlaps_assigned, audit.origin_ms, end, audit.tolerance_ms))
    findings.extend(_finding("detected_unlisted", "no_matching_declaration", detection=detection)
                    for i, detection in enumerate(detections) if i not in assigned)
    findings.extend(_finding("needs_review", "ambiguous_match", detection=candidate)
                    for candidate in possible
                    if not any(_overlap(cue, candidate) for cue in audit.cues)
                    and not any(_overlap(detection, candidate) for detection in detections))
    return findings


def review_csv(findings: list[dict]) -> str:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=REVIEW_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(findings)
    return output.getvalue()


def verify_usage_report(envelope: dict, public_key: bytes, media_sha256: str, cue_sheet_sha256: str) -> dict:
    payload = verify_evidence(envelope, public_key, USAGE_REPORT_TYPE, media_sha256)
    if payload.get("cue_sheet_sha256") != cue_sheet_sha256:
        raise EvidenceError("Cue sheet digest does not match the signed record")
    return payload
