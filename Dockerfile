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

COPY requirements.txt requirements-dev.txt ./
RUN python -m pip install --upgrade pip wheel uv && \
    uv pip install --system -r requirements.txt -r requirements-dev.txt

COPY . .

RUN mkdir -p logs tmp/celery-metrics

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
