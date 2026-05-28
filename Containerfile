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
# Layer strategy (from most-cacheable to least):
#   1. apt system deps
#   2. uv binary
#   3. runtime user
#   4. python deps         (changes only when pyproject.toml / uv.lock changes)
#   5. torch swap          (changes only when TORCH_INDEX_URL build arg changes)
#   6. spaCy model (~500MB) (changes only when spacy version changes)
#   7. app source + project install  ← only this re-runs on a code edit
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
    UV_CACHE_DIR=/root/.cache/uv \
    PATH=/opt/venv/bin:/root/.local/bin:$PATH \
    HF_HOME=/home/audiblez/.cache/huggingface

# --- 1. system deps (python3 + venv only matter on the CUDA base; the
#        python:3.11-slim image already ships /usr/bin/python).
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

# --- 2. uv binary — version-pinned, same binary works under docker / podman / buildah.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

# --- 3. runtime user, created up front so later COPYs don't bust this layer.
RUN useradd -u 1000 -m -s /bin/bash audiblez \
    && mkdir -p /app/outputs /home/audiblez/.cache \
    && chown -R audiblez:audiblez /app /home/audiblez

WORKDIR /app

# --- 4. python dependencies. Only invalidated by pyproject.toml / uv.lock changes.
#        README.md is included because hatchling reads it for package metadata.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra web

# --- 5. torch swap (CPU vs CUDA wheels). Cached as long as TORCH_INDEX_URL is unchanged.
#        uv-created venvs don't ship pip, so use `uv pip` which targets the venv via VIRTUAL_ENV.
RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/opt/venv uv pip install --upgrade \
        --index-url "${TORCH_INDEX_URL}" \
        --extra-index-url https://pypi.org/simple \
        torch

# --- 6. spaCy multilingual model (~500MB). Cached unless the spacy install above changes.
#        Pre-downloading here means the first web request doesn't stall on a model fetch.
RUN /opt/venv/bin/python -m spacy download xx_ent_wiki_sm

# --- 7. app source + project install. THIS is the only layer a code edit invalidates.
#        `uv sync` re-runs but only rebuilds and installs the local audiblez wheel — all
#        external deps and the spaCy model are already in place.
COPY audiblez ./audiblez
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra web

USER audiblez
EXPOSE 8009
CMD ["audiblez-web"]
