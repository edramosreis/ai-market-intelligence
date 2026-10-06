# Repository Guidelines

## Project Structure & Module Organization

Milestone 1 uses Python 3.14, uv, SQLAlchemy Core/psycopg, Alembic, Pydantic Settings, HTTPX, FastAPI/Uvicorn, the official OpenAI SDK, and PostgreSQL 18. `src/market_intelligence/` contains configuration, CLI, and HTTP transport (`api.py`); `db/` contains metadata, roles, connections, and persistence; `ingestion/` contains validation, Coinbase transport, and monthly orchestration; `queries/` contains read-only calculations/evidence; `agent/` contains strict arguments, instructions, two tools, and the bounded Responses loop. `migrations/` has immutable revisions/seeds. `tests/unit/` and `tests/integration/` contain deterministic provider, query/API, agent, and PostgreSQL checks; `tests/agent_fakes.py` supplies synthetic replies through the real SDK's in-memory transport. `scripts/init_local_env.py` creates ignored credentials. Docker/Compose support runtime jobs, the localhost API, and isolated tests. Keep generated output, environments, and datasets out of Git. Add `assets/` only when static resources exist.

The initial Treasury component lives in `treasury/`: native date/tenor/yield models and a bounded monthly XML client. Revision `0002` adds dedicated date/tenor facts and monthly audits with current values plus ingestion provenance. `tests/unit/test_treasury.py` uses synthetic XML and HTTP transports; PostgreSQL tests verify Treasury constraints, grants, and upgrade compatibility. `scripts/check_treasury_live.py` is an opt-in source-only check. Treasury ingestion and HTTP/agent queries are in development.

## Architecture Review and Scope

The contract is Coinbase Exchange spot BTC/USD, completed five-minute candles, retained history initially from 2020-01-01, and configurable earlier dates. Milestone 1's local vertical slice is implemented and verified; README records deterministic tests and separately inspected live-agent checks. Review `ARCHITECTURE.md` before continuing. Local planning is kept in ignored `.private/ROADMAP.md`, outside Git and Docker context. Continue consulting and updating it as work progresses, without staging or publishing it; if absent, discuss future scope before implementing later components. Local live-test authorization, account readiness, and cumulative usage are recorded in `.private/AGENT_ACCEPTANCE.md` and its ignored ledger. Consult them before paid checks, preserve usage across attempts, and do not treat request limits as a monetary cap. Completing a live check does not authorize additional spending. Material source, data-model, technology, or direction changes require discussion; minor choices stay within the reviewed design.

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
- `.venv\Scripts\python.exe scripts/check_treasury_live.py`: opt-in public Treasury monthly samples from 2020/2024; no keys, database access, or model calls.
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

Treasury observations use source dates and nominal yield percentages, not crypto candle timestamps. Preserve source-null versus absent-tenor reasons and actual zero rates. Reject malformed/oversized XML, DTD/entities, duplicate/out-of-month dates, unsupported fields, and truncated monthly feeds. Keep retries, pacing, streaming reads, and deadlines injectable. Source samples establish only returned dates; do not invent holiday observations or claim publication-calendar completeness from a successful fetch.

Agent tests use the real OpenAI SDK with an injected in-memory HTTP transport, no live key/network/spend. Preserve the two-tool allowlist, fixed market, strict/duplicate argument rejection, actual query execution, stateless reasoning relay, exact server-collected evidence, three-tool/four-request maximum, input/output limits, and controlled data/service failures. Verify no database connection is held during model calls. `AGENT_ENABLED` defaults false; never select a live model or make paid calls without the model/spend decision. Strict schemas and tool evidence do not guarantee factual prose; manual live acceptance remains required. Concurrency limits are per process and are not a global spend cap.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects, such as `Add initial data ingestion module`. Keep commits focused.

Pull requests should explain the change, its purpose, and validation performed. Link relevant issues, identify configuration changes, and include screenshots for visible interface changes.

Commit file additions, edits, and removals promptly in small, coherent batches as work progresses. Run checks appropriate to each batch and review staged changes for credentials and private files before committing. Do not wait for an entire feature or milestone to finish. Each commit should describe a reviewable change and accurately state any pending acceptance checks. Pushes, pull requests, and merges still require user authorization. Keep personal workflow preferences and session-specific authorization in ignored local notes.

## Security & Configuration

Never commit credentials, keys, private datasets, dumps, or personal prompts. `.env.example` contains placeholders; actual configuration belongs in ignored `.env` or runtime environment. Supply only needed credentials: administrator for bootstrap/migrations, writer for ingestion, reader for API/agent. Only the API receives optional OpenAI configuration. Market endpoints and deterministic tests need no real OpenAI key. Do not persist conversations or log full prompts/answers, authorization headers, or SDK payloads. Exclude secrets from Docker context and Git. Update this guide and README when commands, layout, or dependencies change.
