from datetime import UTC, datetime
import logging
from time import time

from cryptography.exceptions import InvalidTag

from .catalog import CatalogVerifier, FingerprintBinding, indexing_identity
from .crypto import EvidenceError, EvidenceSigner, MediaCipher, digest
from .decoder import SAMPLE_RATE, DecodeError, Decoder
from .domain import Conflict, JobKind, PolicyOutcome, Recognition, UsagePolicy, evaluate_policy
from .fingerprint import FINGERPRINT_VERSION, ScoreStrategy, decide, fingerprint, retrieve, timed_matches
from .repository import JobRequest, WorkerRepository
from .usage import USAGE_REPORT_TYPE, UsageAuditInput, reconcile_usage


log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, repository: WorkerRepository, decoder: Decoder, scorer: ScoreStrategy,
                 signer: EvidenceSigner, cipher: MediaCipher, lease_seconds: int = 180):
        self.repository = repository
        self.decoder = decoder
        self.scorer = scorer
        self.signer = signer
        self.cipher = cipher
        self.catalog_verifier = CatalogVerifier(repository, signer.public_key)
        self.lease_seconds = lease_seconds

    def run_one(self, *, job_id: str | None = None) -> bool:
        job = self.repository.claim(int(time()), self.lease_seconds, max_attempts=3, job_id=job_id)
        if job is None:
            return False
        try:
            request = JobRequest.from_row(job)
            associated_data = request.associated_data()
            audio = self.cipher.decrypt(job["ciphertext"], associated_data)
            if digest(audio) != request.media_sha256:
                raise InvalidTag
            registration = request.kind == JobKind.REGISTRATION
            samples = self.decoder.decode(audio, max_seconds=180 if registration else 60)
            if registration:
                landmarks = fingerprint(samples)
                if len(landmarks) < 24:
                    raise DecodeError("Reference has insufficient distinct acoustic evidence")
                identity = indexing_identity(request.id, request.tenant_id, request.owner_id, request.media_sha256, request.context)
                attestation = self.signer.sign("reference_fingerprint", {
                    **identity, "fingerprint": FingerprintBinding.from_landmarks(landmarks).payload(),
                })
                result = {"registration_id": request.id, "review_status": "pending", "landmark_count": len(landmarks),
                          "fingerprint_attestation": attestation}
                self.repository.complete(job, int(time()), result, landmarks)
            elif request.kind == JobKind.SCREENING:
                report = self._screen(request, digest(associated_data), samples)
                self.repository.complete(job, int(time()), {"report": report})
            elif request.kind == JobKind.USAGE_AUDIT:
                report = self._audit_usage(request, digest(associated_data), samples)
                self.repository.complete(job, int(time()), {"report": report})
            else:
                raise EvidenceError("Unsupported job kind")
        except (DecodeError, Conflict) as exc:
            self.repository.fail(job, int(time()), str(exc))
        except (InvalidTag, EvidenceError):
            self.repository.fail(job, int(time()), "Queued request or catalog integrity verification failed")
        except Exception:
            log.exception("Screening worker error for job %s", job["id"])
            self.repository.fail(job, int(time()), "Job processing failed")
        return True

    def _audit_usage(self, request: JobRequest, request_digest: str, samples) -> dict:
        audit = UsageAuditInput.model_validate(request.context)
        catalog = self.catalog_verifier.load(request.tenant_id)
        confirmed, uncertain = timed_matches(retrieve(samples, catalog.index), self.scorer)
        duration_ms = round(samples.size * 1000 / SAMPLE_RATE)
        return self.signer.sign(USAGE_REPORT_TYPE, {
            "report_id": request.id,
            "tenant_id": request.tenant_id,
            "media_sha256": request.media_sha256,
            "cue_sheet_sha256": audit.cue_sheet_sha256,
            "request_sha256": request_digest,
            "programme_id": audit.programme_id,
            "origin_ms": audit.origin_ms,
            "duration_ms": duration_ms,
            "catalog_digest": catalog.sha256,
            "fingerprint_version": FINGERPRINT_VERSION,
            "scoring_version": self.scorer.version,
            "threshold": self.scorer.threshold,
            "review_threshold": self.scorer.review_threshold,
            "tie_margin": self.scorer.tie_margin,
            "tolerance_ms": audit.tolerance_ms,
            "evaluated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "findings": reconcile_usage(audit, set(catalog.references), duration_ms, confirmed, uncertain),
            "uncertain_intervals": [segment.payload() for segment in uncertain],
        })

    def _screen(self, request: JobRequest, request_digest: str, samples) -> dict:
        catalog = self.catalog_verifier.load(request.tenant_id)
        decision = decide(retrieve(samples, catalog.index), self.scorer)
        evaluated_at = datetime.now(UTC)
        intended_use = request.context["intended_use"]
        segments = []
        for segment in decision.segments:
            reference = catalog.references[segment.reference_id]
            policy = UsagePolicy.model_validate(reference["policy"])
            outcome = evaluate_policy(policy, intended_use, evaluated_at)
            segments.append({**segment.payload(), "title": reference["title"], "reference_sha256": reference["media_sha256"],
                             "reference_fingerprint_sha256": reference["fingerprint"]["sha256"],
                             "policy_version": reference["policy_version"], "policy_outcome": outcome})

        overall = PolicyOutcome.NOT_EVALUATED
        if decision.status == Recognition.AMBIGUOUS:
            overall = PolicyOutcome.REVIEW
        elif segments:
            outcomes = {segment["policy_outcome"] for segment in segments}
            overall = outcomes.pop() if len(outcomes) == 1 else PolicyOutcome.REVIEW

        return self.signer.sign("screening_report", {
            "report_id": request.id,
            "tenant_id": request.tenant_id,
            "media_sha256": request.media_sha256,
            "request_sha256": request_digest,
            "catalog_digest": catalog.sha256,
            "fingerprint_version": FINGERPRINT_VERSION,
            "scoring_version": self.scorer.version,
            "evaluated_at": evaluated_at.isoformat(timespec="seconds"),
            "intended_use": intended_use,
            "recognition": decision.status,
            "policy_outcome": overall,
            "segments": segments,
            "competing_references": list(decision.competing_references),
        })
