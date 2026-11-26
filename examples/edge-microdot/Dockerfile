FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV EDGE_HOST=127.0.0.1
ENV EDGE_PORT=8080

WORKDIR /app

# linux/arm64 manylinux wheels exist for duckdb and friends; the alpine/musl
# image would compile duckdb from source (too heavy for a 1 GB Pi).
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY bask_223p ./bask_223p
COPY examples/illustrative-ahu-refs.ttl ./examples/illustrative-ahu-refs.ttl
COPY examples/bench-scan.ttl ./examples/bench-scan.ttl
COPY examples/edge-microdot ./examples/edge-microdot

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e . \
    && python -m pip install --no-cache-dir -e "./examples/edge-microdot[sparql]" \
    && mkdir -p /app/examples/edge-microdot/var

WORKDIR /app/examples/edge-microdot

EXPOSE 8080
EXPOSE 47808/udp

ENTRYPOINT ["python", "-m", "edge_microdot"]
CMD ["--host", "127.0.0.1", "--port", "8080", "--driver", "fake"]
