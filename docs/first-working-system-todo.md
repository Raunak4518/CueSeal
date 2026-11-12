# First working system checklist

## What V1 does

A contributor registers permitted reference recordings. A reviewer approves them. An uploader gives the CLI one programme audio excerpt and a CSV of declared music cues. The system matches the excerpt against the approved catalog, compares the detected intervals with the declarations, and produces a signed JSON report and a review CSV. A second person verifies the signed JSON against the original audio and cue sheet with a trusted public key.

The proposed V1 runs locally on PostgreSQL, uses the existing API for registration and approval, and uses the existing CLI for the audit. PostgreSQL is the target because the backend has already been tested and benchmarked on it. SQLite remains available for a quick development run. The demo uses project-owned fixtures and the existing trained logistic verifier. Its scope is one WAV or MP3 excerpt of up to 60 seconds, one tenant's approved catalog, and one cue sheet with known recording IDs. A reviewer reads the CSV and listens to flagged intervals when needed.

The ML component scores aligned fingerprint candidates. Ed25519 signatures and hashes bind evidence to the inputs and approved catalog. Authentication, role checks, encrypted queued media, and catalog verification protect the decision path. A no-evidence finding means the checker could not corroborate the declaration within the catalog and excerpt it searched.

## What already exists

| Status | Existing capability | Evidence |
| --- | --- | --- |
| Verified | Local API, worker, relational job queue, bearer credentials, and contributor, reviewer, and uploader roles | [Backend quickstart](backend-quickstart.md), [system tests](../tests/test_system.py) |
| Verified | Reference registration, fingerprint extraction, reviewer approval, and search against verified catalog postings | [Blueprint](secure-audio-blueprint.md), [catalog tamper results](../output/benchmark/catalog-integrity/attack-results.json) |
| Verified | Trained logistic verifier and a rules baseline evaluated on Song Describer audio | [Model](../output/benchmark/song-describer/logistic-model.json), [evaluation](../output/benchmark/song-describer/benchmark.json) |
| Verified | PostgreSQL lookup and full upload-to-signed-report screening benchmarks | [Lookup results](../output/benchmark/postgres/after.json), [backend results](../output/benchmark/postgres-backend/after.json) |
| Verified | Multi-cue usage audit with signed JSON, review CSV, and programme-relative timestamps | [Signed audit](../output/benchmark/broadcast-usage-fixture/signed-audit.json), [review CSV](../output/benchmark/broadcast-usage-fixture/signed-audit.csv) |
| Verified | Offline verification accepts original files and rejects altered audio, cue sheet, or signed findings | [Verification results](../output/benchmark/broadcast-usage-fixture/verification-results.json) |
| Verified | Shared-fixture comparison with PeakFP and Panako. Secure Audio confirmed six of eight cues, sent the other two to review, and produced zero false alerts in that run | [Comparison results](../output/benchmark/broadcast-matcher-comparison/results.json) |
| To do | Package a repeatable start-to-finish demo and test it from fresh state | V01–V55 below |

The dependencies column lists direct prerequisites. Their own prerequisites apply transitively. **Independent** means the task can start immediately. **External** marks an input outside this repository. The V1 list includes setup and acceptance runs for code that already exists. **If needed** rows call for implementation only if an acceptance run exposes a blocker.

## V1 checklist

### A. Agree on inputs and expected outputs

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| V01 | Adopt the V1 workflow and local runtime described above | Independent | The team agrees on the actor roles, PostgreSQL demo target, CLI entry point, and supported input sizes and formats. |
| V02 | Record the exact trained model used for the demo | Independent | The model file, its hash, version `logistic-v1-5da5509dd9e2d9cd`, and confirmation and review thresholds `0.94` and `0.82` are recorded. |
| V03 | Inventory the permitted demo inputs | Independent | The three generated reference WAVs and 53-second query are located or can be regenerated. Their provenance and hashes are recorded. |
| V04 | Write the expected acoustic cue timeline | V03 | The five true cues in the original broadcast fixture are recorded, including the two simultaneous tracks. The timeline comes from how the audio was generated. |
| V05 | Prepare a declared-cue template using track labels | V04 | The template contains an accurate cue, repeat with a wrong end time, duplicate declaration, two overlap rows, absent cue, unknown ID, and outside-excerpt row. A real occurrence is intentionally omitted. |
| V06 | Freeze the demonstration settings and expected findings | V01, V02, V05 | Programme ID, `120000` ms origin, and `600` ms boundary tolerance are recorded. The expected result for every row and the omitted occurrence is written down. |
| V07 | Identify the files a verifier needs | V01 | The handoff lists signed JSON, original WAV and CSV, and the public key obtained from a trusted source. The review CSV is identified as the readable export. |

### B. Prepare a fresh local environment

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| V08 | Create a fresh Python environment and install the backend | V01 | The project and its `dev` and `postgres` extras install, and the CLI responds to `--help`. Record the Python version used. |
| V09 | Start or select a local PostgreSQL instance | V01 | A local instance is reachable and its connection details are available to the operator. **External** if there is no usable local PostgreSQL installation. |
| V10 | Create a fresh demo database and separate test database | V09 | The demo and integration tests have distinct disposable databases or isolated test schemas, with the database URL for each clearly identified. |
| V11 | Choose an isolated data directory and configure the environment | V02, V08, V10 | The demo points at its own data directory and database, and `SECURE_AUDIO_MODEL` resolves to the pinned artifact. Secrets stay out of the runbook and demo output. |
| V12 | Initialize schema and keys | V11 | `init` succeeds on the fresh database and creates the demo signing and encryption keys. The public key can be exported independently. |
| V13 | Provision contributor, reviewer, and uploader credentials | V12 | Three distinct principals exist in the demo tenant and each credential is assigned to the right role. |
| V14 | Start the loopback API and worker | V12 | The API health endpoint responds and the worker can connect to the same database. Registration jobs can be picked up. |
| V15 | Confirm the local test suite | V08 | The full suite completes and the result is recorded. |
| V16 | Confirm the PostgreSQL integration suite | V08, V10 | The suite runs with `SECURE_AUDIO_TEST_DATABASE_URL` and the output records which tests exercised PostgreSQL. |

### C. Build the approved catalog from the fixtures

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| V17 | Submit all three reference WAVs as the contributor | V03, V13, V14 | The API returns three registration job IDs, and each ID is associated with its fixture label. |
| V18 | Poll indexing jobs and capture their results | V17 | Each job completes with a pending registration and fingerprint attestation. A failed job is reported with its reason. |
| V19 | Verify that pending references stay out of search | V18 | A pending reference is excluded in a test or a captured control run. |
| V20 | Approve the indexed recordings as the reviewer | V18, V19 | All three approvals succeed using the reviewer credential and return signed manifests. |
| V21 | Check the approval bindings | V20 | The approved IDs, media digests, fingerprint version, posting counts, and posting digests match the registered fixtures and their attestations. |
| V22 | Write the fixture-to-recording-ID map | V20 | Labels A, B, and C map to the actual IDs created in this run. |
| V23 | Generate the cue sheet from the template and current IDs | V05, V22 | A UTF-8 CSV with `cue_id,recording_id,start_ms,end_ms` exists. Only the deliberate unknown row uses an ID outside the approved catalog. |

### D. Run the audit and check the result

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| V24 | Run `audit-usage` with the uploader credential | V06, V13, V21, V23 | The CLI uses the pinned model and configured origin and tolerance, completes the job, and writes a fresh JSON/CSV pair. |
| V25 | Check that the signed report identifies the run | V24 | It includes the audio and cue-sheet digests, request and catalog digests, model version and thresholds, programme ID, excerpt origin and duration, and tolerance. |
| V26 | Check the correctly declared cue and the repeated occurrence | V24 | The accurate first cue is confirmed. The separate repeat is matched to its own occurrence and flagged for the wrong end time. |
| V27 | Check the omitted occurrence | V24 | The report identifies the omitted Track B occurrence as `detected_unlisted`, with a programme-relative interval. |
| V28 | Check duplicate and simultaneous cues | V24 | The duplicate declaration cannot claim the same confirmed detection twice. Both simultaneous tracks have review evidence in the fixture. |
| V29 | Check unresolved and outside-excerpt rows | V24 | The absent recording is `declared_without_evidence`. The unknown ID and outside-excerpt row receive their specific review reasons. |
| V30 | Check the review export against the signed JSON | V25, V26, V27, V28, V29 | The CSV has the same findings, statuses, reasons, and time columns as the signed payload. The expected fixture yields nine findings. |
| V31 | Run a negative control through the audit | V13, V21, V24 | An out-of-catalog or silent permitted clip with a header-only cue sheet produces no confirmed cue or unlisted detection. The signed report verifies. |
| V32 | Exercise the MP3 input path | V13, V21, V24 | A permitted MP3 fixture with a known cue goes through the same CLI and produces a verifiable report. Record the observed timing or match behavior. |

### E. Verify the security and failure paths

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| V33 | Verify the original audit offline | V07, V24 | `verify --type music_usage_audit` accepts the signed JSON, both original inputs, and the trusted public key on a verifier environment with no application database or credentials. |
| V34 | Check altered inputs and evidence | V33 | Changing the audio, changing one cue-sheet value, and editing a signed finding each cause a separate verification failure. Preserve the original files. |
| V35 | Check trusted-key enforcement | V33 | Verification fails with a different public key. The demo documents how the reviewer obtains the trusted key. |
| V36 | Check role and tenant boundaries | V13, V14, V18 | Invalid credentials and the wrong role fail. A valid principal in a second tenant cannot retrieve or approve known demo records or jobs. |
| V37 | Check the queued-request tamper case | V16 | The existing tamper test passes on PostgreSQL. An edited authenticated request fails before decoding, and a normal control job succeeds. |
| V38 | Check the approved-posting tamper case | V16 | The existing same-count replacement test passes on PostgreSQL. A cold worker rejects altered postings and a warm worker uses its previously verified copy. |
| V39 | Check invalid and excessive inputs | V13, V14, V23 | Malformed CSV, duplicate cue IDs, invalid times, corrupt audio, an over-limit input, and an invalid output path yield clear failures. An ordinary audit still completes afterward. |
| V40 | Check queue recovery and media cleanup | V16 | Existing lease, quota, and expiry tests pass on the target database. A stale lease cannot overwrite a completed job, and terminal jobs release queued media. |
| V41 | Check failure when an output destination is unusable | V24 | The operator can identify what was written and recover a usable JSON/CSV pair, including when one file cannot be written. Existing reports survive a repeated run targeting the same paths. |
| V42 | Fix any acceptance blockers and rerun affected checks | V15, V16, V30–V41 | **If needed**, address failures that prevent installation, correct findings, usable reports, or required security behavior. Record the reproduced failure and the passing rerun. |

### F. Make it repeatable and hand it over

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| V43 | Create the demo fixture preparation command or script | V03, V05, V22, V23 | The permitted fixtures and cue template are available locally, and the script can fill in fresh registration IDs. |
| V44 | Add one repeatable demo entry point | V24, V33, V43 | One script or a short documented command sequence runs initialization, registration, approval, auditing, and verification from fresh state. It checks exit codes and uses fresh outputs. |
| V45 | Add automatic checks for the expected demo findings | V30, V34, V44 | A run reports whether the expected nine findings and tamper checks passed. It tolerates new job IDs, signing keys, and timestamps on every fresh run. |
| V46 | Write setup and troubleshooting steps | V11–V14, V39, V41, V44 | The runbook covers configuration, service startup, expected outputs, database or port errors, role or model mistakes, failed jobs, and output-path recovery. |
| V47 | Write a short operator walkthrough | V24, V30, V33, V46 | A reader can submit a cue sheet, interpret each result, and verify the JSON. They can tell an unresolved cue from a confirmed discrepancy. |
| V48 | Prepare the shareable demo package | V07, V43, V45, V47 | The package contains permitted inputs, scripts or commands, model identity, example outputs, public verification material, and test instructions. Check it for secrets and restricted audio. |
| V49 | Check artifact reproducibility and historical verification | V33, V45, V48 | Record input hashes, model identity, dependency versions, command used, and test results. The saved example still verifies against its original inputs and public key after a fresh run issues new evidence. |
| V50 | Run the complete demo from a second empty environment | V42, V46, V48, V49 | A teammate or a separate clean environment can follow the runbook using a fresh database and keys. Capture where the operator needed help. |
| V51 | Apply fixes from the fresh run | V50 | **If needed**, correct setup failures or missing instructions and repeat the failed steps from fresh state. |
| V52 | Test the reviewer handoff using only the shared files | V47, V50, V51 | A teammate or a separate verifier session can verify the JSON and identify one confirmed cue, one discrepancy, and one review-only case using the files provided. |
| V53 | Save the final V1 acceptance record | V31–V42, V45, V49, V52 | The record lists the successful demo, observed findings, security tests, known limits, and the exact files used. |
| V54 | Prepare the presentation demo sequence | V48, V53 | The sequence shows the cue sheet, approved catalog, one complete audit, one flagged discrepancy, and verification rejecting a changed input. It labels the matcher benchmark as fixture evidence. This can run alongside V55. |
| V55 | Mark V1 complete | V53 | The team can reproduce the basic workflow, explain the signed evidence, and run the demo from the documented package. |

### Where to start

1. **V01–V07** settle the fixture, model, and expected results. V02 and V03 can start immediately in parallel.
2. **V08–V16** prepare the environment. The Python setup and PostgreSQL setup can run in parallel. The local and PostgreSQL test runs can start before catalog registration.
3. **V17–V24** are the first complete working path. Register, index, approve, map the new IDs, generate the cue sheet, and run the audit.
4. **V25–V42** verify the findings and security behavior. Most checks can run independently once the audit is available.
5. **V43–V55** turn the successful run into a handoff. The finish line is a clean run and independent verification.

The first execution batch is **V01–V16**. The core dependency chain after setup is **V17 → V18 → V19 → V20 → V22 → V23 → V24 → V30 → V45 → V48 → V50 → V52 → V53 → V55**. The security checks also feed into the final tasks.

## After V1

### Real-audio evaluation and evidence of usefulness

BAF access can be handled in parallel with V1. BAF audio is an external dependency for the broadcast benchmark. Use permitted fixtures for the V1 demo and the initial reviewer exercise. [Existing access and evaluation plan](broadcast-usage-audit-plan.md)

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| E01 | Confirm BAF request status and prepare any missing access information | External access account and academic details, if requested | The application status is known and any submission or clarification is ready for the account holder. |
| E02 | Obtain access and verify the terms for the planned experiment | E01, external dataset approval | The allowed local processing, index retention, attribution, and published artifacts are documented. |
| E03 | Validate the received data and annotation format | E02 | The dataset version, hashes, files, annotations, and missing inputs are inventoried. |
| E04 | Choose the first eligible BAF subset and evaluation splits | E03 | Every matcher gets the same references and unchanged query excerpts. Related excerpts stay grouped, and out-of-catalog annotations are accounted for. |
| E05 | Adapt the benchmark to broadcast annotations | E04 | The benchmark loads multi-cue ground truth and represents repeat, overlap, short, background, and negative cases supported by the annotations. |
| E06 | Freeze the protocol and operating points | E05 | Pin tool versions, tolerances, windows, metrics, and test splits. Any tuning uses validation data and finishes before test scoring. |
| E07 | Run rules, logistic, PeakFP, and Panako on the same subset | E06 | Compare cue recall, timing, false alerts per hour, seconds-based precision and recall, and latency. Preserve permitted aggregate results and reproducibility metadata. |
| E08 | Inspect failure cases and choose one improvement target | E07 | Break down missed and spurious detections by condition and determine whether the bottleneck is retrieval, scoring, interval boundaries, or reconciliation. |
| E09 | Test one improvement on a held-out comparison | E08 | Change one factor, measure the tradeoff in missed cues, timing, and false alerts, and decide whether to adopt it. |
| E10 | Prepare a permitted reviewer pilot | V55 | Create comparable cue sheets with known omissions, wrong IDs, timing errors, duplicates, and intact controls. Freeze the expected findings. |
| E11 | Compare manual review with tool-assisted review | E10, reviewer availability | Rotate comparable clips across conditions and measure elapsed review time, real errors missed, and false alerts handled. |
| E12 | Compare assistance from the strongest baseline | E07, E10, reviewer availability | Run the strongest eligible baseline through the same reconciliation and report format. Compare reviewer outcomes at equivalent effort. |
| E13 | Decide what to retain or change based on results | E09, E11, E12 | Document which matcher and review workflow provide the most useful results for the tested cases and which uncertainties remain. |

### Product and operational extensions

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| X01 | Decide whether report inputs need title or external-ID mapping | V55, sample cue sheets from the intended workflow | Common identifiers and ambiguous mappings are understood well enough to define a supported import. |
| X02 | Add the selected cue-sheet importer and mapping review | X01 | It maps known entries to approved recordings, sends unresolved entries to review, and preserves the original input used for verification. |
| X03 | Add full-programme audio support if required | V55, representative programme lengths and boundary cases | Chunking, programme timestamps, cues spanning chunks, duplicate detections, and partial coverage are verified on long files. |
| X04 | Define a persistent reviewer workflow if the pilot supports it | E11 | Decide how reviewers record decisions, correct mappings, and track which signed report they reviewed. |
| X05 | Add an asynchronous audit API if clients need remote submission | V55, agreed remote client workflow | Authenticated clients can submit audio and cues, poll job status, and retrieve the resulting signed evidence. |
| X06 | Build a small interface around the proven operator flow | [Frontend checklist](frontend-todo.md). X05 enables browser submission and X04 enables persistent review | Reviewers can open an audit, locate flagged intervals, and obtain the signed evidence. Browser submission follows when the audit API is available. |
| X07 | Define the deployment threat model and operating limits | V55, intended hosting and data-handling requirements | The team specifies access, decoder isolation, secrets, network exposure, retention, and backup expectations. |
| X08 | Implement and validate deployment controls | X07 | Verify TLS, restricted database access, managed secrets, decoder resource isolation, monitoring, and recovery against the agreed environment. |
| X09 | Add catalog lifecycle and key rotation when operationally required | X07 | Retired references leave active search, prior reports remain verifiable, and trusted catalog history addresses replay or removal of signed approvals. |
| X10 | Validate expected catalog size and concurrency | X07, expected traffic and catalog data | Measure realistic index size, lookup, worker latency, retries, and concurrent jobs. Fix the measured bottleneck if it exceeds the operating limit. |
| X11 | Update the blueprint and project presentation with measured results | V55 for the V1 version, E13 for real-broadcast or reviewer claims | The materials describe the actual workflow, evidence, limitations, and next engineering decision. |
