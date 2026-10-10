# Security policy

## Supported development scope

AI Market Intelligence is a work-in-progress project intended for local development.
Security fixes target the current `master` branch. Earlier commits and feature branches
are not separately maintained release lines.

The current application has no application authentication or cumulative monetary
spending cap. A shared or internet-facing deployment requires authenticated HTTPS,
request/abuse controls, model spending controls and a reviewed network configuration.
The per-question model/tool budgets and process concurrency limit do not replace these
controls.

## Reporting a vulnerability

Use this repository's **Security and quality → Report a vulnerability** option when
GitHub private vulnerability reporting is enabled:

[Repository security advisories](https://github.com/edramosreis/ai-market-intelligence/security/advisories)

This reporting feature must be enabled separately in the repository's GitHub settings;
adding this file does not enable it. If the private-report button is unavailable,
open an issue requesting a private reporting channel. Include no vulnerability details,
exploit instructions, credentials or private data in that public request.

A private report should describe the affected commit, environment, expected and actual
behavior, potential impact and a minimal reproduction using synthetic data. Reproduce
issues only in an instance you control. Redact credentials and user content from any
diagnostic material; do not attach `.env` files, database dumps or full model payloads.

If a credential has been exposed, revoke or rotate it at its issuing service promptly.
Deleting a file or rewriting Git history does not revoke a credential or remove copies
that other people already obtained.

## Safe local configuration

- Generate a separate ignored `.env` for each installation with
  `python scripts/init_local_env.py`; never commit or share it.
- Keep PostgreSQL and the API bound to localhost for the documented local workflow.
  Do not publish PostgreSQL or the Docker daemon through a tunnel.
- Supply administrator credentials only to bootstrap/migrations, ingestion credentials
  only to ingestion jobs, and reader credentials only to the API/agent.
- Keep `AGENT_ENABLED=false` until you have selected a model, supplied your own API key
  and decided how spending will be controlled.
- Use the isolated `compose.test.yaml` project for database tests; keep development
  data, logs, dumps, generated output and personal planning outside Git.
- Keep Docker and locked dependencies updated through reviewed changes. Follow the
  localhost-port caveat and remote-demo requirements in [README.md](README.md).

Downloaded provider data has its own source terms. Publishing this project's source
code does not grant redistribution rights to third-party datasets.
