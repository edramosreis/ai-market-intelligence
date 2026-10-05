# Repository Guidelines

## Project Structure & Module Organization

The Milestone 1 foundation uses Python 3.14, uv, SQLAlchemy Core/psycopg, Alembic, Pydantic Settings, and PostgreSQL 18. `src/market_intelligence/` contains configuration, CLI, connections, Core metadata, and role setup. `migrations/` contains immutable revisions and reference seeds. `tests/unit/` and `tests/integration/` contain configuration and PostgreSQL checks. `scripts/init_local_env.py` creates ignored credentials. `Dockerfile`, `compose.yaml`, and standalone `compose.test.yaml` support runtime jobs and isolated tests. Keep generated output, environments, and datasets out of version control. Add `assets/` only when static resources exist.

## Architecture Review and Scope

The Milestone 1 architecture has been discussed and the user approved beginning its foundation. The accepted contract is Coinbase Exchange spot BTC/USD, completed five-minute candles, initial backfill from 2020-01-01, configurable earlier dates, and retained history. The foundation is implemented; ingestion, query/API, and agent checkpoints remain. Review `ARCHITECTURE.md` before continuing. Keep future components in the seven-milestone roadmap rather than scaffolding them early. Material source, data-model, technology, or direction changes require discussion; minor choices can be made within the reviewed design. OpenAI model/spend remains a decision for the agent checkpoint.

## Build, Test, and Development Commands

Run from the repository root; full installation instructions are in `README.md`.

- `python scripts/init_local_env.py`: create ignored `.env`, refusing overwrite.
- `.tools\Scripts\uv.exe sync --locked --no-python-downloads`: install locked dependencies after uv bootstrap.
- `docker compose build migrate`: build the runtime image.
- `docker compose up -d --wait db`: start persistent local PostgreSQL.
- `docker compose run --rm migrate`: provision roles, migrate, and apply grants.
- `docker compose run --rm check`: verify schema/market with reader credentials.
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

## Commit & Pull Request Guidelines

There is no commit history from which to infer conventions. Use concise, imperative commit subjects, such as `Add initial data ingestion module`. Keep commits focused.

Pull requests should explain the change, its purpose, and validation performed. Link relevant issues, identify configuration changes, and include screenshots for visible interface changes.

Propose logical commit boundaries as work progresses, but do not create commits, stage files, or push without an explicit user request.

## Security & Configuration

Never commit credentials, keys, private datasets, dumps, or personal prompts. `.env.example` contains placeholders only; actual configuration belongs in ignored `.env` or runtime environment. Supply only needed credentials to each job. Administrator access is for local bootstrap/migrations; ingestion can INSERT/UPDATE only candles/runs; API/agent must use the reader role. No OpenAI credential is needed for this foundation. Exclude secrets from Docker context as well as Git. Update this guide and README when commands, layout, or dependencies change.
