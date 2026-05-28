# syntax=docker/dockerfile:1.7
# Audiblez container — supports CPU and CUDA via build args.
#
# CPU build (default):
#   docker build -t audiblez:cpu .
#   podman build -t audiblez:cpu .
#
# CUDA build:
#   docker build -t audiblez:cuda \
#     --build-arg BASE_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 \
#     --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 .
#
# Runs as UID 1000 so files in bind-mounted /app/outputs land with sane
# ownership under rootless podman.

ARG BASE_IMAGE=python:3.11-slim
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

FROM ${BASE_IMAGE} AS runtime
ARG TORCH_INDEX_URL

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH=/opt/venv/bin:/root/.local/bin:$PATH \
    HF_HOME=/home/audiblez/.cache/huggingface \
    XDG_CACHE_HOME=/home/audiblez/.cache

# System deps. python3 + python-is-python3 only matter for the CUDA base
# (the python:3.11-slim image already ships /usr/bin/python).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        espeak-ng \
        libespeak-ng1 \
        ca-certificates \
        curl \
        python3 \
        python3-venv \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy uv from its distroless image — same binary works under docker/podman/buildah.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install Python dependencies first so we cache the heavy layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra web

# Swap torch for the build-arg-specified variant (CPU or CUDA wheels).
# This is a no-op when TORCH_INDEX_URL already matches what uv installed.
RUN /opt/venv/bin/pip install --quiet --upgrade \
        --index-url "${TORCH_INDEX_URL}" \
        --extra-index-url https://pypi.org/simple \
        torch

# Copy app source and install the package itself.
COPY audiblez ./audiblez
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra web

# Pre-download the spaCy multilingual model so the first request doesn't stall.
# Kokoro weights download on first synthesis; mount a volume on HF_HOME to cache.
RUN /opt/venv/bin/python -m spacy download xx_ent_wiki_sm

# Non-root user so bind-mounted outputs end up owned by the host user under rootless podman.
RUN useradd -u 1000 -m -s /bin/bash audiblez \
    && mkdir -p /app/outputs /home/audiblez/.cache \
    && chown -R audiblez:audiblez /app /home/audiblez

USER audiblez
EXPOSE 8000
CMD ["audiblez-web"]
