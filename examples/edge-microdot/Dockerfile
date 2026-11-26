FROM python:3.15-slim@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5

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
    && mkdir -p /app/examples/edge-microdot/var \
    && useradd --uid 1000 --create-home edge \
    && chown -R edge:edge /app/examples/edge-microdot/var

WORKDIR /app/examples/edge-microdot

USER edge

EXPOSE 8080
EXPOSE 47808/udp

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

ENTRYPOINT ["python", "-m", "edge_microdot"]
CMD ["--host", "127.0.0.1", "--port", "8080", "--driver", "fake"]
