# AI Market Intelligence Platform [WIP]

A market research platform being built to collect historical data, produce reproducible analysis, and answer questions grounded in stored market observations.

**Current state: Milestone 1 database foundation implemented and verified.** The Python package, locked dependencies, containers, migrations, restricted roles, and PostgreSQL integration tests are runnable. Ingestion, HTTP queries, and the AI agent are the remaining Milestone 1 checkpoints. No historical candles have been loaded yet.

The approved data contract is **Coinbase Exchange spot BTC/USD, completed five-minute candles, and an initial backfill from 2020-01-01**, with earlier dates configurable subject to source availability. Retain ingested history without a rolling retention limit. Fifteen-minute, hourly, and daily bars will be derived from the canonical five-minute observations.

- [ARCHITECTURE.md](ARCHITECTURE.md): reviewed design, semantics, trade-offs, and full Milestone 1 acceptance criteria.
- [ROADMAP.md](ROADMAP.md): seven separate milestones and suggested commit boundaries.
- [AGENTS.md](AGENTS.md): contributor commands, scope, and conventions.

## Start the foundation

Run commands from the repository root. Start Docker Desktop with Linux containers and Docker Compose v2. A host Python 3.14 interpreter is needed for credential generation; application containers use Python 3.14.8 and PostgreSQL 18.6, with both images pinned by digest. Host development supports Python 3.14.x.

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

This foundation is for local development; it has no HTTP application or remote access endpoint yet. Once the API exists, a remote demo should expose only the app through authenticated HTTPS, using an access-controlled tunnel or a separate hosted environment. Keep PostgreSQL and the Docker daemon private. Before inviting testers, add application authentication, request limits, and model-spend limits at the relevant checkpoint. Sharing a Git repository lets others run their own local copy with independently generated credentials. Never share your `.env`.

## Schema and access

| Table | Purpose |
| --- | --- |
| `data_sources` | Seeded Coinbase Exchange identity |
| `assets` | Seeded BTC and USD identities and kinds |
| `markets` | Seeded Coinbase `BTC-USD` market and base/quote units |
| `ingestion_runs` | Request windows, lifecycle, counts, and audit identity |
| `candles` | Exact OHLCV, five-minute UTC starts, and ingestion provenance |

PostgreSQL enforces identity, unique candle grain, five-minute alignment, `numeric(38,18)`, finite positive prices, finite non-negative volume, OHLC bounds, and run windows/lifecycle/counts. Each candle links to a run for the same market and interval through a composite foreign key. Application validation of naive timestamps, excess decimal scale, closed-candle eligibility, and payloads belongs to ingestion; PostgreSQL can coerce timestamps or round decimals, so these checks must precede inserts.

| Role | Grants |
| --- | --- |
| Administrator | Local bootstrap, schema ownership, migrations, reference seeds, and grants |
| Ingestion | SELECT on foundation tables; INSERT/UPDATE on candles and ingestion runs |
| Reader | SELECT on foundation tables only |

Both restricted roles can read the migration revision, cannot delete rows or create permanent/temporary tables, and have no elevated role flags. The check container receives only reader credentials. The future API/agent will use this reader role.

## Run tests

The full suite uses a separate container, network, database name, and memory-backed PostgreSQL storage, with no published port or development volume. Fixtures refuse integration tests unless host is `db-test` and database is `market_intelligence_test`.

```powershell
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yaml down
```

The first command returns the test runner's exit code. The second removes only test containers/network; run it even after failure. Expected rejection errors appear in test database logs. Each container start initializes an empty database and applies the same migration as development; fixtures use tiny synthetic candles and roll back test writes.

The foundation suite covers configuration/secret masking, migration/schema agreement, downgrade/reapply, repeatable bootstrap, exact decimals/UTC, constraints/FKs, transaction rollback, and actual role permissions. No Coinbase or OpenAI calls are required.

## Host development and quality checks

With Python 3.14 installed, bootstrap project-local uv 0.12.23 and install the locked environment:

```powershell
python -m venv .tools
.tools\Scripts\python.exe -m pip install uv==0.12.23
.tools\Scripts\uv.exe sync --locked --no-python-downloads
```

If uv is already installed, `uv sync --locked --no-python-downloads` is equivalent. Environments and caches are ignored. Runtime dependencies are SQLAlchemy Core, psycopg, Alembic, and Pydantic Settings; pytest, Ruff, and mypy are development dependencies. FastAPI and the OpenAI SDK will be added at their checkpoints.

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

Implement the Coinbase adapter and replayable ingestion: verify BTC volume units, parse exact decimals, filter closed candles, fetch bounded pages, validate monthly chunks, persist atomically, report gaps, and resume failures. Verify a small live range and replay behavior before loading history from 2020-01-01. Then add deterministic historical queries/FastAPI and grounded agent tools. Milestone 1 is complete only when the entire slice meets its acceptance criteria.
