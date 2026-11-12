from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .crypto import EvidenceError, canonical, digest, verify_evidence
from .fingerprint import FINGERPRINT_VERSION, MAX_POSTINGS, Landmark, Posting


FINGERPRINT_ENCODING = "sorted-uint32-pairs-be-v1"
MAX_CATALOG_POSTINGS = 8_000_000


class ReferencePostings(Protocol):
    def reference_postings(self, reference_id: str, limit: int) -> list[tuple[int, int]]: ...


class CatalogSource(ReferencePostings, Protocol):
    def catalog(self, tenant_id: str) -> list[Mapping]: ...


def _encode(pairs: list[tuple[int, int]]) -> np.ndarray:
    values = np.asarray(pairs, dtype=np.int64).reshape(-1, 2)
    if np.any(values < 0) or np.any(values > np.iinfo(np.int32).max):
        raise EvidenceError("Fingerprint posting is outside its supported range")
    return values.astype(">u4")


@dataclass(frozen=True)
class FingerprintBinding:
    count: int
    sha256: str

    @classmethod
    def from_landmarks(cls, landmarks: list[Landmark]):
        return cls.from_pairs([(item.code, item.frame) for item in sorted(landmarks)])

    @classmethod
    def from_pairs(cls, sorted_pairs: list[tuple[int, int]]):
        values = _encode(sorted_pairs)
        return cls(len(values), digest(values.tobytes()))

    @classmethod
    def from_payload(cls, payload: object):
        if not isinstance(payload, dict) or payload.get("encoding") != FINGERPRINT_ENCODING:
            raise EvidenceError("Registration lacks a supported fingerprint binding")
        count, checksum = payload.get("count"), payload.get("sha256")
        if (type(count) is not int or not 24 <= count <= MAX_CATALOG_POSTINGS
                or not isinstance(checksum, str) or len(checksum) != 64
                or any(character not in "0123456789abcdef" for character in checksum)):
            raise EvidenceError("Invalid fingerprint binding")
        return cls(count, checksum)

    def payload(self) -> dict:
        return {"encoding": FINGERPRINT_ENCODING, "count": self.count, "sha256": self.sha256}

    def read_verified(self, repository: ReferencePostings, reference_id: str) -> np.ndarray:
        pairs = repository.reference_postings(reference_id, self.count + 1)
        if len(pairs) != self.count:
            raise EvidenceError("Fingerprint posting count does not match approval")
        values = _encode(pairs)
        if digest(values.tobytes()) != self.sha256:
            raise EvidenceError("Fingerprint postings do not match approval")
        return values


def indexing_identity(reference_id: str, tenant_id: str, contributor_id: str, media_sha256: str, details: dict) -> dict:
    return {
        "registration_id": reference_id, "tenant_id": tenant_id, "contributor_id": contributor_id,
        "media_sha256": media_sha256, "fingerprint_version": FINGERPRINT_VERSION,
        "registration_details_sha256": digest(canonical(details)),
    }


class VerifiedPostingsIndex:
    def __init__(self, references: dict[str, np.ndarray]):
        self._references = tuple(references)
        lengths = [len(rows) for rows in references.values()]
        codes = np.concatenate([rows[:, 0] for rows in references.values()]) if lengths else np.empty(0, dtype=np.uint32)
        frames = np.concatenate([rows[:, 1] for rows in references.values()]) if lengths else np.empty(0, dtype=np.uint32)
        slots = np.repeat(np.arange(len(lengths), dtype=np.int32), lengths)
        order = np.argsort(codes, kind="stable")
        self._codes, self._frames, self._slots = codes[order], frames[order], slots[order]
        for column in (self._codes, self._frames, self._slots):
            column.setflags(write=False)

    def postings(self, codes: set[int]) -> dict[int, list[Posting]]:
        wanted = sorted(codes)
        starts = self._codes.searchsorted(wanted)
        ends = self._codes.searchsorted(wanted, side="right")
        return {
            code: [Posting(self._references[slot], frame)
                   for slot, frame in zip(self._slots[start:end].tolist(), self._frames[start:end].tolist(), strict=True)]
            for code, start, end in zip(wanted, starts, ends, strict=True)
            if 0 < end - start <= MAX_POSTINGS
        }


@dataclass(frozen=True)
class CatalogSnapshot:
    sha256: str
    references: dict[str, dict]
    index: VerifiedPostingsIndex


class CatalogVerifier:
    def __init__(self, repository: CatalogSource, public_key: bytes):
        self.repository = repository
        self.public_key = public_key
        self._cache: tuple[str, CatalogSnapshot] | None = None

    def load(self, tenant_id: str) -> CatalogSnapshot:
        references: dict[str, dict] = {}
        bindings: dict[str, FingerprintBinding] = {}
        manifest_digests = []
        for row in self.repository.catalog(tenant_id):
            manifest = row["manifest"]
            payload = verify_evidence(manifest, self.public_key, "registration", row["media_sha256"])
            identity = {
                "registration_id": row["id"], "tenant_id": tenant_id, "fingerprint_version": FINGERPRINT_VERSION,
                "contributor_id": row["contributor_id"], "reviewer_id": row["reviewer_id"],
            }
            if any(payload.get(field) != value for field, value in identity.items()):
                raise EvidenceError("Registration does not belong to the approved catalog")
            references[row["id"]] = payload
            bindings[row["id"]] = FingerprintBinding.from_payload(payload.get("fingerprint"))
            manifest_digests.append(digest(canonical(manifest)))

        catalog_digest = digest(canonical(["verified-catalog-v1", tenant_id, manifest_digests]))
        if self._cache is not None:
            cached_tenant, snapshot = self._cache
            if tenant_id == cached_tenant and catalog_digest == snapshot.sha256:
                return snapshot
        if sum(binding.count for binding in bindings.values()) > MAX_CATALOG_POSTINGS:
            raise EvidenceError("Approved catalog exceeds the verified snapshot size limit")
        verified = {reference_id: binding.read_verified(self.repository, reference_id)
                    for reference_id, binding in bindings.items()}
        snapshot = CatalogSnapshot(catalog_digest, references, VerifiedPostingsIndex(verified))
        self._cache = (tenant_id, snapshot)
        return snapshot
