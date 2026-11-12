# Run the screening backend

Run these commands from the project directory in PowerShell 7 with Python 3.11 or later. The local database and keys are created under `.secure-audio`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m secure_audio.cli init
```

In separate PowerShell terminals, start the API and worker. The API listens on loopback.

```powershell
.\.venv\Scripts\python.exe -m secure_audio.cli serve
```

```powershell
.\.venv\Scripts\python.exe -m secure_audio.cli worker
```

Provision the three actors and submit a licensed reference WAV or MP3. Set `$referencePath` and `$queryPath` to files you can use. For a first run, use the reference file as the query or export a ten-second excerpt of it. The usage policy in this example is a test fixture.

```powershell
$python = '.\.venv\Scripts\python.exe'
$base = 'http://127.0.0.1:8000'
$contributor = & $python -m secure_audio.cli add-principal --tenant studio --role contributor | ConvertFrom-Json
$reviewer = & $python -m secure_audio.cli add-principal --tenant studio --role reviewer | ConvertFrom-Json
$uploader = & $python -m secure_audio.cli add-principal --tenant studio --role uploader | ConvertFrom-Json
$contributorAuth = @{ Authorization = "Bearer $($contributor.token)" }
$reviewerAuth = @{ Authorization = "Bearer $($reviewer.token)" }
$uploaderAuth = @{ Authorization = "Bearer $($uploader.token)" }
$registrationMetadata = @{
    title = 'Demo recording'
    source = 'Team recording'
    policy = @{ permitted_uses = @('internal'); restricted_uses = @('ads') }
} | ConvertTo-Json -Depth 4 -Compress
$registration = Invoke-RestMethod "$base/v1/registrations" -Method Post -Headers $contributorAuth -Form @{
    audio = Get-Item -LiteralPath $referencePath
    metadata = $registrationMetadata
}
Invoke-RestMethod "$base/v1/jobs/$($registration.id)" -Headers $contributorAuth
```

Once the registration job is `completed`, approve it and submit a query as the uploader.

```powershell
$manifest = Invoke-RestMethod "$base/v1/registrations/$($registration.id)/approve" -Method Post -Headers $reviewerAuth
$query = Invoke-RestMethod "$base/v1/screenings" -Method Post -Headers $uploaderAuth -Form @{
    audio = Get-Item -LiteralPath $queryPath
    metadata = '{"intended_use":"ads"}'
}
$screening = Invoke-RestMethod "$base/v1/jobs/$($query.id)" -Headers $uploaderAuth
```

Once the screening job is `completed`, save its report and verify it against the exact query file. Rerun the last request above to refresh the job status.

```powershell
$screening.result.report | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath 'screening-report.json' -Encoding utf8NoBOM
& $python -m secure_audio.cli verify screening-report.json --public-key .secure-audio/keys/signing.pub --media $queryPath
```

Keep the local signing and media keys private. Distribute `signing.pub` through a trusted path when a different person verifies a report.

## Check a broadcast music usage report

Use the approved recording IDs returned by the registration workflow. Prepare a UTF-8 CSV with the columns `cue_id,recording_id,start_ms,end_ms` in that order. Cue times are whole milliseconds from the start of the programme. Pass the excerpt's programme offset as `--origin-ms`, and choose a tolerance for each cue boundary before the audit.

```powershell
$env:SECURE_AUDIO_TOKEN = $uploader.token
& $python -m secure_audio.cli audit-usage --media excerpt.wav --cues declared.csv --programme-id programme-01 --origin-ms 120000 --tolerance-ms 600 --output usage-audit.json
& $python -m secure_audio.cli verify usage-audit.json --type music_usage_audit --public-key .secure-audio/keys/signing.pub --media excerpt.wav --cues declared.csv
```

The command processes the queued job and writes a signed JSON report and a review CSV beside it. Provide `--review-csv` to choose another path. Both output paths must be new files. Overlapping candidates and cues outside the excerpt go to review. A declared cue without reliable match evidence remains unresolved.

When upgrading an older local installation, drain queued jobs with the original worker before switching, or resubmit pending uploads after the upgrade. Build the new approved catalog in a fresh tenant or database using the original recordings and reviewer approval. Older approvals lack an authenticated fingerprint digest. Keep historical reports and their original public key for offline verification.

## Compare the rules and learned verifier

```powershell
& $python -m secure_audio.cli benchmark --output .secure-audio/benchmark
& $python -m pytest -q
```

The benchmark writes `benchmark.json` and `logistic-model.json`. Its default corpus consists of generated signals and checks that fitting, threshold selection, and inference work together. To test the exported learned model in the API workflow, set `SECURE_AUDIO_MODEL` to that artifact before starting the worker. Without the setting, the worker uses the default rules.

For licensed data, pass `--manifest <path>`. The JSON must contain `train`, `validation`, and `test` objects. Each split contains `references` with `id`, `source_group`, `path`, and `license`, and `queries` with `path`, `source_group`, `condition`, and an optional `reference_id`. A positive query can specify `start_ms` and `end_ms` for localization. An unknown query omits `reference_id`. Keep related recordings in the same source group and separate the groups between splits. Use paths relative to the manifest or absolute paths.

## Reproduce the Song Describer benchmark

Save `audio.zip`, `audio_licenses.txt`, and `song_describer.csv` from the publisher in `.secure-audio/benchmark-data`. Save the pinned waveform export as `nmfp-triplet.onnx` in the same directory. The benchmark checks the model SHA-256 before loading the weights. [Song Describer downloads](https://zenodo.org/records/10072001#files) · [Pinned NMFP Triplet ONNX export](https://huggingface.co/hcX02/echora-nmfp-triplet-v1-onnx/tree/956510e2ea26d22056d94c0ee500f1840fbd4719)

```powershell
& $python -m pip install -e '.[dev,nmfp]'
$data = '.secure-audio/benchmark-data'
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '2'
& $python -m secure_audio.song_describer $data "$data/prepared"
& $python -m secure_audio.cli benchmark --manifest "$data/prepared/manifest.json" --nmfp-model "$data/nmfp-triplet.onnx" --trusted-corpus --output output/benchmark/song-describer
```

The preparation script selects recordings under CC BY, CC BY-SA, CC BY-NC, or CC BY-NC-SA and splits by artist. It records source hashes, attribution, and licenses in `provenance.json`. It derives four positive query conditions from each catalog track and creates one unrelated music query per held-out recording. Keep the generated audio local for this academic benchmark.

The rules and logistic verifier share the landmark index. The NMFP baseline embeds the same catalog and queries and ranks candidates using cosine similarity and time-offset agreement. The report includes validation thresholds and held-out results, with fingerprint, lookup, and decision times measured on decoded audio. The `--trusted-corpus` switch is for verified local benchmark files. The upload worker continues to decode in a subprocess.

## Run against PostgreSQL

Install the `postgres` extra, then supply `SECURE_AUDIO_DATABASE_URL` with a `postgresql+psycopg` URL for an empty application database. Run `init` with the intended URL before starting the processes. The optional `SECURE_AUDIO_TEST_DATABASE_URL` runs the integration tests in temporary schemas. Use a disposable test database for that setting.

For an existing PostgreSQL database, run `init` once to build the covering index on `fingerprint_postings`. PostgreSQL blocks writes to that table while the index builds. To run the worker with the trained verifier, set the model path in the worker's shell before starting it.

```powershell
$env:SECURE_AUDIO_MODEL = (Resolve-Path 'output/benchmark/song-describer/logistic-model.json').Path
& $python -m secure_audio.cli worker
```

The PostgreSQL benchmark measures hash lookup, offset voting, and logistic scoring separately on decoded clips. Point `SECURE_AUDIO_DATABASE_URL` at a disposable database and use the prepared manifest and matching cache of source recordings.

```powershell
& $python benchmarks/postgres_lookup.py --prepared .secure-audio/benchmark-data/prepared --recordings .secure-audio/benchmark-data/mirror/recordings --model $env:SECURE_AUDIO_MODEL --schema lookup_benchmark --output output/benchmark/postgres/repeat.json
```

Omit `--recordings` to reuse the populated benchmark schema. Pass `--before output/benchmark/postgres/before.json` to compare against the saved baseline and check that the ranked candidates and decisions are identical.

## Measure the full PostgreSQL screening path

Use the populated lookup benchmark schema as the catalog source. The preparation command creates a separate schema, copies the indexed benchmark fixtures, and binds their postings to approvals with dedicated benchmark keys.

```powershell
$full = @('--schema', 'full_backend_benchmark', '--data-dir', '.secure-audio/full-backend-benchmark',
          '--model', $env:SECURE_AUDIO_MODEL)
& $python -m benchmarks.postgres_backend @full --source-schema lookup_benchmark
& $python -m benchmarks.postgres_backend @full --prepared .secure-audio/benchmark-data/prepared --output output/benchmark/postgres-backend/repeat.json
```

Run the preparation command once per benchmark schema. Each measured clip goes through the loopback HTTP server, authenticated upload, PostgreSQL queue, continuous worker session, and HTTP retrieval of a signed report. The benchmark dispatches the worker immediately after upload and verifies the signature against the uploaded media. Pass `--before` with a reference run from the same evidence format and signed catalog to compare decoded samples and report contents, allowing for per-job identifiers, request digests, and timestamps.

The continuous worker keeps one decoder subprocess ready for the next upload. Each subprocess decodes one file and exits. The worker closes the spare on shutdown. The `worker --once` command starts the decoder when it claims a job.

This backend slice is intended for local development. A public deployment still needs network and process isolation for the decoder, managed keys, a TLS entry point, and a validated retention policy.
