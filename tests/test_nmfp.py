import numpy as np

from secure_audio.decoder import SAMPLE_RATE
from secure_audio.domain import Recognition
from secure_audio.nmfp import NmfpIndex


class FixedEncoder:
    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, _):
        return self.vectors


def test_temporal_match_finds_offset_and_rejects_ambiguous_candidates():
    vectors = np.eye(128, dtype=np.float32)
    query = vectors[12:19]
    samples = np.zeros(4 * SAMPLE_RATE, dtype=np.float32)
    encoder = FixedEncoder(query)
    references = {"target": vectors[:40], "other": vectors[40:80]}
    decision = NmfpIndex(encoder, references).retrieve(samples).decide(0.9, 0.85)
    assert decision.status == Recognition.MATCH
    assert decision.segments[0].reference_id == "target"
    assert abs(decision.segments[0].payload()["reference_start_ms"] - 6000) < 12

    references["other"] = vectors[:40]
    decision = NmfpIndex(encoder, references).retrieve(samples).decide(0.9, 0.85)
    assert decision.status == Recognition.AMBIGUOUS
    assert set(decision.competing_references) == {"target", "other"}

    decision = NmfpIndex(FixedEncoder(vectors[90:97]), references).retrieve(samples).decide(0.9, 0.85)
    assert decision.status == Recognition.NO_MATCH
