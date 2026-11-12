import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import random
import re
from time import perf_counter_ns
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from secure_audio.decoder import _decode
from secure_audio.fingerprint import FINGERPRINT_VERSION, HOP, decide, fingerprint, retrieve_landmarks
from secure_audio.repository import SqlRepository, jobs, postings_table, principals, registrations, tenants
from secure_audio.scoring import LogisticModel
from secure_audio.song_describer import _reference_audio


def recording_id(track_id):
    return str(uuid5(NAMESPACE_URL, f"song-describer:{track_id}"))


def decode(path, max_seconds):
    return np.frombuffer(_decode(path.read_bytes(), max_seconds), dtype="<f4")


def catalog_sources(prepared, manifest):
    provenance = json.loads((prepared / "provenance.json").read_text(encoding="utf-8"))
    test_references = {item["id"]: item for item in manifest["test"]["references"]}
    return {
        track_id: (recording_id(track_id), source, test_references.get(track_id))
        for track_id, source in sorted(provenance.items())
        if source["split"] != "test" or track_id in test_references
    }


def load_catalog(repository, sources, prepared, recordings):
    with repository.engine.begin() as connection:
        connection.execute(insert(tenants), {"id": "benchmark"})
        connection.execute(insert(principals), {
            "id": "benchmark-contributor", "tenant_id": "benchmark", "role": "contributor", "token_digest": "unused",
        })
        job_rows = [
            {"id": reference_id, "tenant_id": "benchmark", "owner_id": "benchmark-contributor", "kind": "registration",
             "status": "completed", "context": {}, "media_sha256": source["source_sha256"], "created_at": 0,
             "expires_at": 0, "attempts": 1}
            for reference_id, source, _ in sources.values()
        ]
        connection.execute(insert(jobs), job_rows)
        connection.execute(insert(registrations), [
            {"id": reference_id, "tenant_id": "benchmark", "contributor_id": "benchmark-contributor",
             "media_sha256": source["source_sha256"], "fingerprint_version": FINGERPRINT_VERSION, "details": {}}
            for reference_id, source, _ in sources.values()
        ])
        with connection.connection.driver_connection.cursor() as cursor:
            with cursor.copy("COPY fingerprint_postings (reference_id, code, frame) FROM STDIN") as copy:
                for number, (track_id, (reference_id, source, test_reference)) in enumerate(sources.items(), 1):
                    if test_reference:
                        samples = decode(prepared / test_reference["path"], 180)
                    else:
                        raw = (recordings / f"{track_id}.mp3").read_bytes()
                        if sha256(raw).hexdigest() != source["source_sha256"]:
                            raise ValueError(f"Source digest mismatch for {track_id}")
                        samples = _reference_audio(raw)
                    copy.write("".join(f"{reference_id}\t{item.code}\t{item.frame}\n" for item in fingerprint(samples)))
                    if number % 25 == 0 or number == len(sources):
                        print(f"Loaded {number}/{len(sources)} references", flush=True)
    with repository.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("VACUUM (ANALYZE) fingerprint_postings")


class TimedIndex:
    def __init__(self, index):
        self.index = index
        self.lookup_ms = 0.0
        self.posting_count = 0

    def postings(self, codes):
        start = perf_counter_ns()
        postings = self.index.postings(codes)
        self.lookup_ms = (perf_counter_ns() - start) / 1_000_000
        self.posting_count = sum(map(len, postings.values()))
        return postings


def run_query(index, prepared, query, model):
    media = decode(prepared / query["path"], 60)
    started = perf_counter_ns()
    landmarks = fingerprint(media)
    extracted = perf_counter_ns()
    timed = TimedIndex(index)
    candidates = retrieve_landmarks(landmarks, media.size // HOP, timed)
    ranked = perf_counter_ns()
    decision = decide(candidates, model)
    finished = perf_counter_ns()
    accepted = sorted({segment.reference_id for segment in decision.segments}) if decision.status == "match" else []
    evidence = json.dumps([asdict(decision), [[asdict(candidate) for candidate in window] for window in candidates]],
                          sort_keys=True, separators=(",", ":"))
    return {
        "query": query["path"], "condition": query["condition"],
        "expected_reference": recording_id(query["reference_id"]) if query.get("reference_id") else None,
        "accepted_references": accepted, "recognition": decision.status, "evidence_sha256": sha256(evidence.encode()).hexdigest(),
        "distinct_hashes": len({item.code for item in landmarks}), "postings_returned": timed.posting_count,
        "fingerprint_ms": (extracted - started) / 1_000_000, "lookup_ms": timed.lookup_ms,
        "voting_ms": (ranked - extracted) / 1_000_000 - timed.lookup_ms, "verification_ms": (finished - ranked) / 1_000_000,
        "matching_ms": (finished - started) / 1_000_000,
    }


def measure(index, prepared, queries, model):
    for query in queries[:5]:
        run_query(index, prepared, query, model)
    rows = []
    for number, query in enumerate(queries, 1):
        rows.append(run_query(index, prepared, query, model))
        if number % 12 == 0 or number == len(queries):
            print(f"Measured {number}/{len(queries)} queries", flush=True)
    return rows


def sample_queries(manifest, per_condition):
    rng = random.Random(862309)
    all_queries = manifest["test"]["queries"]
    conditions = sorted({query["condition"] for query in all_queries})
    selected = []
    for condition in conditions:
        candidates = [query for query in all_queries if query["condition"] == condition]
        count = per_condition * 4 if condition == "unknown_music" else per_condition
        selected.extend(rng.sample(candidates, min(count, len(candidates))))
    rng.shuffle(selected)
    return selected


def lookup_plans(repository, index, prepared, query):
    media = decode(prepared / query["path"], 60)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(repository.engine, "before_cursor_execute", capture)
    try:
        index.postings({item.code for item in fingerprint(media)})
    finally:
        event.remove(repository.engine, "before_cursor_execute", capture)
    with repository.engine.connect() as connection:
        plans = [connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters).scalar_one()[0]
                 for statement, parameters in statements]
    return {"query": query["path"], "statements": len(plans), "execution_ms_total": sum(plan["Execution Time"] for plan in plans),
            "slowest_statement": max(plans, key=lambda plan: plan["Execution Time"])}


def report_rows(rows):
    times = {}
    for name in ("fingerprint", "lookup", "voting", "verification", "matching"):
        median, tail = np.percentile([row[f"{name}_ms"] for row in rows], [50, 95])
        times[name] = {"p50_ms": round(float(median), 3), "p95_ms": round(float(tail), 3)}
    known = [row for row in rows if row["expected_reference"]]
    unknown = [row for row in rows if not row["expected_reference"]]
    return {
        "timings": times, "known_queries": len(known), "unknown_queries": len(unknown),
        "correct_accepts": sum(row["accepted_references"] == [row["expected_reference"]] for row in known),
        "false_accepts": sum(bool(row["accepted_references"]) for row in unknown),
        "wrong_reference_accepts": sum(bool(set(row["accepted_references"]) - {row["expected_reference"]}) for row in known),
        "reviews": sum(row["recognition"] == "ambiguous" for row in rows),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--recordings", type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--per-condition", default=12, type=int, choices=range(1, 37), metavar="1..36")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", args.schema):
        parser.error("Choose a lowercase PostgreSQL schema identifier of at most 63 characters")
    database_url = os.environ.get("SECURE_AUDIO_DATABASE_URL")
    if not database_url:
        parser.error("Set SECURE_AUDIO_DATABASE_URL to the benchmark database")
    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg":
        parser.error("Use a PostgreSQL database with the psycopg driver")
    manifest = json.loads((args.prepared / "manifest.json").read_text(encoding="utf-8"))
    sources = catalog_sources(args.prepared, manifest)
    model = LogisticModel.from_path(args.model)
    if args.recordings:
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(CreateSchema(args.schema))
        engine.dispose()
    repository = SqlRepository(url.update_query_dict({"options": f"-csearch_path={args.schema}"}))
    if args.recordings:
        repository.initialize()
        load_catalog(repository, sources, args.prepared, args.recordings)
    with repository.engine.connect() as connection:
        server = connection.exec_driver_sql("SHOW server_version").scalar_one()
        catalog_ids = sorted(connection.scalars(select(registrations.c.id)))
        row_count = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {postings_table.name}").scalar_one()
    if catalog_ids != sorted(item[0] for item in sources.values()):
        raise ValueError("Benchmark schema does not match the prepared corpus")
    index = repository.index(catalog_ids)
    queries = sample_queries(manifest, args.per_condition)
    rows = measure(index, args.prepared, queries, model)
    representative = max(rows, key=lambda row: row["distinct_hashes"])["query"]
    example = next(query for query in queries if query["path"] == representative)
    report = {
        "postgresql": server, "host": platform.platform(), "python": platform.python_version(),
        "references": len(catalog_ids), "catalog_postings": row_count, "warmup_queries": 5,
        "model": model.version, "threshold": model.threshold, "review_threshold": model.review_threshold,
        **report_rows(rows), "representative_lookup": lookup_plans(repository, index, args.prepared, example),
        "queries": rows,
    }
    if args.before:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        keys = ("query", "evidence_sha256", "postings_returned")
        if (before["model"], before["references"], before["catalog_postings"]) != (model.version, len(catalog_ids), row_count):
            raise ValueError("Baseline model or catalog differs")
        if [[row[key] for key in keys] for row in before["queries"]] != [[row[key] for key in keys] for row in rows]:
            raise ValueError("Candidate evidence or decisions differ from baseline")
        report["identical_candidates_and_decisions"] = True
        report["p95_speedup"] = {name: round(before["timings"][name]["p95_ms"] / stats["p95_ms"], 3)
                                 for name, stats in report["timings"].items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("references", "catalog_postings", "model", "timings")}, indent=2))
    repository.engine.dispose()


if __name__ == "__main__":
    main()
