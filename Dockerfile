# Production image for the invoice extractor (M8) - multi-stage wheel build.
#
# Pinned multi-arch base (amd64 + arm64/v8 via the same manifest-list tag; a
# specific PATCH + Debian variant, never a moving tag). Rendering is fully
# in-process via PyMuPDF - NO poppler/pdftoppm is installed or invoked.
#
# The builder stage produces exactly ONE wheel; the runtime stage installs it
# and keeps NOTHING of the build (no source tree, no pyproject/requirements,
# no build/ or *.egg-info, no build tooling). /app is left empty.
#
# Build:   docker build -t invoice-extractor:0.1.0 .
# Run:     docker run --rm --env-file .env \
#            -v "$PWD/input:/data/input:ro" -v "$PWD/output:/data/output" \
#            invoice-extractor:0.1.0 run --input /data/input \
#            --output /data/output/results.xlsx

# --- Builder: build our package into a single wheel --------------------------
FROM python:3.13.14-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
# Only what setuptools needs to build the wheel (metadata + package source).
COPY pyproject.toml README.md requirements.txt ./
COPY invoice_extractor ./invoice_extractor
# Build exactly one wheel for our project into /wheels. `pip wheel --no-deps`
# uses build isolation (the pinned setuptools/wheel from pyproject
# [build-system]); build tools and any generated build/ or *.egg-info stay in
# this throwaway stage only.
RUN pip wheel --no-deps --wheel-dir /wheels .

# --- Runtime: minimal, non-root, no build residue ----------------------------
FROM python:3.13.14-slim-bookworm

# Runtime-only OS packages: TLS roots for provider HTTPS, and tini as PID 1 so
# SIGINT/SIGTERM reach Python (Ctrl+C -> exit 130 partial-output handling).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install pinned runtime dependencies (wheels only), then our built wheel with
# --no-deps, then remove the wheel and the temporary lock. Nothing of the
# project source, packaging metadata, or build tooling remains under /app.
COPY requirements.txt /tmp/requirements.txt
COPY --from=builder /wheels /tmp/wheels
RUN pip install --only-binary=:all: -r /tmp/requirements.txt \
    && pip install --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels /tmp/requirements.txt

# Non-root runtime user (default 1000:1000; overridable at run time with
# `--user`/compose `user:` for Linux hosts whose UID differs). Create the
# mount points and make them owned by the runtime user so a first run can
# write to /data/output even before a host bind mount is attached.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser \
    && mkdir -p /data/input /data/output /data/benchmark \
    && chown -R appuser:appuser /data
USER appuser

LABEL org.opencontainers.image.title="invoice-extractor" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.description="Batch invoice PDF extraction to Excel (text + vision LLM routes)." \
      org.opencontainers.image.source="local"

# tini forwards signals so Ctrl+C interrupts Python cleanly (exit 130).
ENTRYPOINT ["tini", "--", "invoice-extractor"]
# Harmless default: print help, make no provider calls.
CMD ["--help"]
