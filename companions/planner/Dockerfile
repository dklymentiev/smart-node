# syntax=docker/dockerfile:1.7

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

ENV PLANNER_MIGRATIONS_DIR=/app/migrations

RUN pip install --upgrade pip && pip install .

EXPOSE 8080

CMD ["uvicorn", "planner.app:app", "--host", "0.0.0.0", "--port", "8080"]
