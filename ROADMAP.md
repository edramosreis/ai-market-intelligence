# Incremental delivery roadmap

Status: **Milestone 1 reviewed and in progress; database foundation implemented.**

Aim for roughly equal depth in data engineering and AI engineering across the completed project. Early work necessarily establishes the data foundation; later milestones add retrieval, agent tools, and evaluation. Technologies below are candidates justified by each checkpoint, not dependencies to add immediately.

Keep README, architecture diagrams, tests, data semantics, decision explanations, and a repeatable demo current at every milestone. Advance when the current slice works and its limitations can be explained. Avoid reserving presentability and interview preparation for the final milestone.

## Milestone 1 - Core vertical slice

Deliver Python, one public crypto source, BTC ingestion, a normalized PostgreSQL schema and migrations, deterministic FastAPI queries, an OpenAI agent using read-only function calls, Docker Compose, and automated tests. The approved contract uses Coinbase Exchange spot BTC/USD, five-minute candles, retained history initially from 2020-01-01 with configurable earlier starts, coarser derived candles, and monthly resumable backfill chunks. Historical questions must reach at least 2024. See [ARCHITECTURE.md](ARCHITECTURE.md).

Completed checkpoint: Python package/tooling/lockfile, pinned runtime and PostgreSQL containers, five-table schema and seeds, restricted roles, migration CLI, disposable PostgreSQL tests, and setup/quality documentation. Remaining checkpoints: replayable provider ingestion and real backfill, deterministic query/API behavior, grounded agent tools, and full live-demo acceptance. No candle history has been loaded yet; Milestone 1 is not complete.

Data engineering emphasis: grain and units, exact decimals, UTC boundaries, source coverage, idempotency, corrections, transactional writes, and recovery from failed ingestion.

AI engineering emphasis: tool schemas, application-side validation, actual tool execution, deterministic calculations, evidence returned with answers, bounded calls, and controlled failure behavior.

Exit evidence: all Milestone 1 acceptance criteria in the architecture document, a documented local demo, deterministic tests plus separate live smoke checks, and an explanation of the chosen source and trade-offs. Swagger UI and HTTP examples are sufficient; a custom frontend is optional future work.

Do not add Kafka/Redpanda, Airflow, FRED, SEC ingestion, RAG, pgvector, MCP, a full eval suite, monitoring infrastructure, or cloud resources here.

## Milestone 2 - Broader structured market data

Add funding and open-interest metrics, a deliberately chosen traditional-market source, and FRED/macro data. Hyperliquid is a candidate for derivatives data; explicitly review its historical retention and any archive/provider requirement. Keep spot and perpetual instruments separate, including traded versus mark/index prices and quote/collateral/settlement semantics. Define each fact table around its actual grain, instrument identity, units, timestamps, calendars, and correction/revision behavior. Do not stretch OHLCV into a universal metric table.

Introduce batch scheduling when unattended jobs justify it. Start by reviewing a simple scheduler; choose Airflow only if dependency graphs, retries, backfills, and operational visibility warrant its local cost. Keep data-fetching logic usable outside the scheduler.

Add analytical tools for comparisons and time-series calculations, with deterministic results and clear definitions. Address release time versus observation period for macro data and exchange calendars for traditional markets; do not assume crypto's 24/7 calendar applies everywhere.

Exit evidence: independently replayable provider ingestion, documented mappings and revision policies, reliable scheduled execution where chosen, and grounded answers spanning at least two structured domains. Review source terms before publicly redistributing their data.

Portfolio checkpoint: after Milestone 1-2, the project is suitable to describe on a CV as an active project, stating only implemented capabilities.

## Milestone 3 - Unstructured financial data and RAG

Add Fed/FOMC documents, SEC filings, and company financial documents with source identifiers, publication dates, document versions, and provenance. Separate acquiring originals from parsing and chunking. Keep embeddings reproducible through document/chunk identifiers and recorded model/configuration versions.

Introduce embeddings and pgvector when retrieval is implemented. Evaluate parsing quality, chunk boundaries, metadata filters, and retrieval behavior with small known examples. Build grounded responses citing identifiable source passages and combine structured queries with document retrieval when the question needs both.

Basic regression checks for retrieval belong here; the systematic comparison and scoring platform remains Milestone 6. Retain suitable source artifacts outside Git and document provider acquisition requirements.

Exit evidence: repeatable document processing, inspectable retrieved passages, source/date-aware answers, and demos connecting a document explanation to stored market observations. Explanations must distinguish observed association from demonstrated causation.

Portfolio checkpoint: after Milestone 3, the project should support substantive Data Engineering and Data+AI interview discussions.

## Milestone 4 - Real-time data engineering

Add WebSocket ingestion and select Kafka or Redpanda after comparing operational needs. Define event schemas, source event time versus receipt time, retention, producer/consumer boundaries, and streaming-to-database behavior.

Implement replay, idempotent sinks, reconnect and gap recovery, backpressure, bounded failures, and consumer restart behavior. State delivery guarantees at each boundary; do not label the pipeline exactly-once solely because a broker offers a feature. Reconcile streaming observations with batch history and source corrections.

Exit evidence: demonstrate disconnect/reconnect, replay, duplicate events, and consumer failure without unexplained data loss or duplicate stored facts. Update architecture diagrams with the actual new boundaries and measured latency.

## Milestone 5 - AI platform capabilities

Expose reviewed structured-data and research tools through an MCP server. Reuse proven query/retrieval functions while explicitly designing the new transport, authentication, permissions, errors, and versioning.

Improve agent orchestration, structured answer outputs, tool-selection controls, and guardrails in response to demonstrated needs. Reassess whether an orchestration framework now reduces complexity. Add tracing and observability for model calls, tool execution, retrieval, latency, and failure categories while redacting sensitive content.

Exit evidence: tools work through an external MCP client, authorization boundaries are testable, agent execution is inspectable, and failure traces explain what the system actually did. Do not introduce multi-agent behavior just for a portfolio diagram.

## Milestone 6 - LLM evaluation

Create a versioned evaluation dataset with expected tool choices, reference calculations, retrieval relevance labels where appropriate, grounded-answer requirements, and difficult missing/stale/ambiguous-data cases.

Measure tool-selection accuracy, retrieval quality, factuality/hallucinations, latency, and cost. Distinguish deterministic checks from model-graded judgments and document their limitations. Record model identifiers, prompt/tool versions, dataset versions, and retrieval settings for comparable runs.

Compare models against the same dataset and workload. Add model routing only if the measurements justify its maintenance cost and routing mistakes are themselves evaluated.

Exit evidence: repeatable evaluation reports with actionable failure analysis and measured trade-offs. This milestone expands earlier unit/regression tests rather than postponing correctness work until now.

Portfolio checkpoint: around Milestone 5-6, the project should become a centerpiece demonstrating roughly equal Data Engineering and AI Engineering depth.

## Milestone 7 - Production-style deployment

Deploy to a deliberately chosen cloud environment. Add deployment CI/CD, infrastructure as code where it makes the environment reproducible, monitoring, managed secrets/configuration, persistence and backup/restore procedures, and cost controls.

Define public-service authentication, abuse/spend limits, network access, operational failure ownership, and provider redistribution rights before exposing data or the agent. Improve public-facing documentation and optionally add a lightweight UI.

Local quality scripts and lockfiles are required much earlier. A small automated quality workflow can be introduced earlier if collaboration warrants it; production deployment automation remains this separate milestone.

Exit evidence: repeatable deployment, tested restore/recovery, usable operational signals, clear cost/permission boundaries, and a public demo whose claims match the implemented features.

## Proposed commit boundaries

These are suggested logical units only. **Do not create commits, stage files, or push without an explicit request.** Keep each implementation change accompanied by relevant tests and updated documentation.

1. `Document vertical slice architecture and roadmap` - reviewed planning documents, contributor guidance, placeholder configuration, and ignore rules.
2. `Add reproducible Python and local database tooling` - dependency lock, formatter/linter/type-check configuration, application image, Compose, role bootstrap, and exact setup/check commands.
3. `Add market schema and versioned migrations` - source/asset/market/candle/run tables, grants, deterministic fixture support, and real PostgreSQL constraint tests.
4. `Add replayable BTC candle ingestion` - provider contract, multi-year backfill/refresh CLI, monthly transaction boundaries, resume, validation, retries, audit behavior, transaction and replay tests.
5. `Expose validated market queries through FastAPI` - analytical definitions, historical date queries, coarser derived candles, provenance/coverage, pagination, readiness, and query/API tests.
6. `Add grounded BTC agent tools` - OpenAI runner, tool validation, budgets, evidence, fake-model tests, and live-smoke instructions.
7. `Document and validate the complete local demo` - final acceptance evidence, failure/recovery examples, architecture updates, and interview walkthrough.

Adjust commit sizes to the actual code: tightly coupled tooling/schema changes can be reviewed together when splitting would leave a broken state. Keep all seven delivery milestones separate.

## Review checkpoints

The source/data contract and local foundation approach have been reviewed, and the user approved implementation. Continue the remaining Milestone 1 checkpoints within that design; discuss material changes and choose OpenAI model/spend when the agent checkpoint needs them. Revisit architecture at every later milestone rather than implementing speculative interfaces now.
