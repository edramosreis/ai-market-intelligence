# Repository Guidelines

## Project Structure & Module Organization

Milestone 1 uses Python 3.14, uv, SQLAlchemy Core/psycopg, Alembic, Pydantic Settings, HTTPX, FastAPI/Uvicorn, and PostgreSQL 18. `src/market_intelligence/` contains configuration, CLI, and HTTP transport (`api.py`); `db/` contains Core metadata, role setup, connections, and chunk persistence; `ingestion/` contains candle/window validation, the Coinbase client, and monthly orchestration; `queries/` contains shared read-only calculations and evidence models. `migrations/` contains immutable revisions and reference seeds. `tests/unit/` and `tests/integration/` contain deterministic provider, query/API, and PostgreSQL checks. `scripts/init_local_env.py` creates ignored credentials. `Dockerfile`, `compose.yaml`, and standalone `compose.test.yaml` support runtime jobs, the local API, and isolated tests. Keep generated output, environments, and datasets out of version control. Add `assets/` only when static resources exist.

## Architecture Review and Scope

The Milestone 1 architecture has been discussed and the user approved foundation, ingestion, and query/API implementation. The accepted contract is Coinbase Exchange spot BTC/USD, completed five-minute candles, initial backfill from 2020-01-01, configurable earlier dates, and retained history. Foundation, ingestion, and query/API are implemented; see README for live verification state. The agent checkpoint remains. Review `ARCHITECTURE.md` before continuing. Keep future components in the seven-milestone roadmap rather than scaffolding them early. Material source, data-model, technology, or direction changes require discussion; minor choices can be made within the reviewed design. OpenAI model/spend remains a decision for the agent checkpoint.

## Build, Test, and Development Commands

Run from the repository root; full installation instructions are in `README.md`.

- `python scripts/init_local_env.py`: create ignored `.env`, refusing overwrite.
- `.tools\Scripts\uv.exe sync --locked --no-python-downloads`: install locked dependencies after uv bootstrap.
- `docker compose build migrate`: build the runtime image.
- `docker compose up -d --wait db`: start persistent local PostgreSQL.
- `docker compose run --rm migrate`: provision roles, migrate, and apply grants.
- `docker compose run --rm check`: verify schema/market with reader credentials.
- `docker compose up -d --wait api`: start localhost HTTP API after explicit migrations.
- `docker compose stop api`: stop only the HTTP service.
- `.venv\Scripts\python.exe -m market_intelligence serve`: equivalent host API on 127.0.0.1:8000.
- `.venv\Scripts\python.exe scripts/check_market_api.py`: opt-in HTTP check of stored 2020/2024 history; requires API/backfill, performs no writes.
- `docker compose run --rm ingest ingest --start 2024-01-01 --end 2024-01-02`: small live historical load/replay.
- `docker compose run --rm ingest ingest --resume`: historical load from 2020, skipping exact successful chunks.
- `docker compose run --rm ingest ingest --refresh`: replay the most recent 72 hours.
- `.venv\Scripts\python.exe -m market_intelligence ingest --start 2024-01-01 --end 2024-01-02`: equivalent host job after dependency installation/database startup.
- `.venv\Scripts\python.exe scripts/check_coinbase_live.py`: opt-in public API samples from 2020/2024; no database access.
- `docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests`: isolated full suite.
- `docker compose -f compose.test.yaml down`: remove test containers/network.
- `.venv\Scripts\python.exe -m pytest -m "not integration"`: host unit tests.
- `.venv\Scripts\ruff.exe format --check src migrations tests scripts`: formatting.
- `.venv\Scripts\ruff.exe check src migrations tests scripts`: linting.
- `.venv\Scripts\mypy.exe`: strict type checking.

Do not delete the development volume without an intentional reset request. Use `docker compose config --quiet` to avoid printing resolved credentials.

Useful checks from the repository root:

- `git status --short`: review changed and untracked files.
- `git diff --check`: detect whitespace errors in unstaged changes.
- `git diff --cached --check`: check staged changes before committing.

When adding tooling, document dependency installation, local execution, build, and test commands in `README.md` and this guide.

## Coding Style & Naming Conventions

Use descriptive names, four-space indentation, Ruff formatting with a 100-character limit, and strict mypy. Prefer synchronous I/O and SQLAlchemy Core with explicit PostgreSQL behavior over speculative frameworks. Use library URL construction, parameterized values, quoted identifiers, and sanitized errors. Use aware UTC instants and Decimal values; validate timestamp awareness, numeric precision/scale, and candle eligibility before persistence.

## Testing Guidelines

Add meaningful pytest checks alongside new behavior and fixes. Integration tests use only the dedicated `db-test` container/database with memory-backed storage, never the development dataset or SQLite. Keep synthetic fixtures small and roll back test writes. Verify migrations, transactional behavior, and privileges against real PostgreSQL. Live provider/model smoke checks remain opt-in and separate from deterministic tests. Review generated migration DDL, including percent operators and constraint names.

Committed ingestion tests explicitly clean candles/runs in the guarded isolated database. Inject HTTP transport and clock/sleeper for deterministic retries and deadlines. Preserve unchanged candle provenance, atomic candle/audit commits, earlier completed months, and gap-aware resume semantics. Do not infer coverage from the latest timestamp or treat a successful gapped run as complete coverage.

Queries use actual reader credentials, read-only repeatable transactions, UTC epoch-aligned output grids, and Decimal JSON strings. Test hand-calculated metrics, exact half-open windows, coalesced/truncated gap ranges, derived constituent coverage, keyset cursor binding, staleness, HTTP errors, and concurrent-correction snapshot consistency. Full-window metrics are unavailable when any canonical bucket is absent. A page's coverage describes the whole requested window; separate pages are separate snapshots. Query test fixtures clean committed synthetic rows only in the guarded test database.

## Commit & Pull Request Guidelines

There is no commit history from which to infer conventions. Use concise, imperative commit subjects, such as `Add initial data ingestion module`. Keep commits focused.

Pull requests should explain the change, its purpose, and validation performed. Link relevant issues, identify configuration changes, and include screenshots for visible interface changes.

Propose logical commit boundaries as work progresses, but do not create commits, stage files, or push without an explicit user request.

## Security & Configuration

Never commit credentials, keys, private datasets, dumps, or personal prompts. `.env.example` contains placeholders only; actual configuration belongs in ignored `.env` or runtime environment. Supply only needed credentials to each job. Administrator access is for local bootstrap/migrations; ingestion can INSERT/UPDATE only candles/runs; API/agent must use the reader role. No OpenAI credential is needed for this foundation. Exclude secrets from Docker context as well as Git. Update this guide and README when commands, layout, or dependencies change.
