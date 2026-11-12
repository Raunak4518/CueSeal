from datetime import UTC, datetime
from time import time
from uuid import uuid4

from .catalog import FingerprintBinding, indexing_identity
from .crypto import EvidenceError, EvidenceSigner, MediaCipher, canonical, digest, verify_evidence
from .domain import Conflict, Forbidden, InvalidMedia, JobKind, JobStatus, NotFound, Principal, RegistrationInput, Role, ScreeningInput
from .repository import JobRequest, QueuedJob, ScreeningRepository
from .usage import UsageAuditInput


class ScreeningService:
    def __init__(self, repository: ScreeningRepository, signer: EvidenceSigner, cipher: MediaCipher,
                 max_bytes: int = 12 * 1024 * 1024, job_quota: int = 8, media_ttl: int = 3600):
        self.repository = repository
        self.signer = signer
        self.cipher = cipher
        self.max_bytes = max_bytes
        self.job_quota = job_quota
        self.media_ttl = media_ttl

    def register(self, actor: Principal, media: bytes, details: RegistrationInput) -> dict:
        self.require_role(actor, Role.CONTRIBUTOR)
        context = {"title": details.title, "source": details.source, "policy": details.policy.snapshot()}
        return self._enqueue(actor, media, JobKind.REGISTRATION, context)

    def screen(self, actor: Principal, media: bytes, details: ScreeningInput) -> dict:
        self.require_role(actor, Role.UPLOADER)
        return self._enqueue(actor, media, JobKind.SCREENING, details.model_dump())

    def audit_usage(self, actor: Principal, media: bytes, details: UsageAuditInput) -> dict:
        self.require_role(actor, Role.UPLOADER)
        return self._enqueue(actor, media, JobKind.USAGE_AUDIT, details.model_dump(mode="json"))

    def _enqueue(self, actor: Principal, media: bytes, kind: JobKind, context: dict) -> dict:
        if not media or len(media) > self.max_bytes:
            raise InvalidMedia("Audio is empty or exceeds the upload limit")
        job_id = str(uuid4())
        now = int(time())
        request = JobRequest(job_id, actor.tenant_id, actor.id, kind, context, digest(media), now, now + self.media_ttl)
        encrypted = self.cipher.encrypt(media, request.associated_data())
        self.repository.submit(QueuedJob(request, encrypted), self.job_quota)
        return {"id": job_id, "status": JobStatus.QUEUED}

    def read_job(self, actor: Principal, job_id: str) -> dict:
        job = self.repository.job(actor.tenant_id, job_id)
        if not job or (job["owner_id"] != actor.id and actor.role != Role.REVIEWER):
            raise NotFound("Job not found")
        return {key: value for key, value in job.items() if key != "owner_id"}

    def read_registration(self, actor: Principal, registration_id: str) -> dict:
        row = self.repository.registration(actor.tenant_id, registration_id)
        if not row or (row["contributor_id"] != actor.id and actor.role != Role.REVIEWER):
            raise NotFound("Registration not found")
        return {"id": row["id"], "status": "approved" if row["reviewer_id"] else "pending", "manifest": row["manifest"]}

    def approve(self, actor: Principal, registration_id: str) -> dict:
        self.require_role(actor, Role.REVIEWER)
        row = self.repository.registration(actor.tenant_id, registration_id)
        if not row:
            raise NotFound("Indexed registration not found")
        if row["contributor_id"] == actor.id:
            raise Forbidden("A contributor cannot approve their own registration")
        if row["reviewer_id"]:
            return row["manifest"]
        details = row["details"]
        result = row["indexing_result"]
        identity = indexing_identity(registration_id, actor.tenant_id, row["contributor_id"], row["media_sha256"], details)
        try:
            if not isinstance(result, dict) or "fingerprint_attestation" not in result:
                raise EvidenceError("Registration lacks an indexing attestation")
            attested = verify_evidence(result["fingerprint_attestation"], self.signer.public_key,
                                       "reference_fingerprint", row["media_sha256"])
            if (row["fingerprint_version"] != identity["fingerprint_version"]
                    or any(attested.get(field) != value for field, value in identity.items())):
                raise EvidenceError("Indexing attestation does not belong to the registration")
            binding = FingerprintBinding.from_payload(attested.get("fingerprint"))
            binding.read_verified(self.repository, registration_id)
        except EvidenceError as exc:
            raise Conflict("Registration fingerprint integrity could not be verified") from exc
        manifest = self.signer.sign("registration", {
            **identity,
            "reviewer_id": actor.id,
            "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "fingerprint": binding.payload(),
            "policy_version": digest(canonical(details["policy"])),
            "title": details["title"],
            "source": details["source"],
            "policy": details["policy"],
        })
        if not self.repository.approve(actor.tenant_id, registration_id, actor.id, manifest):
            reviewed = self.repository.registration(actor.tenant_id, registration_id)
            if reviewed and reviewed["manifest"]:
                return reviewed["manifest"]
            raise Conflict("Registration has already been reviewed")
        return manifest

    @staticmethod
    def require_role(actor: Principal, role: Role) -> None:
        if actor.role != role:
            raise Forbidden("The actor does not have this role")
