# Repository Guidelines

## Project Structure & Module Organization

Milestone 1 uses Python 3.14, uv, SQLAlchemy Core/psycopg, Alembic, Pydantic Settings, HTTPX, FastAPI/Uvicorn, the official OpenAI SDK, and PostgreSQL 18. `src/market_intelligence/` contains configuration, CLI, and HTTP transport (`api.py`); `db/` contains metadata, roles, connections, and persistence; `ingestion/` contains validation, Coinbase transport, and monthly orchestration; `queries/` contains read-only calculations/evidence; `agent/` contains strict arguments, instructions, seven fixed BTC/Treasury/Hyperliquid tools, and the bounded Responses loop. `migrations/` has immutable revisions/seeds. `tests/unit/` and `tests/integration/` contain deterministic provider, query/API, agent, and PostgreSQL checks; `tests/agent_fakes.py` supplies synthetic replies through the real SDK's in-memory transport. `scripts/init_local_env.py` creates ignored credentials. Docker/Compose support runtime jobs, the localhost API, and isolated tests. Keep generated output, environments, and datasets out of Git. Add `assets/` only when static resources exist.

The Treasury component lives in `treasury/`: native date/tenor/yield models, a bounded monthly XML client, explicit monthly orchestration, and read-only curve/range/spread queries with evidence models. `db/treasury_store.py` writes current values plus ingestion provenance to revision `0002`'s dedicated facts/audits. Treasury tests use synthetic XML and real isolated PostgreSQL for constraints, grants, migration compatibility, replay, corrections, omitted dates, locking, rollback, pagination, and snapshots. `scripts/check_treasury_live.py` checks the source; `scripts/check_treasury_api.py` compares stored HTTP results to fresh samples. The agent reuses single-date curves and compact paginated benchmark/spread evidence; simulated-model tests verify actual reader queries. Six Treasury live acceptance cases passed separate evidence/prose inspection; README records their scope and the cursor/coverage fixes.

## Architecture Review and Scope

The Hyperliquid component in `hyperliquid/` reads fixed native BTC perpetual funding events and current OI contexts, with separate persistence, manual jobs, and reader/API evidence. Preserve signed decimal fractions, exact millisecond source times, derived settlement hours, BTC quantity units, USDT denomination, and distinct USDC collateral/settlement. OI has local fetch/receipt times and a snapshot identity, with no source event timestamp. Inclusive funding pagination must remove only identical boundary overlap and reject ambiguous events. `scripts/check_hyperliquid_live.py` is an opt-in public sample check; `scripts/check_hyperliquid_api.py` verifies HTTP/source funding parity and stored OI receipt evidence. Existing locked dependencies suffice. All 541 isolated tests and separate live data acceptance checks pass; README records the observed source omission and retained-data verification. Hyperliquid agent integration adds only latest settled funding, complete-hour funding summaries, and latest stored OI. Adapters reuse the existing reader queries; native units, source/receipt time distinctions, and gap/stale/no-data limitations remain explicit. All 589 deterministic tests (348 unit / 241 integration), Ruff and strict mypy pass. Simulated SDK replies verify real reader queries through HTTP, no writes or held reader connections during model waits, exact evidence after corrections/new receipts, source-specific limitations and existing budgets. Nine separate live acceptance cases passed exact evidence comparison and manual inspection on 2026-10-08, including native funding/OI prose, gap/stale/no-data limitations and a three-source answer. README records their scope; arbitrary prose factuality is not guaranteed.

The contract is Coinbase Exchange spot BTC/USD, completed five-minute candles, retained history initially from 2020-01-01, and configurable earlier dates. Milestone 1's local vertical slice is implemented and verified; README records deterministic tests and separately inspected live-agent checks. Review `ARCHITECTURE.md` before continuing. Local planning is kept in ignored `.private/ROADMAP.md`, outside Git and Docker context. Continue consulting and updating it as work progresses, without staging or publishing it; if absent, discuss future scope before implementing later components. Local live-test authorization, account readiness, and cumulative usage are recorded in `.private/AGENT_ACCEPTANCE.md`, `.private/TREASURY_AGENT_ACCEPTANCE.md`, `.private/HYPERLIQUID_AGENT_ACCEPTANCE.md`, and their separate ignored ledgers. Consult them before paid checks, preserve usage across attempts, and do not treat request limits as a monetary cap. Completing a live check does not authorize additional spending. Material source, data-model, technology, or direction changes require discussion; minor choices stay within the reviewed design.

The reviewed macro direction is direct BLS `CUSR0000SA0` CPI and `LNS14000000`
unemployment, then Federal Reserve Board H.15 `RIFSPFF_N.M` monthly effective federal
funds rates. Section 14 of `ARCHITECTURE.md` records verified source boundaries.
The two provider clients and native models in `macro/` are implemented with synthetic
tests. Revision `0004` adds dedicated macro catalog, receipt audits, immutable versions
and footnotes, and current-version pointers. `db/macro_store.py` atomically persists
initial states and changed content, preserves unchanged provenance, reports retained
omissions and rejects delayed older overlapping receipts. Jobs, reader/API and agent
integration remain pending. `bls.py` reads the
two fixed series through keyless v1; `fed.py` reads only the selected monthly series
from bounded full-release ZIP/XML, without extraction or remote schema loading.
Preserve monthly periods, native index/percent units,
seasonal-adjustment metadata, missing markers and source footnotes. BLS `M13` is an
annual average, not a month; `latest` is a response hint, not a release timestamp.
Fed XML uses month-end labels and includes a timezone-free prepared time. Neither
establishes per-observation publication time. The approved correction policy is current
history plus immutable changes observed locally after collection, with no retrospective
historical-vintage claim. Do not implement a FRED dependency, silently replace native
monthly rates with daily averages, or depend on retiring DDP custom/preformatted routes.

The macro storage checkpoint passes 777 isolated tests (483 unit / 294 integration),
Ruff and strict mypy. Catalogs are SELECT-only, versions/footnotes INSERT-only for the
writer, and current pointers/audits INSERT/UPDATE with no DELETE. Preserve content and
ordered source footnotes in immutable versions; numeric formatting, footnote ordering,
BLS latest hints and Fed prepared/series metadata alone do not create a new version.
Separate audit receipts still record replays. Versions, footnotes, current pointers and
successful counts commit together; controlled failure audits use a separate transaction.
Keep independent provider write locks and stale-receipt protection even after an
unchanged replay. An omitted period stays retained without fresh confirmation. Apply
revision 0004 before starting the updated reader/API; this checkpoint applied it only
in isolated tests. Keep strict requested-ID matching, duplicate JSON rejection,
ten-inclusive-year BLS requests and validated/counted annual exclusions. Validate all
Fed selected rows before window filtering and consume the complete data XML, including
content after the selected series. Enforce compressed/decompressed/archive/work bounds,
safe UTF-8 and native month-end labels; unsupported selected missing statuses still fail.
Preserve source annotations separately from observation footnotes. Provider success does
not establish publication-calendar completeness. Live scripts remain separate opt-in
checks and consume public provider quotas; pacing is not a daily-quota guard.

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
- `.venv\Scripts\python.exe scripts/check_treasury_live.py`: opt-in public Treasury monthly samples from 1990/2020/2024; no keys, database access, or model calls.
- `.venv\Scripts\python.exe scripts/check_hyperliquid_live.py`: opt-in public BTC perpetual funding/context samples; no keys, database, paid archives, or model calls.
- `.venv\Scripts\python.exe scripts/check_bls_live.py --year 2024`: opt-in one-year keyless CPI/unemployment source check; no database, saved dataset or model calls. BLS v1 quotas still apply.
- `.venv\Scripts\python.exe scripts/check_fed_live.py --year 2024`: opt-in full H.15 ZIP/XML source check retaining only the requested monthly-rate year in memory; no database, saved dataset or model calls.
- `.venv\Scripts\python.exe scripts/check_hyperliquid_api.py`: opt-in funding HTTP/source parity and stored OI receipt checks with reader credentials; no writes or model calls.
- `.venv\Scripts\python.exe scripts/check_treasury_api.py`: opt-in stored HTTP pagination/rates/spreads versus fresh 1990/2020/2024 samples; requires API/backfill, performs no writes or model calls.
- `docker compose run --rm ingest ingest-treasury --start 2024-01-01 --end 2024-02-01`: load/replay one Treasury month.
- `docker compose run --rm ingest ingest-treasury --resume`: backfill from 1990; reuse successful historical feed reads and refetch the current month.
- `docker compose run --rm ingest ingest-treasury --refresh`: replay the previous and current source months.
- `docker compose run --rm ingest ingest-funding --resume`: backfill BTC perpetual funding from 2024; refetch gaps/latest failures and the current month.
- `docker compose run --rm ingest ingest-funding --refresh`: replay previous/current months; do not combine with `--resume`.
- `docker compose run --rm ingest collect-open-interest`: append one current BTC OI snapshot with a separate audit; no scheduler or historical archive.
- `.venv\Scripts\python.exe -m market_intelligence ingest-treasury --start 2024-01-01 --end 2024-02-01`: equivalent host job.
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

Preserve valid historical date-only Treasury entries with all fourteen tenors `field_absent`. Keep their available-rate count at zero and their spread unavailable; missing/invalid dates still fail. This retains source-provided dates without fabricating observations from a calendar.

Treasury monthly persistence commits facts and successful audit counts atomically. Preserve unchanged provenance; changed values/reasons replace current facts. Retain/report dates omitted by a refresh, without claiming fresh verification. Resume verifies normalized stored tenor counts before reusing historical audits; the current month is always fetched. Reader/writer grants and independent advisory locks are tested against PostgreSQL. Historical corrections and retries of failed historical replays require explicit replay without `--resume`. Reject combined Treasury `--refresh --resume` before configuration/database access.

Treasury queries use reader-role read-only repeatable snapshots, half-open date windows, date-keyset cursors bound to the dataset/window, maturity order, and Decimal JSON strings. A curve includes all fourteen normalized tenors with explicit source-null/field-absent/not-stored reasons. Require both same-date benchmark yields for 10Y-minus-2Y spreads, preserving signed percentage points and basis points. Coverage counts stored source dates without an invented calendar. Monthly fetch audit time is not an observation release time or a per-row last-verification timestamp. Verify concurrent-correction consistency and sanitized HTTP failures.

Agent tests use the real OpenAI SDK with an injected in-memory HTTP transport, no live key/network/spend. Preserve the seven-tool allowlist (BTC latest/summary, Treasury curve/spread history, and Hyperliquid latest funding/funding summary/latest OI), fixed market/dataset, strict/duplicate argument rejection, actual query execution, stateless reasoning relay, exact server-collected evidence, three-tool/four-request maximum, input/output limits, and controlled data/service failures. Verify no database connection is held during model calls. Treasury tools use strict source dates and twenty-date spread pages; a required nullable cursor starts at null and binds to the same date window. Strict schemas offer only null and the latest server-issued continuation per queried window, retiring completed-window tokens without sharing state between questions. Unfinished cursor chains return partial_results. Preserve source-specific no-data/incomplete explanations and missing_rates for entirely unavailable curves or history benchmark inputs. Coverage first/last bounds describe the whole window; only observations identify returned page dates. Do not infer calendar completeness, page overlap from coverage, substitute dates, forward-fill, or invent cross-domain calculations. Monthly audits and materialization provenance do not establish historical release/verification times. `AGENT_ENABLED` defaults false; never select a live model or make paid calls without the model/spend decision. Strict schemas and tool evidence do not guarantee factual prose; manual live acceptance remains required. Concurrency limits are per process and are not a global spend cap.

## Commit & Pull Request Guidelines

Hyperliquid revision `0003` has a dedicated perpetual catalog, funding facts/window audits, and immutable OI receipts/collection audits. Preserve exact event milliseconds and one settlement per derived UTC hour. Funding corrections update current values; unchanged provenance and omitted facts remain intact. Shifted source event identity in a stored hour fails for inspection. Resume requires a complete latest successful historical read and matching stored counts; refetch gaps/latest failures and the current month. Keep writer exclusion independent by job type and commit facts/success counts atomically after HTTP. Reader grants remain SELECT-only; OI facts are INSERT-only for the writer. Reader queries use read-only repeatable snapshots, Decimal strings, half-open UTC windows and dataset-bound keyset cursors; OI ties use receipt time plus identity. Funding gaps withhold full-window sums; OI counts describe observed receipts, never historical calendar completeness. Distinguish age thresholds from scheduling. Tests use only isolated PostgreSQL and synthetic provider responses.

Use concise, imperative commit subjects, such as `Add initial data ingestion module`. Keep commits focused.

Pull requests should explain the change, its purpose, and validation performed. Link relevant issues, identify configuration changes, and include screenshots for visible interface changes.

Commit file additions, edits, and removals promptly in small, coherent batches as work progresses. Run checks appropriate to each batch and review staged changes for credentials and private files before committing. Do not wait for an entire feature or milestone to finish. Each commit should describe a reviewable change and accurately state any pending acceptance checks. Pushes, pull requests, and merges still require user authorization. Keep personal workflow preferences and session-specific authorization in ignored local notes.

## Security & Configuration

Never commit credentials, keys, private datasets, dumps, or personal prompts. `.env.example` contains placeholders; actual configuration belongs in ignored `.env` or runtime environment. Supply only needed credentials: administrator for bootstrap/migrations, writer for ingestion, reader for API/agent. Only the API receives optional OpenAI configuration. Market endpoints and deterministic tests need no real OpenAI key. Do not persist conversations or log full prompts/answers, authorization headers, or SDK payloads. Exclude secrets from Docker context and Git. Update this guide and README when commands, layout, or dependencies change.
