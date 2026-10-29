# Patch tags and registry digests are pinned; update them deliberately together.
FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d AS base
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
