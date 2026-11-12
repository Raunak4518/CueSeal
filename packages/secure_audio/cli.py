import argparse
from hashlib import file_digest
import json
import logging
import os
from pathlib import Path
import re
import secrets
from time import monotonic, sleep
from uuid import uuid4

from .config import Settings
from .crypto import create_keys, digest, read_json, verify_evidence
from .domain import DomainError, JobStatus, Principal, Role
from .usage import MAX_CUE_SHEET_BYTES, USAGE_REPORT_TYPE, UsageAuditInput, review_csv, verify_usage_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secure-audio")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    user = commands.add_parser("add-principal")
    user.add_argument("--tenant", required=True)
    user.add_argument("--role", choices=list(Role), type=Role, required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    verify = commands.add_parser("verify")
    verify.add_argument("evidence", type=Path)
    verify.add_argument("--public-key", type=Path, required=True)
    verify.add_argument("--media", type=Path)
    verify.add_argument("--cues", type=Path)
    verify.add_argument("--type", choices=("registration", "screening_report", USAGE_REPORT_TYPE), default="screening_report")
    audit = commands.add_parser("audit-usage")
    audit.add_argument("--media", type=Path, required=True)
    audit.add_argument("--cues", type=Path, required=True)
    audit.add_argument("--programme-id", required=True)
    audit.add_argument("--origin-ms", type=int, default=0)
    audit.add_argument("--tolerance-ms", type=int, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--review-csv", type=Path)
    experiment = commands.add_parser("benchmark")
    experiment.add_argument("--manifest", type=Path)
    experiment.add_argument("--output", type=Path, required=True)
    experiment.add_argument("--false-accept-budget", type=float, default=0.01)
    experiment.add_argument("--nmfp-model", type=Path)
    experiment.add_argument("--trusted-corpus", action="store_true", help="Decode verified local benchmark audio in process")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "verify":
            _verify(args)
        elif args.command == "benchmark":
            from .benchmark import run_benchmark

            logging.basicConfig(level=logging.INFO, format="%(message)s")
            report = run_benchmark(args.output, args.manifest, args.false_accept_budget, args.nmfp_model, args.trusted_corpus)
            print(json.dumps(report, indent=2))
        else:
            _run_service_command(args)
    except (DomainError, OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        return 130
    return 0


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _verify(args) -> None:
    if args.type == USAGE_REPORT_TYPE and (args.media is None or args.cues is None):
        raise ValueError("Usage audit verification requires --media and --cues")
    if args.type != USAGE_REPORT_TYPE and args.cues is not None:
        raise ValueError("--cues requires --type music_usage_audit")
    envelope = read_json(args.evidence.read_bytes())
    key = args.public_key.read_bytes()
    checksum = _file_sha256(args.media) if args.media else None
    result = {"verified": True, "media_checked": checksum is not None}
    if args.type == USAGE_REPORT_TYPE:
        result["payload"] = verify_usage_report(envelope, key, checksum, _file_sha256(args.cues))
        result["cues_checked"] = True
    else:
        result["payload"] = verify_evidence(envelope, key, args.type, checksum)
    print(json.dumps(result, indent=2))


def _audit_usage(args, settings, repository) -> None:
    token = os.environ.get("SECURE_AUDIO_TOKEN", "")
    actor = repository.principal(digest(token.encode())) if 32 <= len(token) <= 128 else None
    if actor is None or actor.role != Role.UPLOADER:
        raise ValueError("Set SECURE_AUDIO_TOKEN to an uploader token")

    review_path = args.review_csv or args.output.with_suffix(".csv")
    if args.output.resolve() == review_path.resolve():
        raise ValueError("Use distinct paths for signed JSON and review CSV")
    if args.output.exists() or review_path.exists():
        raise ValueError("Audit output paths must be new files")

    service = settings.service(repository)
    with args.media.open("rb") as stream:
        media = stream.read(service.max_bytes + 1)
    with args.cues.open("rb") as stream:
        audit = UsageAuditInput.from_csv(stream.read(MAX_CUE_SHEET_BYTES + 1), args.programme_id,
                                         args.origin_ms, args.tolerance_ms)
    worker = settings.worker(repository)
    job_id = service.audit_usage(actor, media, audit)["id"]
    worker.run_one(job_id=job_id)
    deadline = monotonic() + 180
    job = service.read_job(actor, job_id)
    while job["status"] in (JobStatus.QUEUED, JobStatus.PROCESSING) and monotonic() < deadline:
        sleep(0.25)
        job = service.read_job(actor, job_id)
    if job["status"] != JobStatus.COMPLETED:
        raise ValueError(f"Audit {job_id} {job['status']}: {job['error'] or 'result is not ready'}")

    envelope = job["result"]["report"]
    payload = verify_usage_report(envelope, service.signer.public_key, digest(media), audit.cue_sheet_sha256)
    if payload.get("report_id") != job_id or payload.get("tenant_id") != actor.tenant_id:
        raise ValueError("Signed report does not belong to this job")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(envelope, indent=2) + "\n")
    with review_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(review_csv(payload["findings"]))
    print(json.dumps({"report": str(args.output), "review_csv": str(review_path), "findings": len(payload["findings"])}))


def _run_service_command(args) -> None:
    settings = Settings.load()
    repository = settings.repository()
    if args.command == "init":
        if not all((settings.key_dir / name).is_file() for name in ("signing.key", "signing.pub", "media.key")):
            create_keys(settings.key_dir)
        repository.initialize()
        print(f"Initialized {settings.data_dir}")
    elif args.command == "audit-usage":
        _audit_usage(args, settings, repository)
    elif args.command == "add-principal":
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", args.tenant):
            raise ValueError("Invalid tenant identifier")
        token = secrets.token_urlsafe(32)
        principal = Principal(str(uuid4()), args.tenant, args.role)
        repository.add_principal(principal, digest(token.encode("ascii")))
        print(json.dumps({"id": principal.id, "tenant": principal.tenant_id, "role": principal.role, "token": token}))
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("secure_audio.api:create_app", factory=True, host="127.0.0.1", port=args.port)
    elif args.command == "worker":
        logging.basicConfig(level=logging.INFO)
        if args.once:
            settings.worker(repository).run_one()
            return
        with settings.worker_session(repository) as worker:
            while True:
                if not worker.run_one():
                    sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
