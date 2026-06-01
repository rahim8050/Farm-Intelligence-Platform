FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    default-libmysqlclient-dev \
    build-essential \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN python -m pip install --upgrade pip wheel uv && \
    uv sync --no-install-project --group dev

COPY . .

RUN mkdir -p logs tmp/celery-metrics

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
