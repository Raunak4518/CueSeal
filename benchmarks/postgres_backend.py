import argparse
from contextlib import ExitStack, contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import socket
from threading import Thread
from time import monotonic, perf_counter_ns, sleep
from unittest.mock import patch
from uuid import uuid4

import httpx
import numpy as np
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema
import uvicorn

from benchmarks.postgres_lookup import recording_id, sample_queries
from secure_audio import api, catalog, fingerprint, worker
from secure_audio.catalog import FingerprintBinding, MAX_CATALOG_POSTINGS, VerifiedPostingsIndex, indexing_identity
from secure_audio.config import Settings
from secure_audio.crypto import canonical, create_keys, digest, verify_evidence
from secure_audio.domain import Principal, Role
from secure_audio.repository import jobs, postings_table, registrations


TENANT = "benchmark"
UPLOAD_METADATA = json.dumps({"intended_use": "research"})


def scoped_url(url, schema):
    return url.update_query_dict({"options": f"-csearch_path={schema}"})


def add_actor(repository, role):
    actor = Principal(str(uuid4()), TENANT, role)
    token = secrets.token_urlsafe(32)
    repository.add_principal(actor, digest(token.encode()))
    return actor, {"Authorization": f"Bearer {token}"}


def prepare(settings, base_url, schema, source_schema):
    if schema == source_schema:
        raise ValueError("Choose a separate schema for the full backend benchmark")
    admin = create_engine(base_url, hide_parameters=True)
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
    finally:
        admin.dispose()
    create_keys(settings.key_dir)
    repository = settings.repository()
    try:
        repository.initialize()
        with repository.engine.begin() as connection:
            connection.exec_driver_sql(f"""
                INSERT INTO tenants SELECT * FROM {source_schema}.tenants WHERE id = 'benchmark'
            """)
            connection.exec_driver_sql(f"""
                INSERT INTO principals SELECT * FROM {source_schema}.principals
                WHERE tenant_id = 'benchmark' AND role = 'contributor'
            """)
            connection.exec_driver_sql(f"""
                INSERT INTO jobs SELECT * FROM {source_schema}.jobs
                WHERE tenant_id = 'benchmark' AND kind = 'registration' AND status = 'completed'
            """)
            connection.exec_driver_sql(f"""
                INSERT INTO registrations
                    (id, tenant_id, contributor_id, media_sha256, fingerprint_version, details)
                SELECT id, tenant_id, contributor_id, media_sha256, fingerprint_version,
                       json_build_object('title', 'Song Describer ' || id, 'source', 'Licensed benchmark',
                         'policy', json_build_object('permitted_uses', json_build_array('research'),
                             'restricted_uses', json_build_array(), 'valid_from', NULL, 'valid_until', NULL))
                FROM {source_schema}.registrations WHERE tenant_id = 'benchmark'
            """)
            print("Copying the indexed reference fingerprints", flush=True)
            connection.exec_driver_sql(f"""
                INSERT INTO fingerprint_postings
                SELECT p.* FROM {source_schema}.fingerprint_postings AS p
                JOIN registrations AS r ON r.id = p.reference_id
            """)
        with repository.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql("VACUUM (ANALYZE) fingerprint_postings")
        reviewer, _ = add_actor(repository, Role.REVIEWER)
        service = settings.service(repository)
        with repository.engine.connect() as connection:
            references = list(connection.execute(select(registrations).order_by(registrations.c.id)).mappings())
        for reference in references:
            reference_id = reference["id"]
            identity = indexing_identity(reference_id, TENANT, reference["contributor_id"], reference["media_sha256"],
                                         reference["details"])
            binding = FingerprintBinding.from_pairs(repository.reference_postings(reference_id, MAX_CATALOG_POSTINGS + 1))
            attestation = service.signer.sign("reference_fingerprint", {**identity, "fingerprint": binding.payload()})
            with repository.engine.begin() as connection:
                connection.execute(update(jobs).where(jobs.c.id == reference_id).values(result={"fingerprint_attestation": attestation}))
            service.approve(reviewer, reference_id)
        print(f"Approved and signed {len(references)} catalog registrations", flush=True)
    finally:
        repository.engine.dispose()


class Timings:
    def __init__(self):
        self.milliseconds = {}
        self.pcm_digest = None

    def call(self, name, function, *args, **kwargs):
        started = perf_counter_ns()
        try:
            result = function(*args, **kwargs)
        finally:
            self.milliseconds[name] = self.milliseconds.get(name, 0.0) + (perf_counter_ns() - started) / 1_000_000
        if name == "decode":
            self.pcm_digest = digest(memoryview(result).cast("B"))
        return result

    def attach(self, stack, target, attribute, name):
        method = getattr(target, attribute)

        @wraps(method)
        def measured(*args, **kwargs):
            return self.call(name, method, *args, **kwargs)

        stack.enter_context(patch.object(target, attribute, measured))


@contextmanager
def http_backend(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 15
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.01)
            if not server.started:
                raise RuntimeError("Benchmark HTTP server did not start")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(timeout=15)


def run_query(client, headers, runner, public_key, prepared, query, timing):
    media_path = prepared / query["path"]
    media = media_path.read_bytes()
    timing.milliseconds.clear()
    timing.pcm_digest = None
    started = perf_counter_ns()
    response = timing.call("upload_http", client.post, "/v1/screenings", headers=headers,
                           files={"audio": (media_path.name, media), "metadata": (None, UPLOAD_METADATA)})
    if response.status_code != 202:
        raise RuntimeError(f"Upload failed with {response.status_code}: {response.text}")
    job_id = response.json()["id"]
    if not timing.call("worker", runner.run_one):
        raise RuntimeError(f"Worker failed to claim {job_id}")
    response = timing.call("fetch_http", client.get, f"/v1/jobs/{job_id}", headers=headers)
    received = perf_counter_ns()
    response.raise_for_status()
    job = response.json()
    if job["status"] != "completed":
        raise RuntimeError(f"Screening failed: {job}")
    report = job["result"]["report"]
    payload = timing.call("verify_report", verify_evidence, report, public_key, "screening_report", digest(media))
    verified = perf_counter_ns()
    if payload["report_id"] != job_id or payload["scoring_version"] != runner.scorer.version:
        raise ValueError("Signed report does not match the benchmark job and model")
    stable_payload = {key: value for key, value in payload.items() if key not in ("report_id", "evaluated_at", "request_sha256")}
    matches = sorted({segment["reference_id"] for segment in payload["segments"]}) if payload["recognition"] == "match" else []
    stages = dict(timing.milliseconds)
    stages["voting"] = stages["retrieve"] - stages["lookup"] - stages["fingerprint"]
    stages["upload_to_report"] = (received - started) / 1_000_000
    stages["upload_to_verified_report"] = (verified - started) / 1_000_000
    return {
        "query": query["path"], "condition": query["condition"], "media_bytes": len(media),
        "expected_reference": recording_id(query["reference_id"]) if query.get("reference_id") else None,
        "recognition": payload["recognition"], "matched_references": matches,
        "decoded_pcm_sha256": timing.pcm_digest, "stable_report_sha256": digest(canonical(stable_payload)),
        "milliseconds": stages,
    }


def measure(settings, prepared, queries):
    api_repository = settings.repository()
    worker_repository = settings.repository()
    try:
        service = settings.service(api_repository)
        _, headers = add_actor(api_repository, Role.UPLOADER)
        timing = Timings()
        app = api.create_app(api_repository, service)
        with ExitStack() as stack:
            runner = stack.enter_context(settings.worker_session(worker_repository))
            for target, attribute, name in (
                (api_repository, "principal", "http_auth"),
                (api_repository, "submit", "queue_write"),
                (api_repository, "job", "report_read"),
                (service.cipher, "encrypt", "upload_encryption"),
                (worker_repository, "claim", "claim"),
                (worker_repository, "catalog", "catalog_read"),
                (worker_repository, "complete", "report_write"),
                (runner.cipher, "decrypt", "decryption"),
                (runner.decoder, "decode", "decode"),
                (runner.signer, "sign", "sign_report"),
                (catalog, "verify_evidence", "verify_catalog_signatures"),
                (runner.catalog_verifier, "load", "verified_catalog"),
                (worker, "retrieve", "retrieve"),
                (worker, "decide", "logistic"),
                (fingerprint, "fingerprint", "fingerprint"),
                (VerifiedPostingsIndex, "postings", "lookup"),
            ):
                timing.attach(stack, target, attribute, name)
            client = stack.enter_context(http_backend(app))
            public_key = runner.signer.public_key
            for query in queries[:5]:
                run_query(client, headers, runner, public_key, prepared, query, timing)
            rows = []
            for number, query in enumerate(queries, 1):
                row = run_query(client, headers, runner, public_key, prepared, query, timing)
                rows.append(row)
                if number % 8 == 0 or number == len(queries):
                    times = summarize(rows)["timings"]
                    stages = ("upload_to_report", "decode", "lookup", "voting")
                    print(f"Measured {number}/{len(queries)} uploads",
                          json.dumps({name: times[name]["p95_ms"] for name in stages}), flush=True)
        return runner.scorer.version, rows
    finally:
        worker_repository.engine.dispose()
        api_repository.engine.dispose()


def summarize(rows):
    timings = {}
    for name in rows[0]["milliseconds"]:
        p50, p95 = np.percentile([row["milliseconds"][name] for row in rows], [50, 95])
        timings[name] = {"p50_ms": round(float(p50), 3), "p95_ms": round(float(p95), 3)}
    known = [row for row in rows if row["expected_reference"]]
    unknown = [row for row in rows if not row["expected_reference"]]
    return {
        "timings": timings, "known_queries": len(known), "unknown_queries": len(unknown),
        "correct_matches": sum(row["matched_references"] == [row["expected_reference"]] for row in known),
        "false_matches": sum(bool(row["matched_references"]) for row in unknown),
        "wrong_reference_matches": sum(bool(set(row["matched_references"]) - {row["expected_reference"]}) for row in known),
        "reviews": sum(row["recognition"] == "ambiguous" for row in rows),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--source-schema")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--per-condition", default=12, type=int, choices=range(1, 37), metavar="1..36")
    args = parser.parse_args()
    for schema in (args.schema, args.source_schema):
        if schema and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            parser.error("Choose lowercase PostgreSQL schema identifiers of at most 63 characters")
    database_url = os.environ.get("SECURE_AUDIO_DATABASE_URL")
    if not database_url or make_url(database_url).drivername != "postgresql+psycopg":
        parser.error("Set SECURE_AUDIO_DATABASE_URL to a disposable PostgreSQL database using psycopg")
    base_url = make_url(database_url)
    settings = Settings(args.data_dir, scoped_url(base_url, args.schema), args.model)
    if args.source_schema:
        prepare(settings, base_url, args.schema, args.source_schema)
        return
    if not args.prepared or not args.output:
        parser.error("Pass --prepared and --output to run the benchmark")
    repository = settings.repository()
    try:
        references = len(repository.catalog(TENANT))
        with repository.engine.connect() as connection:
            server = connection.exec_driver_sql("SHOW server_version").scalar_one()
            posting_count = connection.exec_driver_sql(f"SELECT count(*) FROM {postings_table.name}").scalar_one()
    finally:
        repository.engine.dispose()
    manifest = json.loads((args.prepared / "manifest.json").read_text(encoding="utf-8"))
    queries = sample_queries(manifest, args.per_condition)
    model, rows = measure(settings, args.prepared, queries)
    report = {"postgresql": server, "references": references, "catalog_postings": posting_count, "model": model,
              "warmup_uploads": 5, "worker_dispatch": "immediate after HTTP 202", **summarize(rows), "queries": rows}
    if args.before:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        comparable = ("postgresql", "references", "catalog_postings", "model")
        if any(before[key] != report[key] for key in comparable):
            raise ValueError("Baseline server, catalog, or model differs")
        keys = ("query", "decoded_pcm_sha256", "stable_report_sha256")
        if [[row[key] for key in keys] for row in before["queries"]] != [[row[key] for key in keys] for row in rows]:
            raise ValueError("Decoded samples or verified report content differs from baseline")
        report["identical_pcm_and_verified_report_content"] = True
        report["p95_speedup"] = {name: round(before["timings"][name]["p95_ms"] / stats["p95_ms"], 3)
                                 for name, stats in report["timings"].items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "queries"}, indent=2))


if __name__ == "__main__":
    main()
