#!/usr/bin/env bash
#
# Build a multi-architecture image (linux/amd64 + linux/arm64).
#
# The application is pure Python, so the Dockerfile resolves wheels for the
# target platform instead of compiling. That means an amd64 image can be
# produced on an arm64 builder WITHOUT binfmt_misc / QEMU emulation.
#
# Usage:
#   ./scripts/build-multiarch.sh                 # build + push both arches
#   ./scripts/build-multiarch.sh --load          # build only, load locally
#   ./scripts/build-multiarch.sh --no-push       # build only
#
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-xboxdownload-web}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
REGISTRY="${REGISTRY:-}"
LOAD_LOCAL=0
PUSH=1

for arg in "$@"; do
  case "$arg" in
    --load)    LOAD_LOCAL=1 ;;
    --no-push) PUSH=0 ;;
    --push)    PUSH=1 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

if [ -n "$REGISTRY" ]; then
  FULL_NAME="${REGISTRY}/${IMAGE_NAME}:${TAG}"
else
  FULL_NAME="${IMAGE_NAME}:${TAG}"
fi

cd "$(dirname "$0")/.."

echo "==> Building ${FULL_NAME} for ${PLATFORMS}"

if [ "$LOAD_LOCAL" -eq 1 ] || [ "$PUSH" -eq 0 ]; then
  # Without a registry we cannot produce a multi-arch manifest locally
  # (docker load only accepts a single architecture), so build each
  # architecture separately and tag it explicitly.
  for platform in ${PLATFORMS//,/ }; do
    arch="${platform#linux/}"
    echo "--> Building ${IMAGE_NAME}:${TAG}-${arch}"
    docker build \
      --platform "linux/${arch}" \
      --build-arg "BASE_IMAGE=${BASE_IMAGE:-docker.m.daocloud.io/library/python:3.11-slim-bookworm}" \
      --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}" \
      -t "${IMAGE_NAME}:${TAG}-${arch}" \
      -t "${IMAGE_NAME}:${TAG}" \
      .
  done
else
  docker buildx build \
    --platform "${PLATFORMS}" \
    --build-arg "BASE_IMAGE=${BASE_IMAGE:-docker.m.daocloud.io/library/python:3.11-slim-bookworm}" \
    --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}" \
    -t "${FULL_NAME}" \
    --push \
    .
fi

echo "==> Done: ${FULL_NAME}"
