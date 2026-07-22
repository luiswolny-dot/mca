# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Multi-stage build.
#
# Design decision: the risk test suite runs at BUILD time. An image whose gate,
# sizing or exit logic fails its own tests cannot be produced, let alone
# deployed. This costs ~4 seconds per build and removes an entire class of
# "we shipped a broken stop-loss on Friday evening" incidents.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel && pip install ".[ml]"

# ---------------------------------------------------------------------------
FROM builder AS test
COPY tests/ ./tests/
COPY scripts/ ./scripts/
# Fails the build if any safety-critical test fails.
RUN python scripts/run_tests.py tests.unit.test_core

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root. The executor handles key material; running it as root means a
# container escape is a host compromise.
RUN groupadd -r mca && useradd -r -g mca -d /app -s /sbin/nologin mca

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=test /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

WORKDIR /app
COPY --chown=mca:mca src/ ./src/
COPY --chown=mca:mca scripts/ ./scripts/
RUN mkdir -p /app/var && chown mca:mca /app/var
ENV PYTHONPATH=/app/src

USER mca

HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import mca; import sys; sys.exit(0)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "mca.api.app"]
