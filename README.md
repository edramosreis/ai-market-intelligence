# AI Market Intelligence Platform [WIP]

A market research platform being built to collect historical data, produce reproducible analysis, and answer questions grounded in stored market observations.

**Current state: Milestone 1's local vertical slice is implemented and verified.** Real five-minute history from 2020-01-01 is retained locally, with source gaps reported explicitly. The read-only API serves stored candles, derived bars, coverage, latest observations, and summaries. Deterministic agent tests execute those same queries through simulated model responses; a separate live demonstration with `gpt-6-luna` passed manual evidence and answer inspection.

Treasury ingestion, read-only curve queries, and agent tools are also implemented: daily nominal par yields with native dates, exact percentage values, explicit missing reasons, and current-value provenance. Deterministic agent tests and a separately inspected live acceptance batch verify curves, paginated spreads, mixed-source evidence, and controlled limitations.

The next macro component has a reviewed direct-source contract: BLS monthly CPI and
unemployment, followed by the Federal Reserve Board's native monthly effective federal
funds rate. Public source checks verified their observation history and explicit missing
values. Both native provider clients and domain models are implemented with synthetic
tests. Migration `0004` adds dedicated macro catalog, receipt audits, immutable observed
versions/footnotes and current-version pointers. Atomic persistence preserves unchanged
provenance, appends meaningful value/missing/footnote changes and reports omitted periods
without removing retained history. Delayed older overlapping receipts are rejected.
Manual `ingest-bls` and `ingest-fed` jobs connect those providers to storage, with
native history defaults, bounded reads, explicit refresh/resume and controlled failures.
Read-only HTTP queries expose the fixed catalog, current monthly observations, latest
stored month and locally observed versions. Three narrow agent tools reuse those readers. The
design retains current historical values and corrections observed after local collection;
it does not establish what was known before collection. See
[the macro source contract](ARCHITECTURE.md#14-direct-monthly-macro-source-contract).

The opt-in `.venv\Scripts\python.exe scripts/check_bls_live.py --year 2024` checks one
native year using the keyless API. It performs no database writes, saved downloads or
model calls; the unregistered BLS API's request quotas apply. Existing locked dependencies
suffice. Month identity, Decimal values, source-dash missing reasons and footnotes are
retained; annual averages are excluded and counted. Local fetch/receipt times are not
publication times. The separate `.venv\Scripts\python.exe scripts/check_fed_live.py --year 2024`
reads the full H.15 ZIP/XML in bounded memory and returns the selected monthly-rate year,
preserving native month-end labels, release prepared text and source annotations. No
archive is extracted or saved; remote schemas are not loaded. A live 2024 sample through
each implemented client returned 12 available months per series on 2026-10-08. This
verifies the provider boundary, not historical publication/vintage coverage.
All 977 isolated deterministic tests (623 unit / 354 integration), Ruff and strict
mypy pass. This includes 124 native provider/domain cases and 64 new schema/persistence
cases covering constraints, restricted roles, migration preservation, replay, corrections,
omissions, independent locks, rollback, delayed receipts and reader snapshot consistency.
The writer has INSERT-only access to immutable versions/footnotes; current rows reference
those versions rather than duplicating their content. Local collection times establish
locally observed states, never retrospective publisher vintages. Revision `0004` is also
applied to the local development database; new installations must apply it before API startup.
Another 61 job/CLI cases verify native request windows, correction-aware resume,
request limits, real provider parsing through writer transactions, controlled failure
audits, earlier committed chunks and pre-configuration validation. Another 68 reader/API
cases verify monthly coverage, native evidence, cursors and consistent correction snapshots.
Another 71 agent cases verify strict native arguments, server-issued continuations,
exact SDK/HTTP/reader evidence, gaps, observed corrections and shared call budgets.
See [manual macro ingestion](#ingest-native-monthly-macro-history) and
[stored macro queries](#explore-stored-monthly-macro-data).

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

Expected final output: `Database ready: revision=0004, markets=1, role=read`.

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

Both restricted roles can read the migration revision, cannot delete rows or create permanent/temporary tables, and have no elevated role flags. The check container receives only reader credentials. The API and agent use this reader role.

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

Open [interactive API documentation](http://127.0.0.1:8000/docs). Readiness checks database access and revision `0001`; an unavailable database or unexpected revision returns 503. Liveness does not depend on the database or model configuration. The API uses reader database credentials and creates no schema or data. Market GET endpoints make no Coinbase or OpenAI calls; the optional agent POST is described below. Run migrations explicitly before starting it.

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

## Ask questions through the agent

`POST /v1/agent/query` accepts one JSON question and returns `status`, `answer`, exact server-collected `evidence`, `limitations`, the configured `model`, retrieval time, and request/tool counts. Each request starts a fresh conversation. Inspect its schemas in [Swagger UI](http://127.0.0.1:8000/docs).

The model selects from ten application-owned tools, backed by the same reader-role queries as the HTTP API:

| Tool | Inputs and evidence |
| --- | --- |
| `get_latest_btc_candle` | No arguments; latest stored completed Coinbase spot BTC/USD candle, age and staleness |
| `get_btc_window_summary` | Inclusive `start`, exclusive `end`; UTC dates or aligned timestamps, exact metrics and candle coverage |
| `get_treasury_curve` | `observed_on` as YYYY-MM-DD; fourteen nominal tenors, missing reasons, provenance and same-date 10Y–2Y spread |
| `get_treasury_spread_history` | Date-only `start`/`end` and required nullable `cursor`; up to twenty stored source dates per page, 2Y/10Y yields, spreads and provenance |
| `get_latest_btc_funding` | No arguments; latest stored settled Hyperliquid BTC perpetual funding event, exact source milliseconds, derived UTC settlement hour and staleness |
| `get_btc_funding_summary` | Inclusive `start`, exclusive `end`; complete UTC-hour windows from 2024, arithmetic rate sum/percent/mean and gap coverage |
| `get_latest_btc_open_interest` | No arguments; latest stored Hyperliquid BTC perpetual OI receipt, BTC quantity, USDT context prices, local receipt identity/time and staleness |
| `get_latest_macro_observation` | Fixed `series_id`; latest stored completed month, native value/units, month lag and original content receipt |
| `get_macro_observation_history` | Fixed `series_id`, monthly `start`/`end`, nullable `cursor`; twenty current observations per page, whole-window monthly coverage, footnotes and receipts |
| `get_macro_observed_versions` | Fixed `series_id`, exact `month`, nullable `cursor`; twenty locally observed immutable versions per page, current version number and content-origin receipts |

Macro series are BLS `CUSR0000SA0` CPI index (1982–84=100), BLS `LNS14000000`
unemployment percent, and FRB `RIFSPFF_N.M` monthly effective federal funds percent per
annum. BLS series are seasonally adjusted; the Fed series is not. Monthly labels use
completed `YYYY-MM-01` periods and half-open windows. CPI levels are not inflation rates.
Latest means latest stored, with explicit month lag, including an unavailable value;
it does not establish publisher-current data or verified publication delay.

History and locally observed versions begin with cursor null. Continue only with the
latest server-issued token for the same series/window or series/month. Tokens cannot
cross tool kinds or questions, and completed queries retire them. Unfinished chains
return `partial_results`; pages are separate snapshots. Whole-window coverage counts
represented source-missing months separately from absent stored months. Empty storage,
gaps and source-dash values return controlled limitations rather than fabricated values.
Local versions/receipt times do not establish retrospective release vintages or past
market knowledge. Original content receipts remain attached after unchanged replay or
omission. No agent tool fetches providers, initiates ingestion, computes inflation,
forward-fills or aligns sources automatically.

Macro agent verification on **2026-10-09**: **977 deterministic tests** pass
(**623 unit / 354 integration**), plus Ruff and strict mypy over 110 files. The 71 new
cases include real reader-role PostgreSQL queries through HTTP and synthetic replies
through the official SDK. They verify native units/month labels/notes, unavailable latest
values, uncollected months, twenty-row continuations, shared budgets and exact evidence
after correction/replay/omission during model waits. Reader connections are released
before those waits. This deterministic checkpoint used no live provider/model calls.
The persistent agent remains disabled.

Separate macro live acceptance on **2026-10-09** used `gpt-6-luna`, the actual agent
HTTP handler and reader-role queries. Eleven cases passed exact tool-evidence comparison
against the native HTTP readers and separate manual inspection:

| Cases | Accepted behavior |
| --- | --- |
| Three native 2024 histories | CPI index, unemployment percent and monthly effective federal funds percent per annum; correct adjustment, periods and collection-time limits |
| Latest stored CPI and Fed observations | Actual stored month and month lag, without publisher-current or publication-delay claims |
| Empty and partly collected windows | Distinct `no_data`/`incomplete` limitations, retaining requested bounds and absent months |
| Twenty-row history continuation | All 24 months retrieved across two pages with exact server-issued cursors |
| History exceeding the shared budget | Three pages retain 60 of 72 months; `partial_results` and continuation evidence remain explicit |
| Locally observed version history | Exact native content and original collection/materialization receipt, without historical publisher-vintage or market-knowledge claims |
| BTC/Treasury/CPI question | Three independently attributed sources and native windows/units within the existing shared budget |
| Source-marked missing CPI month (2026-10-10 follow-up) | Actual 2025-10 source dash, native footnote/receipt and `missing_values`, without zero or substituted data |

No production-code repair was needed. All 13 development fact/catalog/audit table
fingerprints stayed unchanged, and no reader connection was held during model calls.
The eleven initial cases made no provider requests and preserved the then-deferred BLS
backfill. After BLS collection, the twelfth scoped case on **2026-10-10** passed exact
native HTTP evidence comparison and manual inspection of the controlled source-missing
response. It used one model request and one reader tool; the server returned
`missing_values` without a model prose continuation. All 13 table fingerprints stayed
unchanged. The cumulative macro acceptance ledger is closed; the persistent agent remains
disabled. Deterministic tests also cover this behavior and corrections. Inspection of
these bounded cases does not guarantee arbitrary answer prose.

Application code fixes the Coinbase market, Treasury dataset, and native Hyperliquid BTC perpetual. Server validation rejects unknown functions, extra/duplicate arguments, unsupported date/timestamp formats, invalid cursors, and oversized windows. The history tool starts with `cursor: null`, then follows returned `next_cursor` values with unchanged bounds. Its strict schema offers only null and the latest server-issued continuation for each queried window, avoiding transcription of opaque tokens. Whole-window coverage counts stored source dates and all normalized tenors, not calendar completeness or only benchmark rates; only each page's observations identify its returned dates. Separate pages are separate snapshots. An answer is accepted only after a data tool executes; its separate evidence preserves exact decimals, source, period, provenance, and coverage. The agent cannot run SQL, ingest, browse, write, or choose another dataset.

Treasury yields are nominal percentages; 10Y-minus-2Y spreads are signed percentage points and basis points. Zero remains a value; missing reasons remain unavailable. Monthly read audits are feed-fetch evidence, not release timestamps or per-row verification times. The agent uses current corrected values, without historical vintages, implicit forward-fill, or calculations aligning Treasury dates with BTC timestamps.

Hyperliquid funding remains signed hourly fractions; positive rates mean longs pay shorts. The summary returns an arithmetic rate sum, its percent representation, and a rounded mean; missing settlement hours withhold these metrics. No annualization, compounding, position PnL, or historical revision reconstruction is supplied. OI is a stored local receipt in BTC with mark/oracle prices in USDT. It has no source event timestamp or historical hourly completeness. USDT denomination remains distinct from USDC collateral/settlement. Stale latest funding/OI and absent/gapped funding produce source-specific server-written limitations with evidence, without another model request. No agent tool collects data or answers historical OI from a current receipt.

Hyperliquid agent integration verification on **2026-10-07**: **589 deterministic tests** pass (**348 unit / 241 integration**), with Ruff and strict mypy over 77 files. The three new tools execute actual reader queries through the HTTP handler and match the existing funding/OI API evidence and exact SDK outputs. Tests retain signed/zero values, source milliseconds and local OI identities, validate UTC-hour/offset windows, stop on absent/gapped/stale data, enforce shared three-call/four-request and output bounds, and sanitize SQL failures. Fact/audit counts stay unchanged during agent reads. Reader connections are released before model calls; collected evidence survives later funding corrections or new OI receipts. Tests use the real SDK with synthetic in-memory model replies and isolated PostgreSQL, without live source/model calls.

Hyperliquid live agent acceptance on **2026-10-08** used `gpt-6-luna`, the actual HTTP handler, and reader-role queries. All nine cases passed exact evidence comparison against the existing HTTP readers and separate manual inspection:

| Case | Inspected behavior |
| --- | --- |
| January 2024 funding | 744/744 hours; arithmetic sum 0.0242847281 fraction, 2.42847281%, and exact server-calculated mean; no compounding or PnL claim. |
| Negative funding hour | [2024-01-15 21:00, 22:00) UTC; signed fraction and percent retained with complete coverage and the funding direction explained. |
| Gapped funding window | [2024-08-15 12:00, 15:00) UTC; 2/3 hours observed, with full-window metrics unavailable. |
| Entirely absent funding hour | [2024-08-15 13:00, 14:00) UTC; `no_data`, unavailable metrics, and no substitution of zero. |
| Stale latest funding | Exact stored event evidence retained; server-written stale limitation requires a manual funding update. |
| Stale latest OI | Exact receipt evidence retained; server-written stale limitation distinguishes local receipt time from exchange event time. |
| Three-source history | Coinbase January 1 high/low/volume, Treasury January 2 spread, and Hyperliquid January 1 funding; separate dates/windows, units and coverage within three tools/four requests. |
| Fresh latest funding | Settled fraction, signed premium, exact source milliseconds, derived UTC hour and age matched evidence. |
| Fresh latest OI | BTC quantity, USDT context prices, local fetch/receipt times, snapshot identity and age matched evidence; no exchange timestamp or historical completeness was invented. USDC collateral/settlement remained distinct. |

Stale checks preceded one manual recent funding update and one OI collection. The update returned 23 events, inserting 22 and preserving one unchanged; the collection appended one receipt. Final retained counts were 24,275 funding events/35 successful funding audits and two OI receipts/two collection audits. The known August 2024 omission remains unavailable. Every agent case preserved all eight fact/audit table fingerprints, and no reader connection was held during model calls. Existing Coinbase/Treasury values and provenance remained unchanged. No production fixes were needed. The separate batch closed after 16 generation requests with zero unreconciled usage; its conservative token-based estimate was below US$0.023, not a billing invoice. The persistent agent remains disabled. These inspected examples do not guarantee factual prose for arbitrary questions.

The agent is **disabled by default**, returning a sanitized 503 while market endpoints keep working. The initial live demonstration used `gpt-6-luna`; model choice remains configurable and access depends on the account. Configure `OPENAI_MODEL` and your `OPENAI_API_KEY` in the ignored local `.env`, and set `AGENT_ENABLED=true` when deliberately enabling paid requests. Keep the key out of Git and chat. Only the API service receives these variables. Rebuild and recreate it after configuration/code changes:

```powershell
docker compose config --quiet
docker compose build migrate
docker compose up -d --wait api
```

Once enabled, this example makes paid OpenAI calls:

```powershell
$agentQuestion = @{ question = 'What were the BTC/USD high, low, and BTC volume on January 1, 2024, in UTC?' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/agent/query' -ContentType 'application/json' -Body $agentQuestion
```

Empty/gapped windows and stale latest candles return `status: limited`, a server-written explanation, and collected evidence without another model request. Full-window metrics remain unavailable when candles are missing. Invalid tool requests, ungrounded model responses, exhausted budgets, and deadlines also return limitations. Model/database failures return 503 while retaining bounded evidence. Malformed questions return 422; bodies over 64 KiB return 413. A busy agent returns 503 immediately.

Treasury absent dates return a source-specific `no_data` limitation. Missing normalized rows return `incomplete`; a stored curve with no available yields or a spread page with unavailable benchmark inputs returns `missing_rates`. These cases retain evidence and stop further model requests. If the model finishes with an unfinished history cursor chain, the server replaces its answer with `partial_results` and retains the collected pages. Long histories can exceed the three-call budget; narrow the window rather than interpreting a partial page as a full-window calculation.

Treasury live acceptance on **2026-10-07** used `gpt-6-luna`, the actual agent HTTP handler, and reader-role queries. Exact evidence comparisons and manual inspection accepted six cases:

| Case | Observed result |
| --- | --- |
| 2024-01-02 curve | All fourteen tenor labels; thirteen available yields; 2Y 4.33%, 10Y 3.95%, and spread −0.38 percentage points / −38 basis points |
| January 1990 history | Twenty-one source dates across two pages; every reported date/spread matched the evidence |
| 2010-10-11 date-only entry | Fourteen `field_absent` yields, zero available rates, and `missing_rates` |
| Absent 1990-01-01 date | `no_data`, without inventing a holiday or another date |
| Separate BTC/Treasury observations | January 1, 2024 BTC high/low/volume and January 2 Treasury benchmarks matched their separate source/window evidence |
| January–April 1990 history | Three pages retained sixty of eighty-two stored dates; `partial_results` preserved the continuation cursor |

Early attempts exposed altered cursor strings and a prose claim of page overlap inferred from repeated whole-window coverage. The strict cursor choices and explicit page-versus-window instructions were added, then complete and bounded pagination were rechecked successfully. Database fact/audit counts remained unchanged, reader connections were released before model calls, and the persistent API stayed disabled. These are inspected examples, not a guarantee of factual prose for arbitrary questions.

Defaults allow **three sequential tools, four model requests, a 60-second execution budget, 2,048 output tokens per model request, 4,000 question characters, and 8,000 answer characters**. One agent request runs at a time per process. SDK retries are disabled; connect/pool waits are at most five seconds, and model I/O timeouts use the remaining budget. The deadline is checked between synchronous operations: in-flight I/O must return before it is checked, so this is not a guaranteed cancellation time. Model context, tool results, and provider responses are bounded at 128,000, 32,000, and 64,000 serialized bytes by default; oversized tool evidence is omitted. `.env.example` lists the `AGENT_*` controls and validation caps calls at three/four and time at 60 seconds. These are request limits, not a global monetary cap or public-deployment protection.

The official SDK uses Responses API strict function schemas and `store=false`; complete reasoning/function items and their outputs are relayed in memory for stateless continuation. The app does not persist conversations or log full questions, answers, credentials, or SDK payloads. Database transactions finish before model calls. See the [function-calling contract](https://developers.openai.com/api/docs/guides/function-calling) and [stateless reasoning guidance](https://developers.openai.com/api/docs/guides/reasoning).

Agent verification on **2026-10-05**: **237 deterministic tests passed** (147 unit and 90 integration) against PostgreSQL 18.6. The real OpenAI SDK uses an in-memory HTTP transport with synthetic replies, so tests need no real key/network/spend. Agent HTTP evidence matched hand-calculated query/API results exactly, with unchanged candle/audit counts and no checked-out database connection during model waits. Tests cover argument rejection, unsupported tools, stateless reasoning, empty/gapped/stale data, malformed responses, failures, concurrency, and execution/size limits. Formatting, linting, and strict type checks also pass.

Treasury agent integration verification on **2026-10-06**: the full suite passed **429 deterministic tests** (255 unit and 174 integration) against isolated PostgreSQL 18.6. Both Treasury tools executed actual reader queries through the HTTP handler; their evidence matched the shared query results and exact SDK tool outputs. Tests cover source-date/cursor validation, missing/null/zero yields, signed spread units, pagination and partial-result handling, mixed BTC/Treasury requests, output bounds, sanitized failures, and concurrent corrections. Fact/audit counts were unchanged by agent reads, and no reader connection was held during model calls. Ruff formatting/lint and strict mypy passed over 59 files. One existing TestClient deprecation warning remains. This uses synthetic model replies and establishes execution/evidence behavior; Treasury live model selection and prose acceptance remain pending.

Live verification on **2026-10-06** used the actual agent HTTP handler, official SDK, reader credentials, and `gpt-6-luna`. All **seven checks** matched `/latest` or `/summary` evidence using exact UTC windows and a shared real retrieval time per question. Manual inspection verified the source, dates, units, and every numerical claim, including rounded percentages:

| Check | Observed result |
| --- | --- |
| Latest before refresh | `limited` / `stale`, with the exact stored candle and age |
| Latest 24 hours before refresh | `limited` / `incomplete`; 32/288 candles and unavailable full-window metrics |
| 2024-01-01 UTC | Complete 288/288 coverage; high USD 44,240.80, low USD 42,175.65, volume 7,977.72851143 BTC |
| 2020-01-01 UTC | Complete 288/288 coverage; open USD 7,165.72, close USD 7,174.33, return +0.12015541% |
| 2020-01-30 17:00–18:40 UTC | `limited` / `no_data`; 0/20 candles, 20 missing buckets, and unavailable metrics |
| Latest after manual refresh | Close USD 86,241.41 for 2026-10-06 12:15–12:20 UTC; age about 329 seconds, below the 900-second stale threshold |
| Latest 24 hours after refresh | 2026-10-05 12:20 through 2026-10-06 12:20 UTC, complete 288/288 coverage; +0.16626352% return, correctly rounded to +0.1663% in the answer |

The manual 72-hour refresh received 864/864 candles, inserted 256, and retained 608 unchanged rows. Agent reads preserved candle/audit counts: 710,896/85 before refresh and 711,152/86 afterward. A separate provider HTTP 429 check returned controlled HTTP 503 / `model_unavailable`, with zero tool calls or data changes. The live batch used a private cumulative spend guard with standard processing and retries disabled; its conservative token-based cost estimate was below US$0.005. This is an upper estimate, not a reconciled billing invoice. The persistent local agent remains disabled.

These inspected examples establish the initial local demonstration, not general factuality across arbitrary prompts. Repeat them against current observations when demonstrating freshness, inspect returned evidence, and refresh ingestion manually as needed. Simulated tests establish tool behavior; systematic model evaluation remains a later checkpoint.

## Read Treasury source samples

The Treasury client reads the official [monthly XML feed](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed), which documents nominal curve history from 1990. It uses existing HTTPX and Python's XML parser, with no additional dependency or API key. It normalizes fourteen maturities from 1 month through 30 years, including the current 1.5-month field. `NEW_DATE` remains a source observation date; yields remain exact percentage values. Missing XML fields and explicit source nulls have distinct reasons, and zero is a valid nominal yield. The primary 30-year field is used without substituting the legacy display field.

Run after the existing locked dependency installation:

```powershell
.venv\Scripts\python.exe scripts/check_treasury_live.py
```

This opt-in check fetches January 1990, January 2020, and January 2024, validates the two-year and ten-year rates, and prints aggregate counts. It reads no local credentials and writes no database or downloaded fixture files. The standard application/test builds include the new package; the development image includes the sample script.

Initial verification on **2026-10-06**: the 2020/2024 sample months each returned **21 source dates and 42 available benchmark yields** through the actual client. Provider unit tests cover the boundary. Parsing rejects malformed/oversized XML, DTD/entities, duplicate/out-of-month dates, unexpected fields, and monthly pagination; HTTP behavior includes bounded streaming reads, sanitized errors, pacing, retries, and deadlines checked as each decoded transport chunk arrives. An in-flight synchronous read must still return before its deadline is checked.

These opt-in source checks perform no writes. Revision `0002` adds separate Treasury fact/audit tables and current-value correction semantics, preserving first/latest materialization provenance. PostgreSQL tests verify constraints, grants, migration compatibility, replay/corrections, omitted-date retention, atomic rollback, independent locking, resume, pagination, spreads, and concurrent-correction snapshots. Returned source dates do not establish a complete trading/publication calendar; weekends, holidays, and unavailable tenors must not be filled with fabricated observations.

After building the runtime image and running migrations as documented above, load Treasury data with the existing writer-only job:

```powershell
docker compose run --rm ingest ingest-treasury --start 2024-01-01 --end 2024-02-01
docker compose run --rm ingest ingest-treasury --resume
docker compose run --rm ingest ingest-treasury --refresh
```

Bounds are first-of-month dates with an exclusive end. Treasury's default history begins **1990-01-01**, with later starts configurable; the default end includes the current source month. Running `--resume` extends an existing 2020 backfill into the earlier history while reusing successful historical monthly reads. Coinbase BTC/USD continues to start in 2020 by default. Host execution uses `.venv\Scripts\python.exe -m market_intelligence ingest-treasury` with the same flags after locked dependency installation and database startup. No new service or dependency is needed.

Resume reuses validated historical feed reads only when stored dates still have all fourteen normalized tenor rows; it always refetches the current month. It does not certify calendar completeness or fetch later historical revisions. Refresh replays the previous/current months; combining `--refresh --resume` is rejected. Replay an older month without `--resume` to check corrections. If a replay fails, retry that exact month without `--resume`, then rerun the original command; an older success must not skip the intended correction fetch. Each source month commits its facts and success audit together; failures retain earlier months and record a sanitized failed audit. Unchanged facts preserve provenance. Dates omitted by a later response remain stored and are counted as `retained_dates`, without being marked freshly verified. `--month-seconds` and `--max-seconds` bound fetch/validation work, and `--request-interval` controls sequential request pacing. Scheduling remains manual.

## Explore stored Treasury curves

Use [Swagger UI](http://127.0.0.1:8000/docs) after the normal migration/API startup. The two routes share the reader-role query functions:

| Route | Inputs and result |
| --- | --- |
| `GET /v1/treasury/curve` | `observed_on=2024-01-02`; native-date rates, missing reasons, provenance, and same-date 10Y-minus-2Y spread |
| `GET /v1/treasury/curves` | Half-open `start`/`end` dates, `limit` (1–100, default 20), optional opaque `cursor`; observed curves plus whole-window stored-date evidence |

Try [a January 2024 curve](http://127.0.0.1:8000/v1/treasury/curve?observed_on=2024-01-02) or [a seven-curve page](http://127.0.0.1:8000/v1/treasury/curves?start=2024-01-01&end=2024-02-01&limit=7). Copy `next_cursor` into the same request's `cursor` parameter until it becomes null. A cursor is bound to the source/dataset/date window; separate pages are separate snapshots. Use YYYY-MM-DD dates without timestamps. Windows are bounded by `API_MAX_WINDOW_DAYS` and today's source date.

`stored` means all fourteen tenor rows are normalized, including unavailable tenors; `available_rates` counts actual values. An absent date returns `no_data`, and a missing stored tenor is `not_stored`. No holiday grid or forward-fill is implied. Yields are Decimal JSON strings in percent. The spread is ten-year minus two-year yield, expressed in percentage points and basis points (times 100), and is unavailable if either same-date input is missing. A negative spread is valid.

`retrieved_at` is the UTC query time. Per-rate provenance records first materialization and latest value/reason change. `latest_month_read` reports a validated source fetch, including retained dates omitted by that response; its timestamp does not certify each retained row's last source confirmation or a release time. Queries use today's stored corrections and cannot reconstruct overwritten values or what was known historically. Treasury endpoints do not make provider/model calls.

Local backfill verification on **2026-10-06**: **82 successful months**, **1,691 source dates** from **2020-01-02 through 2026-10-05**, and **23,674 normalized rate rows**: **21,691 available values**, **1,983 absent-field entries**, and no explicit source-null entries in this load. Independent reader SQL confirmed every stored date has fourteen rows and the existing **711,152 Coinbase candles** remain intact. This describes returned source history; publication-calendar completeness remains unestablished.

The extended **1990-01-01 to current-month** load on 2026-10-06 retained **9,197 returned source dates** from **1990-01-02 through 2026-10-05** and **128,758 normalized rows**: **99,712 available yields** and **29,046 absent fields**. All **442 source months** have a successful audit; 443 successful attempts include a current-month replay. Three failed October 2010 attempts remain in the audit history and were resolved by accepting the feed's valid date-only entry with all fourteen yields explicitly `field_absent`. No yields or source dates are invented. Reader checks found fourteen normalized rows for every stored date, preserved the existing historical Treasury values/provenance, and confirmed all 711,152 Coinbase candles remain intact. Publication-calendar completeness remains unestablished. The default ten-year request-width limit is independent of retained history; use bounded date windows to explore the full dataset.

The opt-in HTTP check compares January 1990/2020/2024 to fresh validated source reads, without database writes, credentials, or model calls:

```powershell
.venv\Scripts\python.exe scripts/check_treasury_api.py
```

On 2026-10-06, January 1990/2020/2024 each matched all **294 rates** and **42 benchmark values/spreads** across **three pages**. Existing Coinbase 2020/2024 daily and hourly HTTP checks also passed after the history extension. At that checkpoint the full **388-test** isolated PostgreSQL suite, formatting, linting, and strict type checks passed. Agent calls remain disabled locally; those source checks are separate from Treasury agent execution tests.

Review the code in this order: `treasury/models.py` and `client.py` for native source semantics; migration `0002`, `db/treasury_store.py`, and `treasury/service.py` for persistence/replay; `treasury/query_models.py` and `queries.py` for evidence/calculations; then the Treasury routes in `api.py`, `agent/` adapters/instructions/runner, and matching unit/integration tests. Live check scripts are separate from the synthetic deterministic fixtures.

## Ingest native monthly macro history

After rebuilding the runtime image and applying migration `0004` using the startup
commands above, run explicit manual jobs with ingestion credentials:

```powershell
docker compose run --rm ingest ingest-bls --start 2024-01-01 --end 2025-01-01
docker compose run --rm ingest ingest-fed --start 2024-01-01 --end 2025-01-01
```

Dates must be first-of-month `YYYY-MM-01`; windows are half-open and end no later than
the current month's start. A completed observation month can still be absent because
publication lags or data is unavailable. Source-dash values, absent months and retained
omissions are reported separately. Current history and changes collected locally do
not establish retrospective publisher vintages or publication times.

| Command policy | BLS CPI/unemployment | Fed monthly effective federal funds |
| --- | --- | --- |
| Default history | 1947-01; unemployment begins in 1948-01 | 1954-07 |
| Fetch windows | At most ten inclusive years per request | One full-release ZIP/XML read for the requested window |
| `--refresh` | Current year plus preceding five years, covering CPI revision history | All native monthly history; the release download already contains it |
| `--resume` | Reuse verified complete older windows; refetch windows touching the revision region | Reuse verified complete older explicit windows; refetch any window including the latest completed month |

```powershell
docker compose run --rm ingest ingest-bls --resume
docker compose run --rm ingest ingest-bls --refresh
docker compose run --rm ingest ingest-fed --resume
docker compose run --rm ingest ingest-fed --refresh
```

Each command accepts configurable `--start`/`--end`, `--window-seconds` (default 180),
`--max-seconds` (default 900), `--request-interval` (default 3; range 3–60) and
`--max-requests` (default 12 BLS / 3 Fed; range 1–25). The request limit counts retries
and all fetches through that command's client. It does not track other processes,
earlier commands or the provider's daily quota. Exhaustion reports `retry_exhausted`
with request usage and a separate `request_budget_exhausted` flag. Refresh cannot be
combined with `--start`, `--end` or `--resume`. Bounds/modes/budgets are validated before
configuration, database or network access.

Resume checks the latest relevant overlapping audit, exact bounds, expected native
month keys and stored counts in one repeatable snapshot. A later failed/running attempt,
a gap or a read retaining omitted periods forces a refetch. Explicit unavailable values
still represent source-returned month keys. Skipped reads preserve their old receipt
provenance and report zero new writes; they do not establish fresh confirmation. Changes
outside the BLS refresh region require an explicit replay without resume. Fed's default
window includes the latest completed month and therefore always reads the release again.

Successful windows commit independently. Download/parsing occur between short database
transactions; no connection is held during HTTP. Versions, footnotes, current pointers
and success counts commit together. A failure preserves earlier committed windows and
attempts a separate controlled failure audit. JSON progress/recovery output contains
counts and identifiers, not downloaded datasets, SQL, credentials or model payloads.
No scheduler or model call is involved. Equivalent host jobs use
`.venv\Scripts\python.exe -m market_intelligence ingest-bls` or `ingest-fed`.

### Local ingestion acceptance — 2026-10-09–10

Explicit migration `0004` and reader-role readiness passed. Real 2024 loads stored 24 BLS
observations and 12 Fed rates. Replays recorded separate audits with zero new versions;
fingerprints confirmed unchanged content and original provenance. Fresh provider reads
matched reader SQL for native period labels, Decimal values, missing reasons and footnotes.

| Series | Retained local observations | Backfill state |
| --- | --- | --- |
| CPI | 956 represented months: 1947-01 through 2026-08; 955 available | Historical resume completed; 2026-09 absent from the returned response |
| Unemployment | 945 represented months: 1948-01 through 2026-09; 944 available | All requested native month keys present |
| Fed funds | 867 available months: 1954-07 through 2026-09 | All requested native month keys present |

BLS initially rejected the 2007–2016 window after six older windows committed. One bounded
diagnostic confirmed daily-quota exhaustion; further BLS calls stopped. The unregistered
API permits [25 queries per day](https://www.bls.gov/developers/api_faqs.htm), while the
job's attempt cap covers only that invocation. No reset time was established. The failure
audit contains `source_rejected`; earlier data and the verified 2024 sample remain intact.
On 2026-10-10, access returned and this bounded resume reused the six older windows and
completed both remaining reads in two HTTP attempts:

```powershell
docker compose run --rm ingest ingest-bls --resume --max-requests 3 --max-seconds 600
```

The job inserted 449 monthly records, preserved 24 unchanged overlapping observations
and recorded no corrections or retained omissions. Both BLS series retain an explicit
unavailable October 2025 value and its source footnote. September 2026 CPI was absent
from the successful response and remains unstored; this does not establish a publication
delay or justify filling it. The interrupted historical backfill is resolved.

Two fresh BLS reads matched all 473 resumed-window keys, exact native labels/values,
missing reasons and ordered notes against reader SQL. Native API/SQL checks also matched
history pages, coverage, latest and observed versions. Fingerprints preserve all earlier
Coinbase/Treasury/Hyperliquid facts/audits, original macro content/catalog/audits and the
2024 sample's provenance. Reader-only resume planning now reuses seven older windows
while always refetching the recent revision region. The API remains healthy and disabled.
No dataset files, scheduler or production-code repair was added. The separate scoped
agent follow-up is recorded above. These checks establish sampled ingestion/replay and
retained native keys, not historical release vintages or publication-calendar completeness.

## Explore stored monthly macro data

After migration `0004`, API startup and manual ingestion, use
[Swagger UI](http://127.0.0.1:8000/docs) or these GET routes:

| Route | Inputs and evidence |
| --- | --- |
| `/v1/macro/series` | Fixed native series, publisher, units, seasonal adjustment and actual retained month counts/bounds |
| `/v1/macro/series/{series_id}/observations` | `start=2024-01-01&end=2025-01-01`; current values, full-window coverage, receipts and an optional continuation cursor |
| `/v1/macro/series/{series_id}/latest` | Latest stored completed month, including a source-unavailable value, and explicit month lag |
| `/v1/macro/series/{series_id}/versions` | `month=2024-01-01`; immutable versions observed locally, their provenance and current version number |

Use `CUSR0000SA0` for CPI, `LNS14000000` for unemployment and `RIFSPFF_N.M` for Fed funds.
The index, percent and percent-per-annum units and seasonal metadata remain native.
Decimal values serialize as strings; Fed native month-end labels remain separate from
the canonical first-of-month identity. Queries read only PostgreSQL, using the reader
role; they make no provider or model requests.

History bounds are first-of-month dates with an exclusive end. Requests start within
the series' native lifetime and include only completed months. Configure
`API_MACRO_MAX_WINDOW_MONTHS` separately from the existing day-based limits; its
default/maximum is 1200 months, covering the full native history. Both paginated routes
accept `limit` from 1 to 100. Supply the returned `next_cursor` with the same series and
window or month. Each page is a separate repeatable snapshot; concurrent ingestion can
change coverage between requests.

Whole-window coverage distinguishes stored month keys, available values, explicit
source-dash values and absent stored keys. An unavailable source value represents a
month, while an absent key has no fabricated observation. Missing ranges are coalesced
and capped at 100 with full range/month counts retained. `complete` refers to the
represented native monthly grid; publication-calendar completeness remains unverified.

Latest returns the latest stored month rather than skipping unavailable values. Its
month lag is not a collection-age threshold or a verified publication delay. Content
provenance identifies the original receipt that created each version; unchanged replays
or retained omissions preserve it. Receipts include source access dates and metadata
separately from observation footnotes. Neither receipt times nor Fed prepared text
establish observation release times. Version history records content changes observed
locally and does not reconstruct publisher vintages or historical market as-of states.
Derived inflation remains outside this implementation. Native macro agent tools reuse
these same readers; see [the agent contract](#ask-questions-through-the-agent).

Run the opt-in stored-data check after the normal locked dependency installation:

```powershell
.venv\Scripts\python.exe scripts/check_macro_api.py
```

The script compares 2024 HTTP pagination, latest evidence and one month's observed
versions with reader SQL, retaining downloaded responses only in memory. It requires a
quiet stored dataset for comparisons across separate HTTP snapshots and performs no
writes, provider requests or model calls. Optional `--start`/`--end` choose a shared
native window; the development Docker target includes the script.

Local verification on **2026-10-09** passed all 36 stored 2024 observations through three
pages per series, with exact native content/footnote/version parity. The shared
1954-07–2026-10 window matched 642 stored months and 225 uncollected months per BLS
series, and all 867 Fed months. Latest and local-version evidence also matched reader
SQL. All 13 existing and macro fact/catalog/audit table fingerprints stayed unchanged;
the API is healthy and the agent remains disabled. All **906 deterministic tests**
(577 unit / 329 integration), Ruff and strict mypy pass, including concurrent-correction
snapshots and controlled 404/422/503 behavior. BLS ingestion was still deferred at that
checkpoint; these HTTP checks consume none of its quota. The interrupted BLS history was
subsequently resumed and verified on 2026-10-10 as recorded above.

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

If uv is already installed, `uv sync --locked --no-python-downloads` is equivalent. Environments and caches are ignored. Runtime dependencies include SQLAlchemy Core, psycopg, Alembic, Pydantic Settings, HTTPX, FastAPI, Uvicorn, and the official OpenAI SDK; pytest, Ruff, and mypy are development dependencies. OpenAI configuration is required only for the explicitly enabled agent.

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

Never commit `.env`, keys, dumps, local datasets, private prompts, or secret-bearing logs. Docker context excludes secrets, environments, datasets, caches, and Git history; images copy selected files and run application commands as a non-root user. Avoid displaying resolved Compose configuration because it contains passwords; use `config --quiet`. Commit small, coherent changes as they are made, after appropriate checks and staged-diff review. Pushes, pull requests, and merges require authorization; see `AGENTS.md` for the local workflow.

## Hyperliquid funding and forward open interest

`hyperliquid/` reads BTC perpetual settled funding and current open interest from the
[public Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals).
Funding history starts in 2024 for this slice. Rates and premiums retain native signed
decimal fractions; positive funding means longs pay shorts. Exact millisecond event
timestamps are retained alongside a derived UTC settlement hour. The client handles
inclusive 500-record pagination with exact boundary deduplication and bounded requests.

Open interest uses BTC underlying units; mark/oracle prices use USDT denomination.
USDC collateral and settlement remain distinct from that denomination under the
[contract specification](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications).
Each context read records its own identity and local fetch/receipt times, with no
invented exchange event timestamp. Current context funding is not settled history.
Historical OI archives and unattended collection are outside this slice.

The existing locked dependencies suffice. After the normal dependency installation,
run the opt-in source check on the host:

```powershell
.venv\Scripts\python.exe scripts/check_hyperliquid_live.py
```

On 2026-10-07 the check returned 24 January 1 funding events and all 744 distinct
settlement hours for January 2024 through two pages, preserving source offsets. A
current BTC context also passed validation. These samples perform no database writes,
archive downloads, or model calls and do not establish all-history coverage.
Synthetic tests separately exercise parsing, pagination, retries, and response bounds.
Revision `0003` adds a separate BTC perpetual catalog, funding events/monthly-window
audits, and immutable OI receipts/collection audits. The writer cannot mutate the catalog
or update/delete OI snapshots; the reader has SELECT-only access. Funding corrections
update current values and preserve unchanged provenance. Omitted events remain stored
and reported. Changed source timestamps within an existing settlement hour fail for
inspection. Historical resume reuses only a complete latest successful read whose stored
hour count still matches; gaps and failed attempts are refetched, and the current month
is always fetched. Explicit replay without `--resume` fetches historical corrections.

Rebuild and migrate explicitly, then run manual jobs with writer credentials:

```powershell
docker compose build migrate
docker compose run --rm migrate
docker compose run --rm ingest ingest-funding --start 2024-01-01 --end 2024-02-01
docker compose run --rm ingest ingest-funding --resume
docker compose run --rm ingest ingest-funding --refresh
docker compose run --rm ingest collect-open-interest
```

Equivalent host commands use `.venv\Scripts\python.exe -m market_intelligence` followed
by the same job arguments. Funding jobs default to 2024-01-01 through current receipt
time, split at UTC month boundaries, with three-second request pacing, 90 seconds per
window and a 900-second overall budget. OI collects exactly one current snapshot with
a 30-second budget. `--refresh --resume` is rejected. Each job records a running audit,
fetches without an open write transaction, then commits facts and success atomically;
sanitized failure audits are separate. No scheduler is started.

The reader API uses the same reader role and repeatable snapshots as the existing
datasets. Five routes expose the fixed BTC perpetual:

| Route | Evidence |
| --- | --- |
| `/v1/hyperliquid/funding/latest` | Latest stored event and source-event age |
| `/v1/hyperliquid/funding?start=...&end=...` | Exact events, whole-window hour coverage, gaps, and continuation |
| `/v1/hyperliquid/funding/summary?start=...&end=...` | Arithmetic sum/mean of settled fractions when all hours are present |
| `/v1/hyperliquid/open-interest/latest` | Latest stored receipt and receipt age |
| `/v1/hyperliquid/open-interest?start=...&end=...` | Collected receipts with deterministic time/identity pagination |

Funding ranges use half-open complete UTC-hour windows. A derived hour grid measures
stored settlements; missing hours withhold full-window metrics. Rate sum is an arithmetic
sum without compounding or a trader's position/notional path. The mean rounds to 18
decimal places, half even. Missing ranges coalesce and truncate after 50 ranges. OI
windows use actual local receipt times and expose observed snapshot counts, without an
expected collection calendar or inferred historical completeness. Mark/oracle context
prices remain separate from traded prices. Decimal values serialize as strings.
Continuations bind to the source/instrument/dataset/window, preserving exact funding
milliseconds and breaking OI receipt-time ties with snapshot identity. Each page is a
separate snapshot; its coverage describes the whole requested window.

`API_FUNDING_STALE_AFTER_SECONDS` defaults to 7200; `API_OPEN_INTEREST_STALE_AFTER_SECONDS`
defaults to 3600. These configured age thresholds do not imply scheduled refreshes.
`API_MAX_WINDOW_DAYS` also bounds these routes. Rebuild/start the API after migration:

```powershell
docker compose up -d --wait api
.venv\Scripts\python.exe scripts/check_hyperliquid_api.py
```

The opt-in reader check compares January 2024 funding HTTP pages and rate sums with
a fresh public source read, and independently matches the latest OI HTTP receipt to
reader SQL. It requires the funding sample and one OI collection already stored.
All 541 isolated tests pass, including migrations, grants, replay, gaps, rollback,
query arithmetic, HTTP serialization, cursor binding, OI ties, and concurrent snapshots.
At the Hyperliquid checkpoint the agent had seven fixed tools: BTC/Treasury plus latest
settled funding, complete-hour funding summaries, and latest stored OI. The separate
2026-10-08 live agent acceptance above inspected all three tools and their limitations.

Local acceptance on 2026-10-07 retained 24,253 funding events and distinct derived hours
from 2024-01-01T00:00:00.151Z through 2026-10-07T13:00:00.058Z across 34 successful
window audits, plus one OI receipt. The January 2024 check matched all 744 events,
timestamps, rates and premiums through four HTTP pages; the arithmetic rate sum matched
the fresh sample at 0.0242847281 fraction (2.42847281 percent). OI evidence independently
matched reader SQL by receipt identity, timing, quantity, and both context prices.

One unavailable funding hour, 2024-08-15 13:00 UTC, was independently checked in a
three-hour source request that returned only its two neighboring events. The full stored
history reports that gap and withholds full-window metrics; no rate is invented.
Successful source reads do not erase this limitation, and resume will refetch the gapped
window. Fingerprints verified unchanged values/provenance for all 711,152 Coinbase
candles, 86 Coinbase audits, 128,758 Treasury facts and 446 Treasury audits. The rebuilt
API/database are healthy, the persistent agent remains disabled, and no model call or
archive access was made for these checks. Test containers/network were removed while
preserving the development volume.
