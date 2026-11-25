FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY bask_223p ./bask_223p
COPY examples/illustrative-ahu-refs.ttl ./examples/illustrative-ahu-refs.ttl
COPY examples/bench-scan.ttl ./examples/bench-scan.ttl
COPY examples/live-ingest ./examples/live-ingest

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e . -e ./examples/live-ingest \
    && mkdir -p /app/var

WORKDIR /app/examples/live-ingest
ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["python", "-m", "live_ingest"]
