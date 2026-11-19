# Patch tags and registry digests are pinned; update them deliberately together.
FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.23
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY alembic.ini ./
COPY migrations/ migrations/

FROM base AS runtime
RUN uv sync --frozen --no-dev --no-editable && \
    useradd --uid 10001 --create-home app
USER app
ENTRYPOINT ["market-intelligence"]
CMD ["check-db"]

FROM base AS development
RUN uv sync --frozen --no-editable && \
    useradd --uid 10001 --create-home app && \
    chown -R app:app /app
COPY --chown=app:app tests/ tests/
COPY --chown=app:app scripts/ scripts/
USER app
CMD ["python", "-m", "pytest"]
