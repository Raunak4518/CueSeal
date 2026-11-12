from copy import deepcopy
import csv
from dataclasses import replace
from datetime import UTC, datetime
import io
import json
import os
import secrets
import subprocess
import sys
from time import monotonic, time
from uuid import uuid4

import av
from fastapi.testclient import TestClient
import numpy as np
import pytest
from scipy.io.wavfile import write
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from secure_audio.api import create_app
from secure_audio.benchmark import demo_signal, run_benchmark
from secure_audio.config import Settings
from secure_audio.crypto import EvidenceError, create_keys, digest, verify_evidence
from secure_audio.decoder import DecodeError, SAMPLE_RATE, SubprocessDecoder
from secure_audio.domain import PolicyOutcome, Principal, Role, UsagePolicy, evaluate_policy
from secure_audio.fingerprint import MAX_POSTINGS, Landmark, MemoryIndex, Posting, decide, fingerprint, retrieve
from secure_audio.repository import JobRequest, QueuedJob, jobs, postings_table
from secure_audio.scoring import LogisticModel, RuleScorer
from secure_audio.usage import UsageAuditInput


def wav(samples) -> bytes:
    stream = io.BytesIO()
    write(stream, SAMPLE_RATE, np.asarray(samples, dtype=np.float32))
    return stream.getvalue()


def mp3(samples) -> bytes:
    stream = io.BytesIO()
    with av.open(stream, mode="w", format="mp3") as output:
        encoder = output.add_stream("libmp3lame", rate=SAMPLE_RATE)
        encoder.layout = "mono"
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="fltp", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        for packet in encoder.encode(frame) + encoder.encode(None):
            output.mux(packet)
    return stream.getvalue()


def provision(repository, tenant, role):
    token = secrets.token_urlsafe(32)
    principal = Principal(str(uuid4()), tenant, role)
    repository.add_principal(principal, digest(token.encode()))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_AUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SECURE_AUDIO_DATABASE_URL", raising=False)
    monkeypatch.delenv("SECURE_AUDIO_MODEL", raising=False)
    settings = Settings.load()
    test_database_url = os.environ.get("SECURE_AUDIO_TEST_DATABASE_URL")
    admin = None
    if test_database_url:
        namespace = f"test_{uuid4().hex}"
        url = make_url(test_database_url)
        admin = create_engine(url, hide_parameters=True)
        with admin.begin() as connection:
            connection.execute(CreateSchema(namespace))
        settings = replace(settings, database_url=url.update_query_dict({"options": f"-csearch_path={namespace}"}))
    create_keys(settings.key_dir)
    repository = settings.repository()
    try:
        repository.initialize()
        worker = settings.worker(repository)
        service = settings.service(repository)
        with TestClient(create_app(repository, service)) as client:
            yield client, worker, repository, settings, service
    finally:
        repository.engine.dispose()
        if admin:
            with admin.begin() as connection:
                connection.execute(DropSchema(namespace, cascade=True))
            admin.dispose()


def submit(client, route, actor, media, metadata):
    result = client.post(route, headers=actor, files={"audio": ("clip.wav", media), "metadata": (None, json.dumps(metadata))})
    assert result.status_code == 202, result.text
    return result.json()["id"]


def report_for(client, actor, job_id):
    result = client.get(f"/v1/jobs/{job_id}", headers=actor)
    assert result.status_code == 200
    assert result.json()["status"] == "completed", result.text
    return result.json()["result"]["report"]


def test_usage_audit_cli_with_repeated_overlapping_and_tampered_cues(system, tmp_path, monkeypatch):
    client, worker, repository, settings, _ = system
    contributor = provision(repository, "studio", Role.CONTRIBUTOR)
    reviewer = provision(repository, "studio", Role.REVIEWER)
    uploader = provision(repository, "studio", Role.UPLOADER)
    tracks = [demo_signal(seed, 14) for seed in (41, 76, 123)]
    references = []
    for i, track in enumerate(tracks):
        registered = submit(client, "/v1/registrations", contributor, wav(track),
                            {"title": f"Generated track {i}", "source": "Project-owned test fixture", "policy": {}})
        assert worker.run_one()
        assert client.post(f"/v1/registrations/{registered}/approve", headers=reviewer).status_code == 200
        references.append(registered)

    first, second, absent = references
    samples = np.zeros(SAMPLE_RATE * 53, dtype=np.float32)
    for start, track in ((3, tracks[0]), (17, tracks[0]), (28, tracks[1])):
        samples[start * SAMPLE_RATE:(start + 8) * SAMPLE_RATE] = track[2 * SAMPLE_RATE:10 * SAMPLE_RATE]
    samples[40 * SAMPLE_RATE:47 * SAMPLE_RATE] = (
        tracks[0][SAMPLE_RATE:8 * SAMPLE_RATE] + tracks[1][SAMPLE_RATE:8 * SAMPLE_RATE]
    ) * 0.7
    audio = tmp_path / "broadcast.wav"
    audio.write_bytes(wav(samples))
    cues = tmp_path / "declared.csv"
    cue_bytes = (
        f"cue_id,recording_id,start_ms,end_ms\n"
        f"first,{first},123000,131000\n"
        f"duplicate,{first},123000,131000\n"
        f"repeat,{first},137000,147500\n"
        f"overlap-a,{first},160000,167000\n"
        f"overlap-b,{second},160000,167000\n"
        f"absent,{absent},169000,171000\n"
        f"unknown,outside-catalog,170000,172000\n"
        f"outside,{first},115000,119000\n"
    ).encode()
    cues.write_bytes(cue_bytes)
    earlier = submit(client, "/v1/screenings", uploader, wav(tracks[0]), {"intended_use": "review"})
    monkeypatch.setenv("SECURE_AUDIO_DATABASE_URL", settings.database_url.render_as_string(hide_password=False))
    monkeypatch.setenv("SECURE_AUDIO_TOKEN", uploader["Authorization"].split()[1])
    report = tmp_path / "audit.json"

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "secure_audio.cli", *map(str, args)],
                              capture_output=True, text=True, timeout=60)

    outcome = cli("audit-usage", "--media", audio, "--cues", cues, "--programme-id", "fixture-show",
                  "--origin-ms", 120000, "--tolerance-ms", 600, "--output", report)
    assert outcome.returncode == 0, outcome.stderr
    assert repository.job("studio", earlier)["status"] == "queued"

    def verify(evidence=report, media=audio, declared=cues):
        return cli("verify", evidence, "--type", "music_usage_audit", "--public-key",
                   settings.key_dir / "signing.pub", "--media", media, "--cues", declared)

    verified = verify()
    assert verified.returncode == 0, verified.stderr
    payload = json.loads(verified.stdout)["payload"]
    assert (payload["origin_ms"], payload["duration_ms"]) == (120000, 53000)
    assert payload["programme_id"] == "fixture-show"
    declared = {row["cue_id"]: row for row in payload["findings"] if row["cue_id"] is not None}
    assert {declared[name]["status"] for name in ("first", "duplicate")} == {"confirmed", "needs_review"}
    assert "overlapping_declaration" in {declared[name]["reason"] for name in ("first", "duplicate")}
    assert declared["repeat"]["status"] == "timing_mismatch"
    assert 137000 <= declared["repeat"]["detected_start_ms"] < 137600
    assert declared["overlap-a"]["reason"] == declared["overlap-b"]["reason"] == "ambiguous_match"
    assert declared["absent"]["status"] == "declared_without_evidence"
    assert declared["unknown"]["reason"] == "recording_not_in_catalog"
    assert declared["outside"]["reason"] == "outside_excerpt"
    assert any(row["status"] == "detected_unlisted" and row["recording_id"] == second
               and 148000 <= row["detected_start_ms"] < 148600 for row in payload["findings"])
    with report.with_suffix(".csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["status"] for row in rows] == [row["status"] for row in payload["findings"]]

    altered_cues = tmp_path / "altered-cues.csv"
    altered_cues.write_bytes(cue_bytes.replace(b"147500", b"147501"))
    altered_media = tmp_path / "altered-media.wav"
    altered_media.write_bytes(audio.read_bytes() + b"\x00")
    altered_report = tmp_path / "altered.json"
    envelope = json.loads(report.read_bytes())
    envelope["payload"]["findings"][0]["status"] = "detected_unlisted"
    altered_report.write_text(json.dumps(envelope), encoding="utf-8")
    for result, expected in ((verify(declared=altered_cues), "Cue sheet digest"),
                             (verify(media=altered_media), "Media digest"),
                             (verify(evidence=altered_report), "Invalid evidence signature")):
        assert result.returncode == 1
        assert expected in result.stderr


def test_usage_cue_sheet_rejects_duplicate_ids_and_invalid_intervals():
    header = b"cue_id,recording_id,start_ms,end_ms\n"
    for data, error in ((b"cue,track,10,20\ncue,track,30,40\n", "Duplicate cue ID"),
                        (b"cue,track,20,10\n", "Cue end must be after its start"),
                        (b"cue,track,1.5,20\n", "whole milliseconds")):
        with pytest.raises(ValueError, match=error):
            UsageAuditInput.from_csv(header + data, "fixture-show", 0, 600)


def test_registration_screening_and_offline_verification(system):
    client, worker, repository, settings, _ = system
    contributor = provision(repository, "studio", Role.CONTRIBUTOR)
    reviewer = provision(repository, "studio", Role.REVIEWER)
    uploader = provision(repository, "studio", Role.UPLOADER)
    other = provision(repository, "other", Role.REVIEWER)
    another_uploader = provision(repository, "studio", Role.UPLOADER)
    track = demo_signal(41)
    reference = wav(track)
    details = {"title": "Studio recording", "source": "Team session", "policy": {"permitted_uses": ["campus"], "restricted_uses": ["ads"]}}
    registration_id = submit(client, "/v1/registrations", contributor, reference, details)
    assert worker.run_one()
    registration_url = f"/v1/registrations/{registration_id}"
    assert client.get(registration_url, headers=contributor).json()["status"] == "pending"
    assert client.post(f"{registration_url}/approve", headers=contributor).status_code == 403
    assert client.post(f"{registration_url}/approve", headers=other).status_code == 404
    assert client.get(registration_url, headers=other).status_code == 404
    assert client.get(registration_url, headers=uploader).status_code == 404

    clip = track[SAMPLE_RATE * 2:SAMPLE_RATE * 12].copy()
    clip += np.random.default_rng(42).normal(0, 0.025, clip.size)
    media = mp3(clip)
    pending_job = submit(client, "/v1/screenings", uploader, media, {"intended_use": "ads"})
    assert worker.run_one()
    assert report_for(client, uploader, pending_job)["payload"]["recognition"] == "no_match"

    approval = client.post(f"{registration_url}/approve", headers=reviewer)
    assert approval.status_code == 200
    assert client.post(f"{registration_url}/approve", headers=reviewer).json() == approval.json()
    assert client.get(registration_url, headers=contributor).json()["status"] == "approved"
    public_key = (settings.key_dir / "signing.pub").read_bytes()
    verify_evidence(approval.json(), public_key, "registration", digest(reference))

    job_id = submit(client, "/v1/screenings", uploader, media, {"intended_use": "ads"})
    assert worker.run_one()
    assert client.get(f"/v1/jobs/{job_id}", headers=other).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}", headers=another_uploader).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}").status_code == 401
    signed = report_for(client, reviewer, job_id)
    payload = verify_evidence(signed, public_key, "screening_report", digest(media))
    assert payload["recognition"] == "match"
    assert payload["policy_outcome"] == "restricted"
    assert payload["segments"][0]["reference_id"] == registration_id
    assert abs(payload["segments"][0]["reference_start_ms"] - 2000) < 400
    assert payload["segments"][0]["query_end_ms"] > 9000
    altered = deepcopy(signed)
    altered["payload"]["segments"][0]["query_end_ms"] -= 2000
    with pytest.raises(EvidenceError, match="signature"):
        verify_evidence(altered, public_key, "screening_report", digest(media))
    with pytest.raises(EvidenceError, match="digest"):
        verify_evidence(signed, public_key, "screening_report", digest(reference))
    with repository.engine.connect() as connection:
        assert connection.scalar(select(jobs.c.ciphertext).where(jobs.c.id == job_id)) is None


def test_queued_intended_use_tampering_fails_before_decoding(system, monkeypatch):
    client, worker, repository, settings, _ = system
    contributor = provision(repository, "studio", Role.CONTRIBUTOR)
    reviewer = provision(repository, "studio", Role.REVIEWER)
    uploader = provision(repository, "studio", Role.UPLOADER)
    media = wav(demo_signal(41))
    details = {"title": "Studio recording", "source": "Team session",
               "policy": {"permitted_uses": ["campus"], "restricted_uses": ["ads"]}}
    reference = submit(client, "/v1/registrations", contributor, media, details)
    assert worker.run_one()
    assert client.post(f"/v1/registrations/{reference}/approve", headers=reviewer).status_code == 200
    baseline = submit(client, "/v1/screenings", uploader, media, {"intended_use": "ads"})
    assert worker.run_one()
    public_key = (settings.key_dir / "signing.pub").read_bytes()
    signed = verify_evidence(report_for(client, uploader, baseline), public_key, "screening_report", digest(media))
    assert (signed["recognition"], signed["policy_outcome"]) == ("match", "restricted")
    assert len(signed["request_sha256"]) == 64

    altered = submit(client, "/v1/screenings", uploader, media, {"intended_use": "ads"})
    with repository.engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == altered).values(context={"intended_use": "campus"}))

    def reject_decode(*args, **kwargs):
        pytest.fail("Unauthenticated request reached the decoder")

    monkeypatch.setattr(worker.decoder, "decode", reject_decode)
    assert worker.run_one()
    failed = client.get(f"/v1/jobs/{altered}", headers=uploader).json()
    assert (failed["status"], failed["result"], failed["error"]) == (
        "failed", None, "Queued request or catalog integrity verification failed",
    )
    with repository.engine.connect() as connection:
        assert connection.scalar(select(jobs.c.ciphertext).where(jobs.c.id == altered)) is None


def test_same_size_posting_swap_cannot_change_approved_match(system):
    client, worker, repository, settings, _ = system
    worker.scorer = RuleScorer(threshold=0.57, review_threshold=0.45)
    contributor = provision(repository, "studio", Role.CONTRIBUTOR)
    reviewer = provision(repository, "studio", Role.REVIEWER)
    uploader = provision(repository, "studio", Role.UPLOADER)
    track = demo_signal(41)
    unknown = demo_signal(109, 8)
    media = wav(unknown)
    registered = submit(client, "/v1/registrations", contributor, wav(track),
                        {"title": "Reference A", "source": "Studio session", "policy": {}})
    assert worker.run_one()
    public_key = (settings.key_dir / "signing.pub").read_bytes()
    originals = repository.reference_postings(registered, 1_000_000)
    original = originals[0]
    with repository.engine.begin() as connection:
        connection.execute(postings_table.delete().where(postings_table.c.reference_id == registered,
                                                         postings_table.c.code == original[0],
                                                         postings_table.c.frame == original[1]))
    approval_url = f"/v1/registrations/{registered}/approve"
    assert client.post(approval_url, headers=reviewer).status_code == 409
    with repository.engine.begin() as connection:
        connection.execute(postings_table.insert().values(reference_id=registered, code=original[0], frame=original[1]))
    approved = client.post(approval_url, headers=reviewer)
    assert approved.status_code == 200
    manifest = verify_evidence(approved.json(), public_key, "registration", digest(wav(track)))
    assert manifest["fingerprint"]["count"] == len(originals)

    def screen(runner):
        job_id = submit(client, "/v1/screenings", uploader, media, {"intended_use": "ads"})
        assert runner.run_one()
        return client.get(f"/v1/jobs/{job_id}", headers=uploader).json()

    baseline = verify_evidence(screen(worker)["result"]["report"], public_key, "screening_report", digest(media))
    assert baseline["recognition"] == "no_match"
    replacement = list(dict.fromkeys([(item.code, item.frame) for item in fingerprint(unknown)] + originals))[:len(originals)]
    assert len(replacement) == len(originals) and set(replacement) != set(originals)
    with repository.engine.begin() as connection:
        connection.execute(postings_table.delete().where(postings_table.c.reference_id == registered))
        connection.execute(postings_table.insert(), [{"reference_id": registered, "code": code, "frame": frame}
                                                      for code, frame in replacement])
    unverified = decide(retrieve(unknown, repository.index([registered])), worker.scorer)
    assert unverified.status == "match" and unverified.segments[0].reference_id == registered
    warm = verify_evidence(screen(worker)["result"]["report"], public_key, "screening_report", digest(media))
    assert (warm["recognition"], warm["catalog_digest"]) == ("no_match", baseline["catalog_digest"])
    cold_worker = settings.worker(repository)
    cold_worker.scorer = worker.scorer
    cold = screen(cold_worker)
    assert (cold["status"], cold["result"], cold["error"]) == (
        "failed", None, "Queued request or catalog integrity verification failed",
    )


def test_bad_upload_failure_and_next_job(system):
    client, worker, repository, _, service = system
    uploader = provision(repository, "studio", Role.UPLOADER)
    route = "/v1/screenings"
    metadata = {"intended_use": "campus"}
    response = client.post(route, headers={**uploader, "Content-Type": "multipart/form-data; boundary=data",
                                          "Content-Length": str(service.max_bytes + 65536)}, content=b"bad")
    assert response.status_code == 413
    bad = submit(client, route, uploader, b"RIFF\x00\x00\x00\x00WAVEinvalid", metadata)
    assert worker.run_one()
    status = client.get(f"/v1/jobs/{bad}", headers=uploader).json()
    assert status["status"] == "failed"
    assert status["result"] is None
    good = submit(client, route, uploader, wav(demo_signal(81)), metadata)
    assert worker.run_one()
    assert report_for(client, uploader, good)["payload"]["policy_outcome"] == "not_evaluated"
    with repository.engine.connect() as connection:
        assert connection.scalar(select(jobs.c.ciphertext).where(jobs.c.id == bad)) is None


def test_worker_reclaims_lease_without_overwriting_new_result(system):
    client, _, repository, _, _ = system
    uploader = provision(repository, "studio", Role.UPLOADER)
    job_id = submit(client, "/v1/screenings", uploader, b"audio", {"intended_use": "campus"})
    now = int(time())
    stale = repository.claim(now, lease_seconds=1, max_attempts=3)
    assert stale["id"] == job_id
    assert repository.claim(now, lease_seconds=1, max_attempts=3) is None
    current = repository.claim(now + 2, lease_seconds=8, max_attempts=3)
    assert current["id"] == job_id and current["lease_id"] != stale["lease_id"]
    assert current["attempts"] == 2
    assert not repository.complete(stale, now + 3, {"wrong": True})
    assert repository.complete(current, now + 3, {"done": True})
    assert not repository.complete(stale, now + 4, {"wrong": True})
    assert client.get(f"/v1/jobs/{job_id}", headers=uploader).json()["result"] == {"done": True}


def test_queue_quota_and_expired_media_cleanup(system):
    client, _, repository, _, service = system
    service.job_quota = 1
    uploader = provision(repository, "studio", Role.UPLOADER)
    route = "/v1/screenings"
    metadata = {"intended_use": "campus"}
    job_id = submit(client, route, uploader, b"audio", metadata)
    response = client.post(route, headers=uploader, files={"audio": ("audio.wav", b"audio"), "metadata": (None, json.dumps(metadata))})
    assert response.status_code == 429
    assert repository.claim(int(time()) + service.media_ttl + 1, lease_seconds=1, max_attempts=3) is None
    assert client.get(f"/v1/jobs/{job_id}", headers=uploader).json()["status"] == "failed"
    with repository.engine.connect() as connection:
        assert connection.scalar(select(jobs.c.ciphertext).where(jobs.c.id == job_id)) is None
    assert submit(client, route, uploader, b"audio", metadata) != job_id


def test_duplicate_reference_rolls_back_indexing(system):
    client, worker, repository, _, _ = system
    contributor = provision(repository, "studio", Role.CONTRIBUTOR)
    media = wav(demo_signal(65, 3))
    details = {"title": "Studio recording", "source": "Team session", "policy": {}}
    first = submit(client, "/v1/registrations", contributor, media, details)
    assert worker.run_one()
    second = submit(client, "/v1/registrations", contributor, media, details)
    assert worker.run_one()
    assert client.get(f"/v1/jobs/{second}", headers=contributor).json()["status"] == "failed"
    assert client.get(f"/v1/registrations/{second}", headers=contributor).status_code == 404
    assert client.get(f"/v1/registrations/{first}", headers=contributor).json()["status"] == "pending"


def test_postings_cap_counts_only_selected_references(system):
    _, _, repository, _, _ = system
    now = int(time())
    references = []
    for tenant, landmarks in (
        ("studio", [Landmark(7, frame) for frame in range(MAX_POSTINGS)]
         + [Landmark(8, frame) for frame in range(MAX_POSTINGS)] + [Landmark(9001, 12)]),
        ("studio", [Landmark(8, 0)]),
        ("other", [Landmark(7, 0), Landmark(9, 0), Landmark(9001, 13)]),
    ):
        principal = Principal(str(uuid4()), tenant, Role.CONTRIBUTOR)
        repository.add_principal(principal, secrets.token_hex(32))
        reference_id = str(uuid4())
        references.append(reference_id)
        request = JobRequest(reference_id, tenant, principal.id, "registration", {}, secrets.token_hex(32), now, now + 120)
        repository.submit(QueuedJob(request, b"fixture"), quota=5)
        claimed = repository.claim(now, lease_seconds=30, max_attempts=3)
        assert repository.complete(claimed, now, {}, landmarks)

    index = repository.index(references[:2])
    result = index.postings(set(range(1, 9100)))
    assert set(result) == {7, 9001}
    assert set(result[7]) == {Posting(references[0], frame) for frame in range(MAX_POSTINGS)}
    assert result[9001] == [Posting(references[0], 12)]
    assert index.postings(set()) == {}
    assert repository.index([]).postings({7}) == {}


def test_equal_evidence_requires_review():
    track = demo_signal(54)
    landmarks = fingerprint(track)
    query = track[SAMPLE_RATE:SAMPLE_RATE * 11]
    decision = decide(retrieve(query, MemoryIndex({"first": landmarks, "second": landmarks})), RuleScorer())
    assert decision.status == "ambiguous"
    assert decision.competing_references == ("first", "second")


def test_policy_checks_explicit_rules_and_effective_dates():
    policy = UsagePolicy(permitted_uses={"campus"}, restricted_uses={"ads"}, valid_until="2026-11-12")
    date = datetime(2026, 11, 12, tzinfo=UTC)
    assert evaluate_policy(policy, "campus", date) == PolicyOutcome.PERMITTED
    assert evaluate_policy(policy, "ads", date) == PolicyOutcome.RESTRICTED
    assert evaluate_policy(policy, "other", date) == PolicyOutcome.REVIEW
    assert evaluate_policy(policy, "campus", date.replace(day=13)) == PolicyOutcome.REVIEW


def test_decoder_rejects_duration_over_limit():
    with pytest.raises(ValueError, match="duration"):
        SubprocessDecoder().decode(wav(demo_signal(20, 2)), max_seconds=1)


def test_warm_decoder_reaps_each_child_and_recovers_after_rejection():
    decoder = SubprocessDecoder()
    samples = demo_signal(20, 2)
    media = wav(samples)
    with decoder.prewarm():
        rejected = decoder._spare
        assert rejected is not None
        with pytest.raises(DecodeError, match="duration"):
            decoder.decode(media, max_seconds=1)
        assert rejected.poll() is not None
        accepted = decoder._spare
        assert accepted is not None and accepted is not rejected
        assert np.array_equal(decoder.decode(media, max_seconds=2), samples)
        assert accepted.poll() == 0
        spare = decoder._spare
        assert spare is not None and spare is not accepted
    assert spare.poll() is not None


def test_decoder_timeout_covers_blocked_upload_pipe(monkeypatch):
    decoder = SubprocessDecoder(timeout_seconds=0.25)
    media = wav(demo_signal(20, 2))

    def stalled_child():
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    monkeypatch.setattr(decoder, "_spawn", stalled_child)
    with decoder.prewarm():
        child = decoder._spare
        assert child is not None
        started = monotonic()
        with pytest.raises(DecodeError, match="timed out"):
            decoder.decode(media, max_seconds=2)
        assert monotonic() - started < 5
        assert child.poll() is not None


def test_benchmark_fits_exports_and_measures_both_strategies(tmp_path):
    report = run_benchmark(tmp_path)
    assert report["corpus"] == "generated_smoke_only"
    assert set(report["strategies"]) == {"rules", "logistic"}
    assert all(strategy["validation"]["false_accepts"] == 0 for strategy in report["strategies"].values())
    model = LogisticModel.from_path(tmp_path / "logistic-model.json")
    assert model.version == report["strategies"]["logistic"]["version"]
