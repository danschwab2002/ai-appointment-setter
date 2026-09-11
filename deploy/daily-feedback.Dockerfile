FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev
COPY src ./src
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED="1"
EXPOSE 8000
CMD ["uvicorn", "bridge.daily_feedback_app:create_application_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
