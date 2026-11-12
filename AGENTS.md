# Agent handoff

Start with [README.md](README.md) for installation and the offline verification smoke test. Use the assigned task ID in the [system checklist](docs/first-working-system-todo.md) or [frontend checklist](docs/frontend-todo.md) to find prerequisites and completion criteria. Check actual code and test results for a task's status.

The current milestone is the local broadcast cue audit. The API registers and approves catalog references. The CLI submits a usage audit to the shared job queue and exports signed JSON plus a review CSV. A fresh catalog generates new recording IDs, so build cue sheets using the IDs returned by the current run.

| For work on | Start with |
| --- | --- |
| Setup, registration, approval, PostgreSQL | [Backend quickstart](docs/backend-quickstart.md), [`api.py`](packages/secure_audio/api.py), [`config.py`](packages/secure_audio/config.py), [`service.py`](packages/secure_audio/service.py) |
| Cue sheets, audit CLI, reconciliation | [`cli.py`](packages/secure_audio/cli.py), [`usage.py`](packages/secure_audio/usage.py), [`worker.py`](packages/secure_audio/worker.py) |
| Fingerprint retrieval and model scoring | [`fingerprint.py`](packages/secure_audio/fingerprint.py), [`scoring.py`](packages/secure_audio/scoring.py), [`benchmarks/`](benchmarks/) |
| Catalog integrity, queue security, signing | [`catalog.py`](packages/secure_audio/catalog.py), [`crypto.py`](packages/secure_audio/crypto.py), [`repository.py`](packages/secure_audio/repository.py), [blueprint](docs/secure-audio-blueprint.md) |
| BAF and reviewer evaluation | [Broadcast audit plan](docs/broadcast-usage-audit-plan.md), [fixture benchmark results](output/benchmark/broadcast-matcher-comparison/results.json) |
| Frontend | [Frontend checklist](docs/frontend-todo.md), [app README](apps/web/app/README.md), [app instructions](apps/web/app/AGENTS.md) |

Run `python -m pytest -q` for the backend. Set `SECURE_AUDIO_TEST_DATABASE_URL` to a disposable PostgreSQL database for changes to storage, the queue, or catalog integrity that need PostgreSQL coverage. Use generated fixtures for routine checks. Keep private keys, tokens, downloaded corpora, and raw logs outside commits. Record the task ID, changes, tests run, and any outstanding dependencies in the handoff.
