# XboxDownload-Web — multi-arch image (linux/amd64 + linux/arm64)
#
# The application is pure Python, so the image is assembled without ever
# executing a target-architecture binary during the build:
#
#   stage "builder"  — pinned to $BUILDPLATFORM (the build machine's
#                      architecture) and uses `pip install --target
#                      --platform ...` to lay down the wheels for the
#                      TARGET architecture.
#   stage "runtime"  — pinned to $TARGETPLATFORM; copies those
#                      target-architecture files into the target base
#                      image. Nothing is executed here, so no binfmt_misc /
#                      QEMU emulation is required.
#
# Consequence: an amd64 image can be produced on an arm64 builder without
# binfmt_misc / QEMU emulation, and vice versa.

ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm
# The builder always runs on the build machine's own architecture.
ARG BUILDER_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm

# ---------------------------------------------------------------------------
# Stage 1: resolve dependencies for the TARGET architecture.
# ---------------------------------------------------------------------------
FROM --platform=$BUILDPLATFORM ${BUILDER_IMAGE} AS builder

ARG TARGETARCH
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /build

COPY requirements.txt .

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) PLATFORM_ARGS="--platform manylinux2014_x86_64" ;; \
      arm64) PLATFORM_ARGS="--platform manylinux2014_aarch64" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    python -m pip install --no-cache-dir --upgrade pip; \
    python -m pip install \
      --only-binary=:all: \
      --implementation cp \
      --python-version 3.11 \
      --abi cp311 \
      ${PLATFORM_ARGS} \
      --target /site-packages \
      --index-url "${PIP_INDEX_URL}" \
      --trusted-host "${PIP_TRUSTED_HOST}" \
      -r requirements.txt; \
    ls -1 /site-packages | wc -l

# ---------------------------------------------------------------------------
# Stage 2: assemble the runtime image for the target architecture.
# ---------------------------------------------------------------------------
FROM --platform=$TARGETPLATFORM ${BASE_IMAGE} AS runtime

ARG TARGETPLATFORM
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/site-packages \
    XBD_STATE_DIR=/state \
    XBD_DATA_DIR=/app/data \
    XBD_WEB_DIR=/app/web \
    XBD_HTTP_HOST=0.0.0.0 \
    XBD_HTTP_PORT=8080

WORKDIR /app

# The target-architecture packages are copied in as plain files — no target
# binary is executed during the build.
COPY --from=builder /site-packages /site-packages

# Application code and static data.
COPY app/ /app/app/
COPY web/ /app/web/
COPY data/ /app/data/

# /state holds settings.json, host mappings and the generated certificates.
# Docker creates the mount point for the volume automatically.
VOLUME ["/state"]

EXPOSE 8080 53 80 443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["sh", "-c", "exec python -m uvicorn app.main:app --host \"${XBD_HTTP_HOST:-0.0.0.0}\" --port \"${XBD_HTTP_PORT:-8080}\""]
