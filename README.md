# AI Market Intelligence Platform [WIP]

A market research platform being built to collect historical data, produce reproducible analysis, and answer questions grounded in stored market observations.

**Current state: Milestone 1 foundation, Coinbase ingestion, and historical query/API implemented and verified.** Real five-minute history from 2020-01-01 is retained locally, with source gaps reported explicitly. The read-only HTTP API serves stored candles, derived bars, coverage, latest observations, and deterministic summaries. The AI agent is the remaining Milestone 1 checkpoint.

The approved data contract is **Coinbase Exchange spot BTC/USD, completed five-minute candles, and an initial backfill from 2020-01-01**, with earlier dates configurable subject to source availability. Retain ingested history without a rolling retention limit. Fifteen-minute, hourly, and daily bars will be derived from the canonical five-minute observations.

- [ARCHITECTURE.md](ARCHITECTURE.md): reviewed design, semantics, trade-offs, and full Milestone 1 acceptance criteria.
- [AGENTS.md](AGENTS.md): contributor commands, scope, and conventions.

## Start the local environment

Run commands from the repository root. Start Docker Desktop with Linux containers and Docker Compose v2. A host Python 3.14 interpreter is needed for credential generation; application containers use Python 3.14.8 and PostgreSQL 18.6, with both images pinned by digest. Host development supports Python 3.14.x.

If Docker Desktop reports **WSL update required** on Windows, update WSL with `wsl --update` and restart Desktop before running Compose commands.

Generate separate random local passwords without printing them:

```powershell
python scripts/init_local_env.py
```

The script creates an ignored `.env` and refuses to overwrite an existing one. Defaults are database `market_intelligence`, host `127.0.0.1`, port `55432`, and roles `market_admin`, `market_ingest`, and `market_reader`. Adjust the ignored file if the local port is occupied. `.env.example` contains placeholders; copying it without replacing them will not work.

Build, start PostgreSQL, initialize the schema/roles, and verify reader access:

```powershell
docker compose config --quiet
docker compose build migrate
docker compose up -d --wait db
docker compose run --rm migrate
docker compose run --rm check
```

Expected final output: `Database ready: revision=0001, markets=1, role=read`.

`init-db` provisions restricted roles, applies Alembic revisions, and installs grants. Rerunning it does not duplicate reference data and applies current ingestion/reader passwords. Changing the administrator password in `.env` does **not** rotate an already initialized volume's password. Use this bootstrap only for the project's dedicated local database.

The development database persists in named volume `ai-market-intelligence_postgres_data` and binds only to localhost. PostgreSQL 18 stores its cluster under `/var/lib/postgresql/18/docker`; Compose mounts the volume at `/var/lib/postgresql`.

The localhost binding is the intended access boundary, not a security guarantee on every Docker version. Docker documents that Engines older than 28.0.0 can expose localhost-published ports to hosts on the same layer-2 network. Update Docker Desktop to a supported release with Engine 28 or newer before relying on this boundary; keep PostgreSQL protected by authentication and host network controls. See [Docker port-publishing guidance](https://docs.docker.com/engine/network/port-publishing/).

## Remote demos

The HTTP API binds to localhost and is intended for local development. A remote demo should expose only the app through authenticated HTTPS, using an access-controlled tunnel or a separate hosted environment. Keep PostgreSQL and the Docker daemon private. Before inviting testers, add application authentication, request limits, and model-spend limits at the relevant checkpoint. Sharing a Git repository lets others run their own local copy with independently generated credentials and their own initially empty database. Never share your `.env`.

## Schema and access

| Table | Purpose |
| --- | --- |
| `data_sources` | Seeded Coinbase Exchange identity |
| `assets` | Seeded BTC and USD identities and kinds |
| `markets` | Seeded Coinbase `BTC-USD` market and base/quote units |
| `ingestion_runs` | Request windows, lifecycle, counts, and audit identity |
| `candles` | Exact OHLCV, five-minute UTC starts, and ingestion provenance |

PostgreSQL enforces identity, unique candle grain, five-minute alignment, `numeric(38,18)`, finite positive prices, finite non-negative volume, OHLC bounds, and run windows/lifecycle/counts. Each candle links to a run for the same market and interval through a composite foreign key. Ingestion validates naive timestamps, excess decimal scale, closed-candle eligibility, and payloads before persistence; PostgreSQL can coerce timestamps or round decimals, so these checks precede inserts.

| Role | Grants |
| --- | --- |
| Administrator | Local bootstrap, schema ownership, migrations, reference seeds, and grants |
| Ingestion | SELECT on foundation tables; INSERT/UPDATE on candles and ingestion runs |
| Reader | SELECT on foundation tables only |

Both restricted roles can read the migration revision, cannot delete rows or create permanent/temporary tables, and have no elevated role flags. The check container receives only reader credentials. The future API/agent will use this reader role.

## Load and refresh historical candles

After building and initializing the database, start with a small live range and replay it:

```powershell
docker compose run --rm ingest ingest --start 2024-01-01 --end 2024-01-02
docker compose run --rm ingest ingest --start 2024-01-01 --end 2024-01-02
```

The first run inserts available observations. An identical replay reports them as `unchanged`, without changing candle provenance. Provider corrections update only affected observations and preserve their first ingestion time. These commands call the public Coinbase Exchange API without a key or paid data subscription; they write real candles to the local development volume.

Load history from the approved default start, skipping previously successful matching month ranges:

```powershell
docker compose run --rm ingest ingest --resume
```

The default start is **2020-01-01 UTC**. The exclusive default end is the five-minute boundary at or before **current UTC time minus 60 seconds**. This excludes unfinished candles and allows a short settling period. The range is divided at UTC calendar-month boundaries; the first and last chunks are clipped. Requests contain at most 250 five-minute buckets, below Coinbase's 300-candle limit, and start at least 0.5 seconds apart. A multi-year load makes thousands of sequential requests and can take tens of minutes; progress appears after each committed month.

To load an earlier period, refresh the most recent 72 hours, or repair a particular range:

```powershell
docker compose run --rm ingest ingest --start 2017-01-01 --end 2017-01-02
docker compose run --rm ingest ingest --refresh
docker compose run --rm ingest ingest --start 2024-01-01 --end 2024-02-01
```

Earlier history is subject to source availability. Date-only arguments mean midnight UTC; full timestamps must include `Z` or an explicit UTC offset. Both boundaries must align to five minutes, and `--end` is exclusive. An explicit end after the closed-candle cutoff is rejected. `--start` and `--refresh` are mutually exclusive. No scheduler or automatic refresh runs in the background.

Each month gets a committed `running` audit before its first HTTP request. The entire chunk is fetched and validated before one transaction writes candles and marks the run successful. Malformed rows or conflicting duplicates fail the chunk. Earlier months remain committed. Failure output includes a safe error code, failed range, audit identity where available, and recovery instructions; it excludes provider bodies and credentials. If the failure audit cannot be written, `audit_recorded` is false and the original run can remain `running`.

`--resume` skips only **exact matching successful chunk ranges** and reports current stored coverage for them. A successful run can contain absent buckets: it means processing succeeded, not that Coinbase supplied every observation. Missing buckets remain absent. Do not use resume to repair a successful but gapped chunk; request that range again without `--resume`. A later default end will reprocess the changed final month. For an exact restart or repeatable comparison, reuse an explicit `--start` and `--end`.

Progress is newline-delimited JSON. `expected` counts requested five-minute buckets; `received` counts distinct validated observations after filtering. A fetched chunk's `missing_buckets` uses provider coverage; a skipped chunk uses stored coverage, as identified by `coverage_basis`. The final summary keeps those totals separate. Stored observations absent from a later provider response are retained. API coverage calculations will independently inspect stored rows at the query checkpoint.

Transient connection failures, timeouts, HTTP 429, and selected 5xx responses get up to five attempts with backoff/jitter and valid `Retry-After` guidance. Connect/read timeouts are bounded; the default fetch/validation budget is 600 seconds per chunk. `--max-seconds` adds an optional command budget, checked between requests and chunks. A synchronous request or database operation already in progress must return before its deadline is checked. `--request-interval` accepts 0.1–60 seconds; the default remains conservative. One advisory lock prevents cooperating ingestion jobs from writing concurrently. Graceful interruption records failure where possible; after an abrupt termination, resume retries unfinished ranges.

Live contract check on 2026-10-05: product metadata identified BTC as the base asset and USD as the quote asset. For **11:55–12:00 UTC**, the candle's volume **17.88862151** exactly matched the sum of **1,310** public trades' BTC `size` values. This validates base-volume interpretation for that sample; it does not establish uninterrupted historical coverage. Wire order, historical omissions, and request limits follow the [Coinbase candle reference](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles).

The implemented adapter also fetched and validated all **12 expected candles** for 00:00–01:00 UTC on both **2020-01-01** and **2024-01-01**, without authentication. The complete deterministic suite passed **113 tests** against PostgreSQL 18.6, including actual ingestion-role transactions. A live 2024-01-01 00:00–01:00 UTC ingestion inserted 12 candles; replaying it reported 12 unchanged, zero inserts, and zero updates.

Historical verification on **2026-10-05**:

- The initial **2020-01-01 00:00 to 2026-10-05 14:35 UTC** range completed in **82 successful chunks**. Independent reader-role queries found **710,891 stored observations**, **711,247 expected buckets**, and **356 absent buckets**.
- Exact-range resume skipped all 82 chunks with zero writes and still reported 356 stored gaps. It did not fetch the skipped source windows.
- A normal 72-hour refresh through **15:00 UTC** received all 864 expected candles: **5 inserts, 859 unchanged, zero updates, zero gaps**.
- After refresh, the stored **2020-01-01 00:00 to 2026-10-05 15:00 UTC** range contained **710,896 of 711,252 expected candles** (about **99.95% coverage**). The latest candle opens at **14:55 UTC**. All 85 audit runs succeeded; none remained failed or running.

The 356 absent buckets remain real coverage gaps in stored history. A separate narrow raw request for **2020-01-30 17:00–18:40 UTC** also returned no observations inside that 20-bucket gap, confirming that sample was not lost at an ingestion page boundary. The reason for source omissions cannot be determined from OHLCV alone. These measurements describe this local snapshot; future provider corrections or explicit repairs can change it. Dataset and verification logs stay outside Git.

## Query stored history through HTTP

After database initialization, start the API:

```powershell
docker compose up -d --wait api
```

Open [interactive API documentation](http://127.0.0.1:8000/docs). Readiness checks database access and revision `0001`; an unavailable database or unexpected revision returns 503. Liveness does not depend on the database. The API receives only reader credentials, creates no schema or data, and makes no Coinbase or OpenAI calls. Run migrations explicitly before starting it.

Examples using seeded market id `1` (discover ids through `/v1/markets`):

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/v1/markets'
Invoke-RestMethod 'http://127.0.0.1:8000/v1/markets/1/latest'
Invoke-RestMethod 'http://127.0.0.1:8000/v1/markets/1/summary?start=2024-01-01&end=2024-01-02'
Invoke-RestMethod 'http://127.0.0.1:8000/v1/markets/1/candles?start=2024-01-01&end=2024-01-02&interval_seconds=3600&limit=24'
```

Dates mean midnight UTC; timestamps require an offset. Windows are half-open and align to five minutes. Derived output intervals are **900, 3600, or 86400 seconds**; their windows must align to that interval's UTC epoch grid (UTC midnight for daily bars). Rows are ascending; the page limit is 1–500 (default 200). Send `next_cursor` as `cursor` with the same market, start/end, and interval for another page; URL-encode it with your HTTP client's parameter builder. `next_cursor: null` ends pagination. Absent derived groups remain gaps; partial groups include constituent counts and `null` full-bar OHLCV.

Each page reports coverage of the **entire requested canonical window**, independently of page length. Every response reads one repeatable, read-only snapshot; separate pages use separate snapshots, so restart pagination if ingestion corrections occur between pages. Cursors are validated continuation positions, not authorization tokens.

Summaries calculate the first required bucket's open, last required bucket's close, high/low, summed BTC volume, and open-to-close return. If any required bucket is absent, all full-window metrics are `null`. Coverage is `incomplete` with some rows or `no_data` with zero rows. Missing five-minute ranges are coalesced, with at most 50 details, total range count, a truncation flag, and exact missing bucket count. Provenance includes first/last ingestion times and contributing run counts; canonical candles also expose their last ingestion run id.

Decimal prices and volume serialize as **JSON strings**, preserving precision. Return percentages and coverage ratios use eight decimal places with Decimal `ROUND_HALF_EVEN`; prices and volume are not rounded for presentation. Latest results report age from the candle's end to server UTC time and mark it stale when age exceeds 900 seconds. Only observations eligible under the existing completed-candle cutoff are returned. Future windows report absent coverage.

Optional `.env` settings: `API_PORT=8000`, `API_MAX_WINDOW_DAYS=3653`, and `API_STALE_AFTER_SECONDS=900`. The roughly ten-year request-width limit is independent of retention or how old requested dates are. Invalid ranges/intervals/cursors return 422, unknown market ids return 404, and unavailable database reads return a sanitized 503. The database pool waits at most five seconds for a connection, connect timeout is five seconds, and each SQL statement has a fifteen-second timeout.

The host equivalent is `.venv\Scripts\python.exe -m market_intelligence serve` after installing locked dependencies and starting PostgreSQL. Stop only the API with `docker compose stop api`. After code/dependency changes, rebuild with `docker compose build migrate` and recreate it with `docker compose up -d --wait api`.

Opt-in check against an already backfilled local database:

```powershell
.venv\Scripts\python.exe scripts/check_market_api.py
```

This checks HTTP health, both 2020-01-01 and 2024-01-01 summaries, their 24 derived hourly bars, and latest freshness. It performs no database writes or external provider calls. It needs the documented backfill and a running API; it is separate from deterministic tests.

Query/API verification on **2026-10-05**: the full isolated PostgreSQL suite passed **165 tests** (81 unit and 84 integration). HTTP summaries for both sampled days matched independent reader-role SQL for opening/closing prices, return, high/low, and volume. The known **2020-01-30 17:00–18:40 UTC** gap returned `no_data`, 20 missing buckets, and unavailable metrics. The full stored range through **2026-10-05 15:00 UTC** returned `incomplete`, **710,896 actual buckets and 356 missing**, matching SQL; its summary request plus independent SQL check took about 0.85 seconds in this local sample. A multi-year daily request returned 500 rows and a continuation cursor. Latest correctly reported stale data after the last manual refresh. These are local observations, not throughput guarantees; the API checks did not alter the stored dataset.

## Run tests

The full suite uses a separate container, network, database name, and memory-backed PostgreSQL storage, with no published port or development volume. Fixtures refuse integration tests unless host is `db-test` and database is `market_intelligence_test`.

```powershell
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yaml down
```

The first command returns the test runner's exit code. The second removes only test containers/network; run it even after failure. Expected rejection errors appear in test database logs. Each container start initializes an empty database and applies the same migration as development. Fixtures use synthetic candles and roll back their writes; tests that exercise committed monthly ingestion explicitly clean their rows in this isolated database.

The deterministic suite covers configuration/secret masking, migration/schema agreement, downgrade/reapply, repeatable bootstrap, exact decimals/UTC, constraints/FKs, transaction rollback, and actual role permissions. Ingestion tests add payload ordering/filtering, numeric validation, paging, retries, deadlines, gaps, duplicate handling, replay, corrections, atomic writes/audits, failed-month preservation, interruption, writer locking, and resume. PostgreSQL ingestion tests use the actual restricted ingestion role with a fake HTTP transport. No Coinbase or OpenAI calls are required; live checks are separate manual operations.

Query/API tests add hand-calculated analytics, complete/incomplete derived bars, half-open UTC windows, coalesced and truncated missing ranges, cursor binding and pagination across gaps, exact decimal JSON, latest staleness, controlled HTTP errors, and consistent snapshots during concurrent corrections. They use actual reader credentials and committed synthetic fixtures that are cleaned only in the guarded test database.

To reproduce the two historical live adapter samples after installing the host dependencies:

```powershell
.venv\Scripts\python.exe scripts/check_coinbase_live.py
```

This opt-in check calls Coinbase for 00:00–01:00 UTC on 2020-01-01 and 2024-01-01 through the real adapter. It verifies product identity, parses/validates candles, and prints expected/validated/missing counts. Each window has 12 expected candles. Samples remain in memory; the script never loads database credentials or writes to PostgreSQL. It returns nonzero on a provider/validation failure or missing sampled buckets. It is separate from pytest and does not certify coverage outside those two hours.

## Host development and quality checks

With Python 3.14 installed, bootstrap project-local uv 0.12.23 and install the locked environment:

```powershell
python -m venv .tools
.tools\Scripts\python.exe -m pip install uv==0.12.23
.tools\Scripts\uv.exe sync --locked --no-python-downloads
```

If uv is already installed, `uv sync --locked --no-python-downloads` is equivalent. Environments and caches are ignored. Runtime dependencies include SQLAlchemy Core, psycopg, Alembic, Pydantic Settings, HTTPX, FastAPI, and Uvicorn; pytest, Ruff, and mypy are development dependencies. The OpenAI SDK will be added at the agent checkpoint.

```powershell
.venv\Scripts\ruff.exe format --check src migrations tests scripts
.venv\Scripts\ruff.exe check src migrations tests scripts
.venv\Scripts\mypy.exe
.venv\Scripts\python.exe -m pytest -m "not integration"
git diff --check
```

The same database commands can run on the host after starting PostgreSQL:

```powershell
.venv\Scripts\python.exe -m market_intelligence init-db
.venv\Scripts\python.exe -m market_intelligence check-db
.venv\Scripts\python.exe -m market_intelligence check-db --role ingest
.venv\Scripts\python.exe -m market_intelligence ingest --start 2024-01-01 --end 2024-01-02
```

For schema changes, update Core metadata, generate/review an Alembic revision, then apply it with `init-db` so grants are reapplied. Review PostgreSQL alignment checks carefully: autogeneration can escape `%` as `%%`. Keep applied revisions immutable. `.venv\Scripts\alembic.exe upgrade head --sql` generates offline SQL without credentials. Verify migrations against PostgreSQL rather than SQLite.

## Stop and recover

```powershell
docker compose stop db
docker compose up -d --wait db
```

Stopping/restarting preserves the volume; `docker compose down` also preserves it. **`docker compose down --volumes` deletes the development database**; use it only for an intentional reset after preserving needed data. Keep `.env` privately alongside the retained volume because it contains the generated credentials.

If a job reports database failure, confirm Docker is running, inspect service health with `docker compose ps`, check the local port, and rerun `migrate` with the volume's credentials. `check` requires completed migrations. Rebuild `migrate` after runtime/dependency changes and rebuild the test image after test changes.

Never commit `.env`, keys, dumps, local datasets, private prompts, or secret-bearing logs. Docker context excludes secrets, environments, datasets, caches, and Git history; images copy selected files and run application commands as a non-root user. Avoid displaying resolved Compose configuration because it contains passwords; use `config --quiet`. Review files before any commit. Do not stage, commit, or push without an explicit request.

## Next checkpoint

Ground the agent's read-only tools in the implemented query functions. Choose the OpenAI model and smoke-test spending at that checkpoint. Milestone 1 is complete only when the entire slice meets its acceptance criteria.
