from dataclasses import dataclass
from math import exp, isfinite
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from .crypto import canonical, digest, read_json
from .fingerprint import Candidate, FEATURE_NAMES


@dataclass(frozen=True)
class RuleScorer:
    threshold: float = 0.50
    review_threshold: float = 0.37
    tie_margin: float = 0.04

    @property
    def version(self) -> str:
        return "rules-v1-" + digest(canonical([self.threshold, self.review_threshold, self.tie_margin]))[:16]

    def score(self, candidate: Candidate) -> float:
        return (0.26 * min(1, candidate.aligned_count / 24)
                + 0.24 * min(1, candidate.aligned_fraction / 0.22)
                + 0.15 * min(1, candidate.concentration / 0.55)
                + 0.13 * candidate.coverage
                + 0.12 * min(1, candidate.duration / 2.5)
                + 0.10 * max(0, candidate.margin))


class LogisticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    features: tuple[str, ...] = FEATURE_NAMES
    center: tuple[float, ...]
    scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    threshold: float = 0.5
    review_threshold: float = 0.4
    tie_margin: float = 0.04

    @model_validator(mode="after")
    def check_model(self):
        columns = (self.center, self.scale, self.coefficients)
        if self.schema_version != 1 or self.features != FEATURE_NAMES or any(len(column) != len(FEATURE_NAMES) for column in columns):
            raise ValueError("Incompatible model feature schema")
        if not all(isfinite(value) for column in columns for value in column) or not isfinite(self.intercept):
            raise ValueError("Model parameters must be finite")
        if min(self.scale) <= 0 or not 0 <= self.review_threshold <= self.threshold <= 1 or not 0 <= self.tie_margin <= 1:
            raise ValueError("Invalid scaling or operating thresholds")
        return self

    @property
    def version(self) -> str:
        return "logistic-v1-" + digest(canonical(self.model_dump(mode="json")))[:16]

    def score(self, candidate: Candidate) -> float:
        value = self.intercept + sum(coef * ((feature - center) / scale) for coef, feature, center, scale
                                     in zip(self.coefficients, candidate.features, self.center, self.scale, strict=True))
        return 1 / (1 + exp(-max(-700, min(700, value))))

    @classmethod
    def from_path(cls, path: Path):
        return cls.model_validate(read_json(path.read_bytes()))
