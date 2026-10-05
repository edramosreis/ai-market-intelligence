# Patch tags and registry digests are pinned; update them deliberately together.
FROM python:3.14.8-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88 AS base
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
