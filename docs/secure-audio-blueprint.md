# Secure Audio Screening

## 1. Project scope

This engineering blueprint supersedes the original Secure Audio Fingerprinting System blueprint. It defines a prototype for verifiable audio reuse screening against a controlled recording catalog. The prototype locates intervals that reuse registered audio, evaluates the catalog's configured policy, and issues a signed report that can be verified offline. YouTube Content ID and Pex provide examples of matching uploads against a reference catalog and applying policy. The project evaluates its own matcher and decision integrity with licensed audio and simulated policies. [YouTube Content ID](https://support.google.com/youtube/answer/2797370) · [Pex Attribution Engine](https://pex.com/products/attribution-engine/)

The project demonstrates three disciplines through the same decision. Audio fingerprinting proposes candidates. A supervised verifier learns when the aligned evidence supports acceptance. Cryptography binds the decision to the upload and to approved reference records. Information security protects the catalog, media, and decision process.

The current implementation accepts WAV and MP3. The licensed benchmark covers ten-second excerpts, MP3 re-encoding, added noise, music overlays, and out-of-catalog music. Covers, similar intros, and shared loops are planned tests. Platform approval records who accepted a registration and what they approved. Legal rights still depend on the underlying evidence. A no-match result means no reliable match was found in the screened catalog. [Benchmark results](../output/benchmark/song-describer/benchmark.json)

The implementation order is a backend vertical slice, an offline benchmark and operating point, deployment hardening, and then the interface. Longer video uploads, semantic search, live monitoring, royalties, and interoperability with C2PA are later work.

## 2. Requirements and workflows

1. A contributor submits a reference, source, and proposed usage policy. The worker decodes and fingerprints it. The catalog excludes pending references from search.
2. A different, authorized reviewer approves the registration. Approval signs its digest and policy. Exact duplicate submissions within a tenant are rejected. Similar recordings and shared excerpts can require reviewer inspection.
3. An uploader submits a query and its intended use. An authenticated API records a durable job. The worker screens against the tenant catalog snapshot it reads when processing starts.
4. Recognition returns `match`, `ambiguous`, or `no_match`. Decode and worker failures have a separate job state. Individual results include the query interval, the corresponding reference interval, and evidence of aligned support.
5. The policy evaluator returns `permitted`, `restricted`, `review`, or `not_evaluated`. An unknown upload leaves policy unevaluated. Ambiguous audio and absent or expired permission go to review. An explicit restriction applies to the stated use under a valid policy.
6. The worker signs its report. An offline verifier checks the signature against an independently supplied public key and verifies the exact bytes of a supplied media file. A historical report remains interpretable against the catalog and policy it recorded.

The first build uses provisioned bearer credentials for `contributor`, `reviewer`, and `uploader`. Reviewers can access tenant jobs and sign catalog approvals. Other roles can access their own jobs. A general review queue, reviewer overrides, and registration retirement are product requirements after the initial slice.

## 3. Architecture and low-level design

FastAPI is the transport. A separate polling process owns job execution. Both use the same application package and relational repository. The local configuration uses SQLite. PostgreSQL is the intended deployment database. The frontend consumes the HTTP contract later.

| Boundary | Responsibility |
| --- | --- |
| API adapter | Parse bounded multipart input, authenticate the caller, invoke use cases, and map domain errors to HTTP responses. |
| Screening service | Check roles and object ownership, submit jobs, and approve references. Inject the repository, media protection, and signing dependencies. |
| Job runner | Claim work by lease, decrypt the bounded input, invoke a decoder and fingerprint engine, and commit results through the repository. |
| Fingerprint engine and index | Generate quantized peak pairs and retrieve aligned evidence using an in-memory snapshot of verified tenant catalog postings. Benchmarks also support direct SQL and in-memory comparison indexes. |
| Scoring strategies | Use the same candidates and feature schema for a rules baseline and a fitted logistic regression artifact. Version the decision rules and thresholds. |
| Policy evaluator | Evaluate declared use against an immutable policy and an explicit time. Return a structured result independently of acoustic similarity. |
| Evidence signer and verifier | Canonicalize typed records, sign them, and verify with a trusted key. The verification command can run independently of the API and database. |

The design uses dependency injection at the API and worker entry points. A Repository keeps SQL and transaction details out of domain decisions. Strategy allows a controlled comparison of rules and learned scores. The matcher consumes only the narrow postings interface it needs. Keep stable contracts at boundaries that already have two callers or implementations.

## 4. Storage and transitions

| Record | Key fields and invariants |
| --- | --- |
| Tenant and principal | Tenant ID, principal ID, role, and a digest of a high-entropy token. A token identifies a stored principal and tenant. |
| Job | Tenant and owner, kind, payload metadata, original digest, ciphertext, timestamps, expiry, lease token, attempts, status and result. |
| Registration | Tenant, submitter, source, title, exact digest, fingerprint version, immutable policy, reviewer and signed manifest. An exact digest is unique within the tenant. |
| Fingerprint posting | Registration, acoustic hash, and anchor frame. An index on hash supports bounded candidate lookup. |

A job transitions `queued → processing → completed` or `failed`. An expired lease can be reclaimed up to the configured attempt limit. Every completion requires the current lease token. The result and media cleanup commit together. A permanent failure also clears ciphertext. Expired inputs are cleared by worker maintenance.

Reference indexing and the transition to completed happen in one transaction. The registration worker signs an indexing attestation that binds the submitted media and metadata to the count and SHA-256 digest of the sorted fingerprint postings. Approval checks that attestation and the stored postings before signing the manifest. Approval uses a conditional update and checks that contributor and reviewer are different.

The screening worker verifies the approved manifests and builds a read-only index from the exact posting bytes it verified. It holds one compact catalog snapshot per worker, keyed by tenant and the digest of the approved manifests. A different tenant or manifest set triggers a reload. The report identifies the snapshot digest and each matched reference's fingerprint digest. The snapshot has a limit of eight million postings. An independently stored catalog head would be needed to detect suppression or replay of signed approvals.

## 5. Recognition and machine learning

Decode mono PCM at a fixed sample rate. Compute a short-time Fourier transform and find local peaks in time and frequency. Pair an anchor with nearby peaks and quantize both frequency bins and their frame difference. The resulting acoustic hash can repeat across recordings. Match by joining hash postings and grouping votes by recording and reference-minus-query offset. Count distinct evidence, cap common postings, and scan overlapping windows. Merge supporting intervals only when their reference and offset agree.

The rules strategy uses explicit thresholds for aligned hashes, coverage, and duration. The learned strategy fits regularized logistic regression to candidate features from the same retriever. Inputs are alignment counts and fractions, concentration, duration, temporal coverage, and the margin over another candidate. The artifact contains the ordered feature names, scaling parameters, coefficients, intercept and operating thresholds. No serialized Python objects are needed at inference. Scores are decision scores. Require explicit calibration evidence before presenting a probability to users.

For the benchmark, separate source recordings into train, validation, and test before generating clips. Group duplicates and related recordings where feasible. Insert held-out positive references in the evaluation index and keep their signals out of fitting and tuning. Keep true unknown recordings out of the index. Use the same candidates and uploads for both decision strategies, and tune both on validation at a shared per-upload false-accept budget. Compare candidate recall, correct acceptance, wrong-track accepts, review rate, segment error, latency, and index size.

The first reproducible experiment can use generated signals to verify wiring. Final claims require real, licensed audio with known intervals and hard negatives. Report results by crop length and transformation, with uncertainty based on independent source recordings. The proposed starting targets are 95% clean 10-second correct acceptance and an approximately 1% false-accept budget for unknown uploads. These are evaluation targets.

## 6. Cryptography and information security

Hash original bytes with SHA-256. A report includes tenant, report and upload IDs, the upload digest, catalog snapshot, fingerprint and scoring versions, decision time, match intervals, reference digests and policy outcomes. A registration manifest includes the source, digest, policy, and approval actor. Sign canonical JSON using Ed25519 and use a public-key digest as the key ID. The verifier checks the expected record type and trusted key, then checks a supplied media digest when requested. A server timestamp records the issuer's stated time.

Use AES-GCM for queued media, with an independent encryption key and random nonce. Its versioned associated data binds the ciphertext to the tenant, job, submitter, job type, request metadata, media digest, creation time, and expiry. The worker authenticates these fields before decoding. The signed screening report includes the digest of the authenticated request. The API and job runner require access to local service keys in the first slice. The decoder subprocess receives bounded input through stdin, a limited environment, an explicit audio format, and a wall-clock timeout. Deployment must add process isolation, OS resource limits, private database access, secret management, and TLS. Do not expose the development API publicly.

| Threat | Required evidence |
| --- | --- |
| Catalog poisoning | Pending registration cannot influence matches. Contributor cannot approve their own claim. Exact duplicate detection works. |
| Cross-tenant data access | A valid token from another tenant cannot fetch a known job, report, or registration ID. |
| Hostile media or resource exhaustion | Oversized and corrupt media fails. Active-job quota, duration and decoder timeout are enforced. A normal subsequent job completes. |
| Modified evidence | Changing a signed interval or policy invalidates verification. Substituting another file fails the digest check. |
| Changed request or index | Editing the queued intended use fails before decoding. Substituting same-count postings prevents a worker from loading the altered catalog, and a warm worker searches its previously verified copy. |
| Worker interruption | A reclaimed lease completes at most once. A stale lease cannot overwrite the resulting report. |

## 7. Interfaces and release gates

| Interface | Contract |
| --- | --- |
| `POST /v1/registrations` | Contributor submits `audio` and a JSON `metadata` form field. Returns a queued job ID, also used as registration ID. |
| `POST /v1/registrations/{id}/approve` | A reviewer in the tenant approves an indexed reference. Returns the signed registration manifest. |
| `GET /v1/registrations/{id}` | The contributor or a tenant reviewer reads approval status and the manifest when available. |
| `POST /v1/screenings` | Uploader submits `audio` and JSON `metadata` containing `intended_use`. Returns a queued job ID. |
| `GET /v1/jobs/{id}` | Owner or tenant reviewer reads status, error, or completed result. |
| Offline CLI | Initialize storage and keys, provision demo credentials, run the worker, fit and compare scoring strategies, verify signed evidence. |

The first delivery is complete when a CLI or API client can drive submission, approval, screening, failure handling, and offline verification, and when automated tests demonstrate those behaviors. The project release additionally requires a licensed corpus, a frozen benchmark and operating point, deployment validation, and a reviewer workflow. Capture the recording manifest, split assignment, transform recipe, model and decision versions, security test output, and runbook with the final submission.

## 8. Engineering references

- [Wang's landmark fingerprinting paper](https://www.princeton.edu/~cuff/ele201/files/Wang03-shazam.pdf)
- [ISMIR 2025 neural music fingerprinting](https://zenodo.org/record/17706424/files/000046.pdf)
- [Track and musical version identification, 2026](https://arxiv.org/abs/2608.19919)
- [RFC 8032 for Ed25519](https://www.rfc-editor.org/rfc/rfc8032.html) and [RFC 8785 for canonical JSON](https://www.rfc-editor.org/rfc/rfc8785.html)
- [OWASP file upload guidance](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) and [OWASP object authorization guidance](https://api-security.owasp.org/editions/2023/en/0xa1-broken-object-level-authorization/)
- [Project research and tradeoffs](secure-audio-project-review.md)
