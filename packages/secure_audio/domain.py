from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator


class Role(StrEnum):
    CONTRIBUTOR = "contributor"
    REVIEWER = "reviewer"
    UPLOADER = "uploader"


class JobKind(StrEnum):
    REGISTRATION = "registration"
    SCREENING = "screening"
    USAGE_AUDIT = "usage_audit"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Recognition(StrEnum):
    MATCH = "match"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


class PolicyOutcome(StrEnum):
    PERMITTED = "permitted"
    RESTRICTED = "restricted"
    REVIEW = "review"
    NOT_EVALUATED = "not_evaluated"


class DomainError(Exception):
    pass


class Forbidden(DomainError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class CapacityExceeded(DomainError):
    pass


class InvalidMedia(DomainError):
    pass


@dataclass(frozen=True)
class Principal:
    id: str
    tenant_id: str
    role: Role


UseName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Source = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]


class UsagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    permitted_uses: frozenset[UseName] = frozenset()
    restricted_uses: frozenset[UseName] = frozenset()
    valid_from: date | None = None
    valid_until: date | None = None

    @model_validator(mode="after")
    def check_consistency(self):
        if self.permitted_uses & self.restricted_uses:
            raise ValueError("Each use must have one policy outcome")
        if self.valid_from and self.valid_until and self.valid_from > self.valid_until:
            raise ValueError("Policy dates are out of order")
        return self

    def snapshot(self) -> dict:
        data = self.model_dump(mode="json")
        data["permitted_uses"] = sorted(self.permitted_uses)
        data["restricted_uses"] = sorted(self.restricted_uses)
        return data


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    source: Source
    policy: UsagePolicy


class ScreeningInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intended_use: UseName


def evaluate_policy(policy: UsagePolicy, intended_use: str, evaluated_at: datetime) -> PolicyOutcome:
    day = evaluated_at.date()
    if (policy.valid_from and day < policy.valid_from) or (policy.valid_until and day > policy.valid_until):
        return PolicyOutcome.REVIEW
    if intended_use in policy.permitted_uses:
        return PolicyOutcome.PERMITTED
    if intended_use in policy.restricted_uses:
        return PolicyOutcome.RESTRICTED
    return PolicyOutcome.REVIEW
