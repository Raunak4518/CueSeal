from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.ndimage import maximum_filter

from .decoder import SAMPLE_RATE
from .domain import Recognition


FINGERPRINT_VERSION = "landmark-v1"
FRAME_SIZE = 1024
HOP = 128
FRAME_MS = HOP * 1000 / SAMPLE_RATE
MAX_POSTINGS = 100
PAIR_CODE_BITS = 22


@dataclass(frozen=True, order=True)
class Landmark:
    code: int
    frame: int


@dataclass(frozen=True)
class Posting:
    reference_id: str
    frame: int


class PostingsIndex(Protocol):
    def postings(self, codes: set[int]) -> dict[int, list[Posting]]: ...


def fingerprint(samples: np.ndarray) -> list[Landmark]:
    if samples.size < FRAME_SIZE or np.max(np.abs(samples)) < 1e-5:
        return []
    frames = sliding_window_view(samples, FRAME_SIZE)[::HOP]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(FRAME_SIZE), axis=1))
    spectrum[:, :6] = 0
    spectrum[:, 420:] = 0
    local_max = maximum_filter(spectrum, size=(7, 9), mode="constant")
    floor = np.maximum(spectrum.max(axis=1, keepdims=True) * 0.18, spectrum.max() * 0.025)
    times, bins = np.nonzero((spectrum == local_max) & (spectrum > floor))
    if not times.size:
        return []
    starts = np.searchsorted(times, times + 4)
    stops = np.searchsorted(times, times + 47)
    counts = np.minimum(stops - starts, 16)
    anchors = np.repeat(np.arange(times.size), counts)
    targets = np.repeat(starts - np.cumsum(counts) + counts, counts) + np.arange(anchors.size)
    deltas = (times[targets] - times[anchors] + 1) // 2
    codes = ((bins[anchors] // 2) << 14) | ((bins[targets] // 2) << 6) | deltas
    packed = np.unique((times[anchors] << PAIR_CODE_BITS) | codes)
    code_mask = (1 << PAIR_CODE_BITS) - 1
    return [Landmark(value & code_mask, value >> PAIR_CODE_BITS) for value in packed.tolist()]


FEATURE_NAMES = ("aligned_count", "aligned_fraction", "concentration", "coverage", "duration", "margin")


@dataclass(frozen=True)
class Candidate:
    reference_id: str
    offset_frame: int
    start_frame: int
    end_frame: int
    aligned_count: int
    aligned_fraction: float
    concentration: float
    coverage: float
    margin: float = 0

    @property
    def duration(self) -> float:
        return (self.end_frame - self.start_frame) * FRAME_MS / 1000

    @property
    def features(self) -> tuple[float, ...]:
        return (self.aligned_count, self.aligned_fraction, self.concentration, self.coverage, self.duration, self.margin)

    @property
    def has_minimum_evidence(self) -> bool:
        return self.aligned_count >= 6 and self.duration >= 0.75


@dataclass(frozen=True)
class Segment:
    reference_id: str
    offset_frame: int
    start_frame: int
    end_frame: int
    score: float
    aligned_count: int

    def payload(self) -> dict:
        return {
            "reference_id": self.reference_id,
            "query_start_ms": round(self.start_frame * FRAME_MS),
            "query_end_ms": round(self.end_frame * FRAME_MS),
            "reference_start_ms": max(0, round((self.start_frame + self.offset_frame) * FRAME_MS)),
            "reference_end_ms": max(0, round((self.end_frame + self.offset_frame) * FRAME_MS)),
            "aligned_hashes": self.aligned_count,
            "decision_score": round(self.score, 6),
        }


class ScoreStrategy(Protocol):
    version: str
    threshold: float
    review_threshold: float
    tie_margin: float

    def score(self, candidate: Candidate) -> float: ...


@dataclass(frozen=True)
class MatchDecision:
    status: Recognition
    segments: tuple[Segment, ...]
    competing_references: tuple[str, ...] = ()


def retrieve(samples: np.ndarray, index: PostingsIndex, top_k: int = 5) -> list[list[Candidate]]:
    return retrieve_landmarks(fingerprint(samples), samples.size // HOP, index, top_k)


def retrieve_landmarks(landmarks: list[Landmark], duration_frames: int, index: PostingsIndex,
                       top_k: int = 5) -> list[list[Candidate]]:
    if not landmarks:
        return []
    postings = index.postings({item.code for item in landmarks})
    window_size = round(4000 / FRAME_MS)
    hop_size = round(2000 / FRAME_MS)
    last_start = max(0, duration_frames - window_size)
    starts = list(range(0, last_start + 1, hop_size))
    if starts[-1] != last_start:
        starts.append(last_start)
    if not any(postings.values()):
        return [[] for _ in starts]
    times = np.array([item.frame for item in landmarks])
    offsets = _OffsetIndex(postings, int(times[0]), int(times[-1]))
    ranges = np.searchsorted(times, np.asarray(starts)[:, None] + [0, window_size])
    return [offsets.rank(landmarks[left:right], top_k) for left, right in ranges]


class _OffsetIndex:
    def __init__(self, postings: dict[int, list[Posting]], first_query_frame: int, last_query_frame: int):
        reference_indexes: dict[str, int] = {}
        reference_frames: list[int] = []
        reference_columns: list[int] = []
        groups: dict[int, slice] = {}
        for code, matches in postings.items():
            start = len(reference_frames)
            for posting in dict.fromkeys(matches):
                reference_frames.append(posting.frame)
                reference_columns.append(reference_indexes.setdefault(posting.reference_id, len(reference_indexes)))
            if start != len(reference_frames):
                groups[code] = slice(start, len(reference_frames))

        self.references = tuple(reference_indexes)
        self.minimum_offset = min(reference_frames) - last_query_frame
        self.stride = max(reference_frames) - first_query_frame - self.minimum_offset + 2
        keys = (np.asarray(reference_columns, dtype=np.int64) * self.stride
                + np.asarray(reference_frames, dtype=np.int64) - self.minimum_offset)
        self.keys_by_code = {code: keys[group] for code, group in groups.items()}

    def rank(self, landmarks: list[Landmark], top_k: int) -> list[Candidate]:
        matched = [(item.frame, self.keys_by_code[item.code])
                   for item in dict.fromkeys(landmarks) if item.code in self.keys_by_code]
        if not matched:
            return []
        frames, posting_keys = zip(*matched, strict=True)
        hit_indexes = np.repeat(np.arange(len(frames)), [len(keys) for keys in posting_keys])
        offsets = np.concatenate(posting_keys) - np.asarray(frames)[hit_indexes]
        references = offsets // self.stride
        winning = self._winning_offsets(offsets)
        aligned_hits = np.abs(offsets - winning[references]) <= 1
        totals = np.bincount(references, minlength=len(self.references))

        aligned: dict[int, set[int]] = defaultdict(set)
        for reference, hit in zip(references[aligned_hits].tolist(), hit_indexes[aligned_hits].tolist(), strict=True):
            aligned[reference].add(hit)
        candidates = []
        bucket_size = max(1, round(250 / FRAME_MS))
        for reference, hits in aligned.items():
            if len(hits) < 3:
                continue
            times = {frames[hit] for hit in hits}
            start, stop = min(times), max(times) + 1
            occupied = {frame // bucket_size for frame in times}
            coverage = len(occupied) / ((stop - 1) // bucket_size - start // bucket_size + 1)
            offset = int(winning[reference] % self.stride) + self.minimum_offset
            candidates.append(Candidate(self.references[reference], offset, start, stop, len(hits), len(hits) / len(landmarks),
                                        len(hits) / int(totals[reference]), coverage))
        candidates.sort(key=lambda item: (-item.aligned_count, -item.coverage, item.reference_id))
        best = candidates[0].aligned_count if candidates else 0
        runner_up = candidates[1].aligned_count if len(candidates) > 1 else 0
        return [replace(item, margin=(item.aligned_count - (runner_up if i == 0 else best)) / best)
                for i, item in enumerate(candidates[:top_k])]

    def _winning_offsets(self, offsets: np.ndarray) -> np.ndarray:
        keys, counts = np.unique(offsets, return_counts=True)
        support = counts.copy()
        adjacent = np.diff(keys) == 1
        support[1:] += counts[:-1] * adjacent
        support[:-1] += counts[1:] * adjacent
        eligible = np.flatnonzero(support >= 3)
        references = keys[eligible] // self.stride
        order = np.lexsort((keys[eligible], -counts[eligible], -support[eligible], references))
        winners, first = np.unique(references[order], return_index=True)
        result = np.full(len(self.references), -2, dtype=np.int64)
        result[winners] = keys[eligible[order[first]]]
        return result


def decide(windows: list[list[Candidate]], strategy: ScoreStrategy) -> MatchDecision:
    segments: list[Segment] = []
    competing: set[str] = set()
    for candidates in windows:
        scores = sorted(((strategy.score(item), item) for item in candidates if item.has_minimum_evidence),
                        key=lambda pair: pair[0], reverse=True)
        if not scores or scores[0][0] < strategy.review_threshold:
            continue
        score, candidate = scores[0]
        runners = {other.reference_id for other_score, other in scores[1:]
                   if other_score >= strategy.review_threshold and score - other_score <= strategy.tie_margin}
        if runners or score < strategy.threshold:
            competing.update(runners | {candidate.reference_id})
            continue
        segments.append(Segment(candidate.reference_id, candidate.offset_frame, candidate.start_frame,
                                candidate.end_frame, score, candidate.aligned_count))
    if competing:
        return MatchDecision(Recognition.AMBIGUOUS, _merge_segments(segments), tuple(sorted(competing)))
    if not segments:
        return MatchDecision(Recognition.NO_MATCH, ())
    return MatchDecision(Recognition.MATCH, _merge_segments(segments))


def timed_matches(windows: list[list[Candidate]], strategy: ScoreStrategy) -> tuple[tuple[Segment, ...], tuple[Segment, ...]]:
    confirmed, uncertain = [], []
    for window in windows:
        scores = [(strategy.score(candidate), candidate) for candidate in window if candidate.has_minimum_evidence]
        plausible = [(score, candidate) for score, candidate in scores if score >= strategy.review_threshold]
        for score, candidate in plausible:
            competes = any(
                other is not candidate
                and max(other.start_frame, candidate.start_frame) < min(other.end_frame, candidate.end_frame)
                and (other_score >= strategy.threshold or abs(score - other_score) <= strategy.tie_margin)
                for other_score, other in plausible
            )
            segment = Segment(candidate.reference_id, candidate.offset_frame, candidate.start_frame,
                              candidate.end_frame, score, candidate.aligned_count)
            (uncertain if score < strategy.threshold or competes else confirmed).append(segment)
    return _merge_segments(confirmed), _merge_segments(uncertain)


def _merge_segments(segments: list[Segment]) -> tuple[Segment, ...]:
    by_reference: dict[str, list[Segment]] = defaultdict(list)
    for segment in sorted(segments, key=lambda item: item.start_frame):
        merged = by_reference[segment.reference_id]
        if merged and abs(merged[-1].offset_frame - segment.offset_frame) <= 2 and segment.start_frame <= merged[-1].end_frame:
            last = merged[-1]
            merged[-1] = replace(last, end_frame=max(last.end_frame, segment.end_frame),
                                 score=max(last.score, segment.score), aligned_count=max(last.aligned_count, segment.aligned_count))
        else:
            merged.append(segment)
    return tuple(sorted((item for group in by_reference.values() for item in group), key=lambda item: item.start_frame))


class MemoryIndex:
    def __init__(self, catalog: dict[str, list[Landmark]]):
        self._postings: dict[int, list[Posting]] = defaultdict(list)
        for reference_id, landmarks in catalog.items():
            for item in landmarks:
                self._postings[item.code].append(Posting(reference_id, item.frame))

    def postings(self, codes: set[int]) -> dict[int, list[Posting]]:
        return {code: self._postings[code] for code in codes
                if code in self._postings and len(self._postings[code]) <= MAX_POSTINGS}
