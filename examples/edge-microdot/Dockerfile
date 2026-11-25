FROM python:3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV EDGE_HOST=0.0.0.0
ENV EDGE_PORT=8080

WORKDIR /app

RUN apk add --no-cache \
    libffi-dev \
    gcc \
    musl-dev \
    python3-dev

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY bask_223p ./bask_223p
COPY examples/illustrative-ahu-refs.ttl ./examples/illustrative-ahu-refs.ttl
COPY examples/bench-scan.ttl ./examples/bench-scan.ttl
COPY examples/edge-microdot ./examples/edge-microdot

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e . \
    && python -m pip install --no-cache-dir -e ./examples/edge-microdot \
    && mkdir -p /app/examples/edge-microdot/var

WORKDIR /app/examples/edge-microdot

EXPOSE 8080
EXPOSE 47808/udp

ENTRYPOINT ["python", "-m", "edge_microdot"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--driver", "fake"]