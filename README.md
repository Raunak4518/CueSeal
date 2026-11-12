# CueSeal

A backend prototype that checks declared music cues against programme audio. A reviewer approves the reference catalog, the matcher locates recordings in the audio, and a trained logistic model scores the match evidence. The audit CLI writes a review CSV and a signed JSON report that a verifier can check against the original audio and cue sheet.

The multi-cue audit and offline verification have been demonstrated with project-owned fixtures. PostgreSQL screening and catalog-integrity benchmarks are available. The next milestone is a repeatable fresh-database audit for the team. Broadcast validation on BAF depends on dataset access.

## Start here

Use Python 3.11 or later. Run the commands from the repository root. SQLite is sufficient for the quickstart.

**Windows PowerShell**

```powershell
python -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -e '.[dev]'
& $python -m pytest -q
& $python -m secure_audio.cli init
```

**macOS or Linux**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m secure_audio.cli init
```

`init` creates the default database and local keys under `.secure-audio/`. The application reads environment variables from the shell. `.env.example` lists example values for the local data directory and the trained model.

## Verify the included example

Run this single command with the environment's Python. On PowerShell, replace `python` with `& $python`. On macOS or Linux, use `.venv/bin/python`.

```text
python -m secure_audio.cli verify output/benchmark/broadcast-usage-fixture/signed-audit.json --type music_usage_audit --public-key output/benchmark/broadcast-usage-fixture/signing.pub --media output/benchmark/broadcast-usage-fixture/broadcast.wav --cues output/benchmark/broadcast-usage-fixture/declared-cues.csv
```

Expect `"verified": true`, `"media_checked": true`, and `"cues_checked": true`. This example uses its original demo public key and requires neither a database nor a running server. Use a public key obtained from a trusted source when verifying another operator's report. [Example review CSV](output/benchmark/broadcast-usage-fixture/signed-audit.csv)

## Run your own audit

1. Start `python -m secure_audio.cli serve` and `python -m secure_audio.cli worker` in separate terminals, using the environment's Python and the same database and data-directory settings. The API listens on `127.0.0.1:8000` by default.
2. Provision contributor, reviewer, and uploader credentials. Register permitted WAV or MP3 references and approve them with the reviewer credential. The quickstart shows the requests.
3. Build a UTF-8 cue sheet with `cue_id,recording_id,start_ms,end_ms`. Use the recording IDs from your approvals and programme-relative times in whole milliseconds. The IDs in the example CSV belong to the example catalog.
4. Set `SECURE_AUDIO_TOKEN` to the uploader credential. Run `python -m secure_audio.cli audit-usage --help` for the audio, cue sheet, programme ID, origin, tolerance, and output options. Provide fresh output paths.
5. Read the review CSV and verify the signed JSON against the exact original inputs. A declaration without reliable evidence remains unresolved.

To use the trained verifier, set `SECURE_AUDIO_MODEL` to `output/benchmark/song-describer/logistic-model.json` in the worker and audit CLI shells. The default scorer uses rules. For PostgreSQL, use an existing local server, install `.[dev,postgres]`, set `SECURE_AUDIO_DATABASE_URL`, and run `init` against a fresh application database. Set `SECURE_AUDIO_TEST_DATABASE_URL` to a disposable PostgreSQL database when running integration tests. [Backend quickstart](docs/backend-quickstart.md)

## Work with the team

- Pick a task ID from the [first working system checklist](docs/first-working-system-todo.md). Check its dependencies and acceptance criteria before starting.
- For UI work, pick an ID from the [frontend checklist](docs/frontend-todo.md). The first milestone is a report viewer using the signed fixture. Browser audit submission follows the backend API contract.
- Open a branch for the task. In the pull request, include the task ID, verification commands, and their results.
- Start with [AGENTS.md](AGENTS.md) when handing a task to a coding agent. It points to the relevant code and docs by task.
- Keep credentials, private keys, database files, downloaded datasets, and raw benchmark logs local. The checked-in example uses generated audio and a public verification key.

## Run the frontend scaffold

The frontend is an early Next.js scaffold. Use Node.js 22 and pnpm 10.20.0 to match the development setup. Start it from the repository root with these commands.

```sh
cd apps/web/app
pnpm install --frozen-lockfile
pnpm dev
```

Open `http://localhost:3000`. The frontend currently serves the scaffold. The CLI workflow above runs the working audit. [Frontend setup and checks](apps/web/app/README.md)

| Path | Purpose |
| --- | --- |
| [`packages/secure_audio/`](packages/secure_audio/) | API, CLI, worker, matcher, audit reconciliation, and evidence verification |
| [`tests/`](tests/) | System, security, and matcher tests |
| [`benchmarks/`](benchmarks/) | PostgreSQL and matcher comparison runners |
| [`apps/web/app/`](apps/web/app/) | Frontend scaffold and its package configuration |
| [`docs/secure-audio-blueprint.md`](docs/secure-audio-blueprint.md) | Backend architecture and threat model |
| [`docs/broadcast-usage-audit-plan.md`](docs/broadcast-usage-audit-plan.md) | Broadcast workflow, BAF access, and evaluation protocol |
| [`docs/archive/original-blueprint.pdf`](docs/archive/original-blueprint.pdf) | Original design blueprint for historical reference |
