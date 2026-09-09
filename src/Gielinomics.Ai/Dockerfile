# Build context is this directory, not the repo root: nothing here needs the
# .NET projects, and a root context would ship the whole solution into the
# image's build cache to copy two files out of it.
FROM python:3.12-slim AS build
WORKDIR /app

# uv rather than pip, to install from the lockfile the repo already has. A
# resolver that picks versions at image build time makes the container a
# different environment from the one the tests ran in.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies before source. They change on the order of once a month and the
# source changes every commit, so this layer is the one worth keeping cached --
# fastembed pulls onnxruntime, which is not a small download.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim
WORKDIR /app

# curl for the healthcheck. The alternative is a Python one-liner, which would
# start an interpreter and import httpx every thirty seconds to make one GET.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# Not root. Nothing here needs to write outside /app/data, which is a volume.
RUN useradd --create-home --uid 10001 reldo
COPY --from=build --chown=reldo:reldo /app /app

USER reldo
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# The index is a ~330 MB build artifact, mounted rather than baked in: it is
# rebuilt from the wiki on its own schedule, and baking it would make every
# image rebuild a 330 MB push of bytes that did not change.
ENV RELDO_INDEX_PATH=/app/data/index.npz

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8100/health || exit 1

# Search only by default. `reldo bot` and the model-backed ask path are opt-in
# through the compose command, because both need something this container does
# not provide: a Discord token and a GPU box respectively.
CMD ["reldo", "serve", "--host", "0.0.0.0", "--port", "8100", "--no-model"]
