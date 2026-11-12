from dataclasses import dataclass, replace
from collections.abc import Callable
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .crypto import digest, read_json
from .decoder import SAMPLE_RATE, SubprocessDecoder, _decode
from .domain import Recognition
from .fingerprint import FRAME_MS, HOP, Candidate, MatchDecision, MemoryIndex, decide, fingerprint, retrieve_landmarks
from .scoring import LogisticModel, RuleScorer

if TYPE_CHECKING:
    from .nmfp import NeuralEvidence, NmfpEncoder


log = logging.getLogger(__name__)


class ReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source_group: str
    path: Path
    license: str


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    source_group: str
    condition: str
    reference_id: str | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_interval(self):
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("Provide both interval bounds")
        if self.start_ms is not None and (self.reference_id is None or self.end_ms <= self.start_ms):
            raise ValueError("An interval requires a positive query and ordered bounds")
        return self


class SplitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: list[ReferenceInput] = Field(min_length=1)
    queries: list[QueryInput] = Field(min_length=2)


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    train: SplitInput
    validation: SplitInput
    test: SplitInput


@dataclass(frozen=True)
class Example:
    windows: list[list[Candidate]]
    reference_id: str | None
    condition: str
    retrieval_ms: float
    start_ms: int | None = None
    end_ms: int | None = None
    fingerprint_ms: float = 0
    neural: "NeuralEvidence | None" = None


def demo_signal(seed: int, seconds: int = 14) -> np.ndarray:
    rng = np.random.default_rng(seed)
    samples = np.zeros(SAMPLE_RATE * seconds, dtype=np.float32)
    for start in range(0, samples.size, SAMPLE_RATE // 5):
        length = min(samples.size - start, SAMPLE_RATE // 5)
        t = np.arange(length) / SAMPLE_RATE
        freqs = rng.integers(180, 2400, 6)
        notes = np.sin(2 * np.pi * freqs[:, None] * t).mean(axis=0)
        samples[start:start + length] = notes * np.hanning(length) * 0.85
    return samples


def _retrieve(samples, index, reference_id, condition, start_ms=None, end_ms=None) -> Example:
    start = perf_counter()
    landmarks = fingerprint(samples)
    extracted = perf_counter()
    windows = retrieve_landmarks(landmarks, samples.size // HOP, index)
    return Example(windows, reference_id, condition, (perf_counter() - start) * 1000, start_ms, end_ms,
                   (extracted - start) * 1000)


def _demo_split(seed: int) -> tuple[list[Example], int]:
    signals = {f"reference-{n}": demo_signal(n) for n in range(seed, seed + 8)}
    fingerprints = {name: fingerprint(signal) for name, signal in signals.items()}
    index = MemoryIndex(fingerprints)
    rng = np.random.default_rng(seed)
    examples = []
    for name, signal in signals.items():
        crop = signal[SAMPLE_RATE * 2:SAMPLE_RATE * 12]
        noise = rng.normal(0, 0.05, crop.size)
        mixed = rng.normal(0, 0.03, crop.size)
        mixed[SAMPLE_RATE * 2:SAMPLE_RATE * 8] += crop[:SAMPLE_RATE * 6]
        for waveform, condition, start, end in ((crop, "clean", 0, 10000), (crop + noise, "noise", 0, 10000),
                                                (mixed, "inserted", 2000, 8000)):
            examples.append(_retrieve(waveform, index, name, condition, start, end))
    for number in range(8):
        examples.append(_retrieve(demo_signal(seed + 50 + number, 10), index, None, "unknown"))
    for signal in list(signals.values())[:4]:
        confuser = rng.normal(0, 0.055, SAMPLE_RATE * 10)
        confuser[:SAMPLE_RATE // 2] += signal[:SAMPLE_RATE // 2]
        examples.append(_retrieve(confuser, index, None, "short_shared_excerpt"))
    return examples, sum(map(len, fingerprints.values()))


def _load_manifest(path: Path, encoder: "NmfpEncoder | None" = None, trusted_corpus: bool = False):
    from .nmfp import NmfpIndex

    manifest = ExperimentInput.model_validate(read_json(path.read_bytes()))
    decoder = SubprocessDecoder()
    groups_seen: set[str] = set()
    digest_groups: dict[str, str] = {}
    results = []
    for name in ("train", "validation", "test"):
        split = getattr(manifest, name)
        references = {reference.id: reference for reference in split.references}
        log.info("Indexing %s: %d references, %d queries", name, len(references), len(split.queries))
        groups = {reference.source_group for reference in split.references} | {query.source_group for query in split.queries}
        if groups & groups_seen or len(references) != len(split.references):
            raise ValueError("Source groups must stay within one split and reference IDs must be unique in each split")
        groups_seen.update(groups)
        if not any(query.reference_id for query in split.queries) or not any(query.reference_id is None for query in split.queries):
            raise ValueError("Each split needs known and unknown queries")

        def decode(item, max_seconds):
            source = path.parent / item.path
            if source.stat().st_size > 12 * 1024 * 1024:
                raise ValueError("Benchmark audio exceeds the configured byte limit")
            data = source.read_bytes()
            checksum = digest(data)
            if digest_groups.setdefault(checksum, item.source_group) != item.source_group:
                raise ValueError("Identical audio bytes have inconsistent source groups")
            if trusted_corpus:
                samples = np.frombuffer(_decode(data, max_seconds), dtype="<f4")
                if not np.isfinite(samples).all():
                    raise ValueError(f"Invalid audio samples in {source}")
            else:
                samples = decoder.decode(data, max_seconds)
            return samples, checksum

        fingerprints, reference_digests, embeddings = {}, set(), {}
        for reference in split.references:
            samples, checksum = decode(reference, 180)
            if checksum in reference_digests:
                raise ValueError("Duplicate benchmark reference bytes")
            reference_digests.add(checksum)
            fingerprints[reference.id] = fingerprint(samples)
            if encoder is not None and name != "train":
                embeddings[reference.id] = encoder.embed(samples)
        if any(not landmarks for landmarks in fingerprints.values()):
            raise ValueError("Benchmark reference has no acoustic landmarks")
        index = MemoryIndex(fingerprints)
        neural_index = NmfpIndex(encoder, embeddings) if embeddings else None
        examples = []
        for number, query in enumerate(split.queries, 1):
            if query.reference_id is not None and (query.reference_id not in references
                                                   or references[query.reference_id].source_group != query.source_group):
                raise ValueError("Positive query must name its source reference in the same split")
            samples, checksum = decode(query, 60)
            if query.reference_id is None and checksum in reference_digests:
                raise ValueError("An unknown query is identical to an indexed recording")
            if query.end_ms is not None and query.end_ms > round(samples.size * 1000 / SAMPLE_RATE):
                raise ValueError("Query annotation exceeds the decoded audio duration")
            example = _retrieve(samples, index, query.reference_id, query.condition, query.start_ms, query.end_ms)
            if neural_index is not None:
                example = replace(example, neural=neural_index.retrieve(samples))
            examples.append(example)
            if number % 50 == 0 or number == len(split.queries):
                log.info("%s: %d/%d queries scored", name, number, len(split.queries))
        results.append((examples, sum(map(len, fingerprints.values()))))
    return results


def _fit(examples: list[Example]) -> LogisticModel:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    items = [(item, example) for example in examples for window in example.windows for item in window
             if item.has_minimum_evidence]
    features = np.array([item.features for item, _ in items])
    labels = [_correct_candidate(item, example) for item, example in items]
    if len(set(labels)) < 2:
        raise ValueError("Training needs both correct candidates and hard negative candidates")
    scaler = StandardScaler().fit(features)
    estimator = LogisticRegression(class_weight="balanced", random_state=0).fit(scaler.transform(features), labels)
    return LogisticModel(center=tuple(scaler.mean_), scale=tuple(scaler.scale_), coefficients=tuple(estimator.coef_[0]),
                         intercept=float(estimator.intercept_[0]))


def _correct_candidate(candidate: Candidate, example: Example) -> bool:
    if candidate.reference_id != example.reference_id:
        return False
    if example.start_ms is None:
        return True
    start, end = candidate.start_frame * FRAME_MS, candidate.end_frame * FRAME_MS
    overlap = min(end, example.end_ms) - max(start, example.start_ms)
    return overlap >= (end - start) / 2


def _measure(examples: list[Example], evaluate: Callable[[Example], MatchDecision], neural: bool = False) -> dict:
    positive = sum(example.reference_id is not None for example in examples)
    negative = len(examples) - positive
    retrieved = correct = wrong = false_accepts = reviews = 0
    errors, verification_ms = [], []
    for example in examples:
        start = perf_counter()
        decision = evaluate(example)
        verification_ms.append((perf_counter() - start) * 1000)
        reviews += decision.status == Recognition.AMBIGUOUS
        accepted = {segment.reference_id for segment in decision.segments} if decision.status == Recognition.MATCH else set()
        reference_id = example.reference_id
        if reference_id is None:
            false_accepts += bool(accepted)
            continue
        candidates = example.neural.candidates if neural else [item for window in example.windows for item in window]
        retrieved += any(item.reference_id == reference_id for item in candidates)
        correct += accepted == {reference_id}
        wrong += bool(accepted - {reference_id})
        if accepted == {reference_id} and example.start_ms is not None:
            intervals = [segment.payload() for segment in decision.segments]
            errors.extend((abs(min(item["query_start_ms"] for item in intervals) - example.start_ms),
                           abs(max(item["query_end_ms"] for item in intervals) - example.end_ms)))
    extraction = [item.neural.extraction_ms if neural else item.fingerprint_ms for item in examples]
    lookup = [item.neural.lookup_ms if neural else item.retrieval_ms - item.fingerprint_ms for item in examples]
    totals = np.array(extraction) + lookup + verification_ms
    return {"known_uploads": positive, "unknown_uploads": negative, "candidate_recall_at_5": retrieved / positive if positive else None,
            "correct_accepts": correct, "wrong_track_accepts": wrong, "false_accepts": false_accepts,
            "false_accept_rate": false_accepts / negative if negative else None, "review_rate": reviews / len(examples),
            "boundary_error_p50_ms": float(np.median(errors)) if errors else None,
            "boundary_error_p95_ms": float(np.percentile(errors, 95)) if errors else None,
            "fingerprint_p95_ms": float(np.percentile(extraction, 95)), "lookup_p95_ms": float(np.percentile(lookup, 95)),
            "retrieval_p95_ms": float(np.percentile(np.array(extraction) + lookup, 95)),
            "matching_p50_ms": float(np.median(totals)), "matching_p95_ms": float(np.percentile(totals, 95)),
            "verification_p95_ms": float(np.percentile(verification_ms, 95))}


def _calibrate(strategy, examples: list[Example], budget: float):
    def at_threshold(threshold):
        options = {"threshold": threshold, "review_threshold": max(0.23, threshold - 0.12)}
        scorer = replace(strategy, **options) if isinstance(strategy, RuleScorer) else strategy.model_copy(update=options)
        return scorer, _measure(examples, lambda example: decide(example.windows, scorer))

    return _select_threshold(at_threshold, budget)


def _select_threshold(at_threshold, budget: float):
    best = None
    thresholds = sorted({*np.linspace(0.35, 1, 66), 0.995, 0.999, 0.9995, 0.9999})
    for threshold in thresholds:
        candidate, result = at_threshold(float(threshold))
        if result["false_accept_rate"] > budget:
            continue
        key = (result["correct_accepts"], -result["wrong_track_accepts"], -result["review_rate"], float(threshold))
        if best is None or key > best[0]:
            best = (key, candidate, result)
    if best is None:
        raise ValueError("No validation threshold satisfies the requested false-accept budget")
    return best[1:]


def run_benchmark(output: Path, manifest: Path | None = None, false_accept_budget: float = 0.01,
                  nmfp_model: Path | None = None, trusted_corpus: bool = False) -> dict:
    if not 0 <= false_accept_budget < 1:
        raise ValueError("False-accept budget must be between zero and one")
    encoder = None
    if nmfp_model:
        if manifest is None:
            raise ValueError("The NMFP benchmark requires an audio manifest")
        from .nmfp import NmfpEncoder

        encoder = NmfpEncoder(nmfp_model)
    train, validation, test = (_load_manifest(manifest, encoder, trusted_corpus) if manifest
                               else [_demo_split(seed) for seed in (100, 200, 300)])
    model, ml_validation = _calibrate(_fit(train[0]), validation[0], false_accept_budget)
    rules, rules_validation = _calibrate(RuleScorer(), validation[0], false_accept_budget)
    cases = {name: [example for example in test[0] if example.condition == name]
             for name in sorted({example.condition for example in test[0]})}
    report = {"corpus": "manifest" if manifest else "generated_smoke_only", "false_accept_budget": false_accept_budget,
              "test_index_postings": test[1], "strategies": {}}
    for name, scorer, results in (("rules", rules, rules_validation), ("logistic", model, ml_validation)):
        def evaluate(example):
            return decide(example.windows, scorer)

        report["strategies"][name] = {"version": scorer.version, "threshold": scorer.threshold,
                                       "review_threshold": scorer.review_threshold, "validation": results,
                                       "test": _measure(test[0], evaluate),
                                       "by_condition": {label: _measure(items, evaluate) for label, items in cases.items()}}
    if encoder is not None:
        def at_threshold(threshold):
            review_threshold = max(0.25, threshold - 0.06)
            return (threshold, review_threshold), _measure(
                validation[0], lambda item: item.neural.decide(threshold, review_threshold), neural=True,
            )

        (threshold, review_threshold), results = _select_threshold(at_threshold, false_accept_budget)

        def evaluate_neural(example):
            return example.neural.decide(threshold, review_threshold)

        report["strategies"]["nmfp_triplet"] = {
            "version": "nmfp-triplet-onnx-temporal-v1", "weights_sha256": encoder.sha256, "threshold": threshold,
            "review_threshold": review_threshold, "validation": results, "test": _measure(test[0], evaluate_neural, neural=True),
            "by_condition": {label: _measure(items, evaluate_neural, neural=True) for label, items in cases.items()},
        }
    output.mkdir(parents=True, exist_ok=True)
    (output / "logistic-model.json").write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
