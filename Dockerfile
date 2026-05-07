# weave service — FastAPI + Neo4j gateway + AI watcher/worker. Runs on 8007.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.5.11

WORKDIR /app

COPY requirements.txt pyproject.toml /app/
RUN uv pip install --system --no-cache -r requirements.txt

COPY app/ /app/app/

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin weavesvc \
    && chown -R weavesvc:weavesvc /app
USER weavesvc

EXPOSE 8007

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007", "--proxy-headers", "--forwarded-allow-ips", "*"]
