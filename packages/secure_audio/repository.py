from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import uuid4

from sqlalchemy import (
    JSON, BigInteger, Column, ForeignKey, Index, Integer, LargeBinary, MetaData, String, Table,
    UniqueConstraint, and_, bindparam, create_engine, event, func, insert, or_, select, text, update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine, URL

from .crypto import canonical
from .domain import CapacityExceeded, Conflict, JobStatus, Principal, Role
from .fingerprint import FINGERPRINT_VERSION, MAX_POSTINGS, Landmark, Posting


schema = MetaData()
tenants = Table("tenants", schema, Column("id", String(64), primary_key=True))
principals = Table(
    "principals", schema, Column("id", String(36), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id"), nullable=False),
    Column("role", String(16), nullable=False), Column("token_digest", String(64), nullable=False, unique=True),
)
jobs = Table(
    "jobs", schema, Column("id", String(36), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id"), nullable=False),
    Column("owner_id", ForeignKey("principals.id"), nullable=False), Column("kind", String(16), nullable=False),
    Column("status", String(16), nullable=False), Column("context", JSON, nullable=False),
    Column("media_sha256", String(64), nullable=False), Column("ciphertext", LargeBinary),
    Column("created_at", BigInteger, nullable=False), Column("expires_at", BigInteger, nullable=False),
    Column("lease_id", String(36)), Column("lease_until", BigInteger), Column("attempts", Integer, nullable=False),
    Column("result", JSON), Column("error", String(160)),
    Index("jobs_claim", "status", "created_at"), Index("jobs_quota", "tenant_id", "status"),
)
registrations = Table(
    "registrations", schema, Column("id", ForeignKey("jobs.id"), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id"), nullable=False),
    Column("contributor_id", ForeignKey("principals.id"), nullable=False),
    Column("media_sha256", String(64), nullable=False), Column("fingerprint_version", String(32), nullable=False),
    Column("details", JSON, nullable=False), Column("reviewer_id", ForeignKey("principals.id")),
    Column("manifest", JSON),
    UniqueConstraint("tenant_id", "media_sha256"), Index("registrations_catalog", "tenant_id", "fingerprint_version"),
)
postings_table = Table(
    "fingerprint_postings", schema, Column("reference_id", ForeignKey("registrations.id"), primary_key=True),
    Column("code", Integer, primary_key=True), Column("frame", Integer, primary_key=True),
    Index("fingerprint_hash_lookup", "code", "reference_id"),
)


@dataclass(frozen=True)
class JobRequest:
    id: str
    tenant_id: str
    owner_id: str
    kind: str
    context: dict
    media_sha256: str
    created_at: int
    expires_at: int

    @classmethod
    def from_row(cls, row: Mapping):
        return cls(row["id"], row["tenant_id"], row["owner_id"], row["kind"], row["context"], row["media_sha256"],
                   row["created_at"], row["expires_at"])

    def associated_data(self) -> bytes:
        return canonical(["secure-audio-job-v2", self.id, self.tenant_id, self.owner_id, self.kind, self.context,
                          self.media_sha256, self.created_at, self.expires_at])


@dataclass(frozen=True)
class QueuedJob:
    request: JobRequest
    ciphertext: bytes


class ScreeningRepository(Protocol):
    def submit(self, job: QueuedJob, quota: int) -> None: ...
    def job(self, tenant_id: str, job_id: str) -> Mapping | None: ...
    def registration(self, tenant_id: str, registration_id: str) -> Mapping | None: ...
    def reference_postings(self, reference_id: str, limit: int) -> list[tuple[int, int]]: ...
    def approve(self, tenant_id: str, registration_id: str, reviewer_id: str, manifest: dict) -> bool: ...


class WorkerRepository(Protocol):
    def claim(self, now: int, lease_seconds: int, max_attempts: int, *, job_id: str | None = None) -> Mapping | None: ...
    def complete(self, job: Mapping, now: int, result: dict, landmarks: list[Landmark] | None = None) -> bool: ...
    def fail(self, job: Mapping, now: int, reason: str) -> bool: ...
    def catalog(self, tenant_id: str) -> list[Mapping]: ...
    def reference_postings(self, reference_id: str, limit: int) -> list[tuple[int, int]]: ...


_ACTIVE = (JobStatus.QUEUED, JobStatus.PROCESSING)
_PUBLIC_JOB_COLUMNS = tuple(jobs.c[name] for name in ("id", "owner_id", "kind", "status", "created_at", "result", "error"))


class SqlRepository:
    def __init__(self, database_url: str | URL):
        self.engine = create_engine(database_url, pool_pre_ping=True, hide_parameters=True)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)

    def initialize(self) -> None:
        schema.create_all(self.engine)
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as connection:
                connection.exec_driver_sql("""
                    CREATE INDEX IF NOT EXISTS fingerprint_code_cover
                    ON fingerprint_postings (code) INCLUDE (reference_id, frame)
                """)

    def add_principal(self, principal: Principal, token_digest: str) -> None:
        with self.engine.begin() as connection:
            try:
                with connection.begin_nested():
                    connection.execute(insert(tenants).values(id=principal.tenant_id))
            except IntegrityError:
                if connection.scalar(select(tenants.c.id).where(tenants.c.id == principal.tenant_id)) is None:
                    raise
            connection.execute(insert(principals).values(id=principal.id, tenant_id=principal.tenant_id,
                                                        role=principal.role, token_digest=token_digest))

    def principal(self, token_digest: str) -> Principal | None:
        statement = select(principals.c.id, principals.c.tenant_id, principals.c.role).where(principals.c.token_digest == token_digest)
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        return Principal(row.id, row.tenant_id, Role(row.role)) if row else None

    def submit(self, job: QueuedJob, quota: int) -> None:
        request = job.request
        with self.engine.begin() as connection:
            connection.execute(update(tenants).where(tenants.c.id == request.tenant_id).values(id=tenants.c.id))
            count = connection.scalar(select(func.count()).select_from(jobs).where(
                jobs.c.tenant_id == request.tenant_id, jobs.c.status.in_(_ACTIVE), jobs.c.expires_at > request.created_at,
            ))
            if count >= quota:
                raise CapacityExceeded("Tenant queue is full")
            connection.execute(insert(jobs).values(
                id=request.id, tenant_id=request.tenant_id, owner_id=request.owner_id, kind=request.kind, context=request.context,
                media_sha256=request.media_sha256, ciphertext=job.ciphertext, created_at=request.created_at,
                expires_at=request.expires_at, status=JobStatus.QUEUED, attempts=0,
            ))

    def job(self, tenant_id: str, job_id: str) -> Mapping | None:
        statement = select(*_PUBLIC_JOB_COLUMNS).where(jobs.c.tenant_id == tenant_id, jobs.c.id == job_id)
        with self.engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def registration(self, tenant_id: str, registration_id: str) -> Mapping | None:
        statement = select(registrations, jobs.c.result.label("indexing_result")).join(
            jobs, jobs.c.id == registrations.c.id,
        ).where(registrations.c.tenant_id == tenant_id, registrations.c.id == registration_id)
        with self.engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def approve(self, tenant_id: str, registration_id: str, reviewer_id: str, manifest: dict) -> bool:
        statement = update(registrations).where(
            registrations.c.id == registration_id, registrations.c.tenant_id == tenant_id,
            registrations.c.reviewer_id.is_(None), registrations.c.contributor_id != reviewer_id,
        ).values(reviewer_id=reviewer_id, manifest=manifest)
        with self.engine.begin() as connection:
            return connection.execute(statement).rowcount == 1

    def claim(self, now: int, lease_seconds: int, max_attempts: int, *, job_id: str | None = None) -> Mapping | None:
        lease_expired = and_(jobs.c.status == JobStatus.PROCESSING, jobs.c.lease_until <= now)
        available = and_(or_(jobs.c.status == JobStatus.QUEUED, lease_expired), jobs.c.expires_at > now,
                         jobs.c.attempts < max_attempts)
        with self.engine.begin() as connection:
            connection.execute(update(jobs).where(
                jobs.c.status.in_(_ACTIVE), or_(jobs.c.expires_at <= now, and_(lease_expired, jobs.c.attempts >= max_attempts)),
            ).values(status=JobStatus.FAILED, error="Job expired or exhausted its attempts", ciphertext=None,
                     lease_id=None, lease_until=None))
            candidate = select(jobs.c.id).where(available).order_by(jobs.c.created_at, jobs.c.id).limit(1)
            if job_id is not None:
                candidate = candidate.where(jobs.c.id == job_id)
            if self.engine.dialect.name == "postgresql":
                candidate = candidate.with_for_update(skip_locked=True)
            statement = update(jobs).where(jobs.c.id == candidate.scalar_subquery(), available).values(
                status=JobStatus.PROCESSING, lease_id=str(uuid4()), lease_until=now + lease_seconds,
                attempts=jobs.c.attempts + 1,
            ).returning(*jobs.c)
            return connection.execute(statement).mappings().one_or_none()

    def _finish(self, job: Mapping, now: int, status: JobStatus, **fields):
        return update(jobs).where(
            jobs.c.id == job["id"], jobs.c.status == JobStatus.PROCESSING, jobs.c.lease_id == job["lease_id"],
            jobs.c.lease_until > now, jobs.c.expires_at > now,
        ).values(status=status, ciphertext=None, lease_id=None, lease_until=None, **fields)

    def complete(self, job: Mapping, now: int, result: dict, landmarks: list[Landmark] | None = None) -> bool:
        with self.engine.begin() as connection:
            if connection.execute(self._finish(job, now, JobStatus.COMPLETED, result=result)).rowcount != 1:
                return False
            if landmarks is not None:
                try:
                    connection.execute(insert(registrations).values(
                        id=job["id"], tenant_id=job["tenant_id"], contributor_id=job["owner_id"],
                        media_sha256=job["media_sha256"], fingerprint_version=FINGERPRINT_VERSION, details=job["context"],
                    ))
                except IntegrityError as exc:
                    raise Conflict("This recording is already registered in the tenant") from exc
                for offset in range(0, len(landmarks), 1000):
                    connection.execute(insert(postings_table), [
                        {"reference_id": job["id"], "code": item.code, "frame": item.frame}
                        for item in landmarks[offset:offset + 1000]
                    ])
        return True

    def fail(self, job: Mapping, now: int, reason: str) -> bool:
        with self.engine.begin() as connection:
            return connection.execute(self._finish(job, now, JobStatus.FAILED, error=reason)).rowcount == 1

    def catalog(self, tenant_id: str) -> list[Mapping]:
        statement = select(registrations).where(
            registrations.c.tenant_id == tenant_id, registrations.c.reviewer_id.is_not(None),
            registrations.c.fingerprint_version == FINGERPRINT_VERSION,
        ).order_by(registrations.c.id)
        with self.engine.connect() as connection:
            return list(connection.execute(statement).mappings())

    def index(self, reference_ids: list[str]) -> "SqlPostingsIndex":
        return SqlPostingsIndex(self.engine, reference_ids)

    def reference_postings(self, reference_id: str, limit: int) -> list[tuple[int, int]]:
        statement = select(postings_table.c.code, postings_table.c.frame).where(
            postings_table.c.reference_id == reference_id,
        ).order_by(postings_table.c.code, postings_table.c.frame).limit(limit)
        with self.engine.connect() as connection:
            return [(code, frame) for code, frame in connection.execute(statement)]


_POSTGRES_POSTINGS = text("""
    SELECT code, array_agg(ARRAY[reference_id, CAST(frame AS text)]) AS matches
    FROM fingerprint_postings
    WHERE code = ANY(:codes) AND reference_id = ANY(:reference_ids)
    GROUP BY code
    HAVING count(*) <= :posting_limit
""").bindparams(bindparam("codes", type_=ARRAY(Integer)), bindparam("reference_ids", type_=ARRAY(String(36))))
_POSTGRES_BATCH_SIZE = 8192


class SqlPostingsIndex:
    def __init__(self, engine: Engine, reference_ids: list[str]):
        self.engine = engine
        self.reference_ids = reference_ids

    def postings(self, codes: set[int]) -> dict[int, list[Posting]]:
        if not codes or not self.reference_ids:
            return {}
        all_codes = sorted(codes)
        if self.engine.dialect.name == "postgresql":
            return self._postgres_postings(all_codes)
        result: dict[int, list[Posting]] = {}
        with self.engine.connect() as connection:
            for offset in range(0, len(all_codes), 500):
                hits = select(postings_table, func.count().over(partition_by=postings_table.c.code).label("frequency")).where(
                    postings_table.c.code.in_(all_codes[offset:offset + 500]),
                    postings_table.c.reference_id.in_(self.reference_ids),
                ).subquery()
                statement = select(hits.c.code, hits.c.reference_id, hits.c.frame).where(hits.c.frequency <= MAX_POSTINGS)
                for code, reference_id, frame in connection.execute(statement):
                    result.setdefault(code, []).append(Posting(reference_id, frame))
        return result

    def _postgres_postings(self, codes: list[int]) -> dict[int, list[Posting]]:
        result: dict[int, list[Posting]] = {}
        with self.engine.connect() as connection:
            for offset in range(0, len(codes), _POSTGRES_BATCH_SIZE):
                rows = connection.execute(_POSTGRES_POSTINGS, {
                    "codes": codes[offset:offset + _POSTGRES_BATCH_SIZE],
                    "reference_ids": self.reference_ids,
                    "posting_limit": MAX_POSTINGS,
                })
                for code, matches in rows:
                    result[code] = [Posting(reference_id, int(frame)) for reference_id, frame in matches]
        return result


def _enable_sqlite_foreign_keys(connection, _):
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
