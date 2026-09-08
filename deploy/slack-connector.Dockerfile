FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev
RUN groupadd --system --gid 10001 connector \
    && useradd --system --uid 10001 --gid connector --home-dir /nonexistent --shell /usr/sbin/nologin connector \
    && mkdir -p /app/data \
    && chown connector:connector /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    SLACK_STORAGE_PATH="/app/data/slack-connector.sqlite3"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"

USER connector

CMD ["uvicorn", "slack_correlation.app:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
