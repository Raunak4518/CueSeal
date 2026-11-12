from collections import defaultdict
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path
from time import perf_counter

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.signal import resample_poly

from .decoder import SAMPLE_RATE
from .domain import Recognition
from .fingerprint import FRAME_MS, MatchDecision, Segment


WINDOW_SAMPLES = 8000
HOP_SAMPLES = 4000
HOP_MS = 500
MODEL_SHA256 = "d48b86bbf6cb35d46716e1a491f2a7a42f4e51119f81c50b7c756eee3452b3e2"


class NmfpEncoder:
    def __init__(self, path: Path):
        try:
            import onnxruntime
        except ImportError as exc:
            raise ValueError("Install the nmfp extra to evaluate the ONNX checkpoint") from exc
        with path.open("rb") as stream:
            self.sha256 = file_digest(stream, "sha256").hexdigest()
        if self.sha256 != MODEL_SHA256:
            raise ValueError("Expected the pinned NMFP Triplet waveform ONNX checkpoint")
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])

    def embed(self, samples: np.ndarray) -> np.ndarray:
        waveform = resample_poly(samples, 8000, SAMPLE_RATE)
        if waveform.size < WINDOW_SAMPLES:
            return np.empty((0, 128), dtype=np.float32)
        windows = np.ascontiguousarray(sliding_window_view(waveform, WINDOW_SAMPLES)[::HOP_SAMPLES], dtype=np.float32)
        vectors = self._session.run(["embedding"], {"waveform": windows})[0]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors /= np.maximum(norms, 1e-12)
        vectors[np.max(np.abs(windows), axis=1) < 1e-6] = 0
        return vectors


@dataclass(frozen=True)
class NeuralEvidence:
    candidates: tuple[Segment, ...]
    extraction_ms: float
    lookup_ms: float

    def decide(self, threshold: float, review_threshold: float) -> MatchDecision:
        if not self.candidates or self.candidates[0].score < review_threshold:
            return MatchDecision(Recognition.NO_MATCH, ())
        best = self.candidates[0]
        alternatives = tuple(item.reference_id for item in self.candidates[1:]
                             if item.score >= review_threshold and best.score - item.score <= 0.04)
        if alternatives or best.score < threshold:
            return MatchDecision(Recognition.AMBIGUOUS, (), (best.reference_id, *alternatives))
        return MatchDecision(Recognition.MATCH, (best,))


class NmfpIndex:
    def __init__(self, encoder: NmfpEncoder, references: dict[str, np.ndarray]):
        self.encoder = encoder
        self.references = tuple(references)
        matrices = list(references.values())
        self.vectors = np.concatenate(matrices) if matrices else np.empty((0, 128), dtype=np.float32)
        self.track_ids = np.repeat(np.arange(len(matrices)), [len(item) for item in matrices])
        self.track_frames = np.concatenate([np.arange(len(item)) for item in matrices]) if matrices else np.empty(0, dtype=int)

    def retrieve(self, samples: np.ndarray) -> NeuralEvidence:
        start = perf_counter()
        query = self.encoder.embed(samples)
        extracted = perf_counter()
        candidates = self._candidates(query, samples.size * 1000 / SAMPLE_RATE)
        return NeuralEvidence(candidates, (extracted - start) * 1000, (perf_counter() - extracted) * 1000)

    def _candidates(self, query: np.ndarray, duration_ms: float) -> tuple[Segment, ...]:
        if not query.size or not self.vectors.size:
            return ()
        similarity = query @ self.vectors.T
        k = min(32, similarity.shape[1])
        neighbors = np.argpartition(similarity, -k, axis=1)[:, -k:]
        votes = defaultdict(list)
        for query_frame, row in enumerate(neighbors):
            for index in row:
                score = similarity[query_frame, index]
                if score > 0:
                    key = (self.track_ids[index], self.track_frames[index] - query_frame)
                    votes[key].append((query_frame, float(score)))
        best = {}
        for (track, offset), points in votes.items():
            if len(points) < 3:
                continue
            segment = self._best_run(self.references[track], int(offset), points, duration_ms)
            if segment and (track not in best or segment.score > best[track].score):
                best[track] = segment
        return tuple(sorted(best.values(), key=lambda item: (-item.score, item.reference_id))[:5])

    @staticmethod
    def _best_run(reference_id: str, offset: int, points: list[tuple[int, float]], duration_ms: float) -> Segment | None:
        runs = [[points[0]]]
        for point in points[1:]:
            if point[0] - runs[-1][-1][0] > 2:
                runs.append([])
            runs[-1].append(point)
        choices = [run for run in runs if len(run) >= 3]
        if not choices:
            return None

        def score(run):
            coverage = len(run) / (run[-1][0] - run[0][0] + 1)
            return sum(value for _, value in run) / len(run) * min(1, len(run) / 6) * coverage

        run = max(choices, key=score)
        begin = round(run[0][0] * HOP_MS / FRAME_MS)
        end = round(min(duration_ms, run[-1][0] * HOP_MS + 1000) / FRAME_MS)
        return Segment(reference_id, round(offset * HOP_MS / FRAME_MS), begin, end, min(1.0, score(run)), len(run))
