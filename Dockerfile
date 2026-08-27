# cogame-halite Coworld image: game server + bundled players + the static
# replay-viewer bundle, in ONE image.
#
# Stage 1 (wasm-builder) compiles the Nim -> emscripten static replay viewer
# with coworld-ctf's pinned toolchain (emsdk 4.0.15 + nimby 0.1.27 + Nim 2.2.4
# + `nimby --global sync nimby.lock`) into viewer/dist. It runs on
# $BUILDPLATFORM: wasm output is architecture-independent, so an ARM host need
# not emulate x86 to compile it.
#
# Stage 2 is the linux/amd64 runtime: python:3.12-slim + locked deps via uv.
# The repo layout is preserved at /workspace (server code resolves vendor/,
# build/khalite/ and viewer/dist relative to the repo root, and PYTHONPATH
# covers server/ and the root), so the project is NOT pip-installed.
#
# Entrypoints (Coworld manifest `run`):
#   game    python -m cogame_halite.server     (also /bin/halite)
#   players python -m players.halite_player    (also /bin/halite-player)
#
# Build: docker build --platform=linux/amd64 -t coworld-halite:local .

FROM --platform=$BUILDPLATFORM emscripten/emsdk:4.0.15 AS wasm-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL -o /usr/local/bin/nimby \
      https://github.com/treeform/nimby/releases/download/0.1.27/nimby-Linux-X64 && \
    echo "3b3084394bd26b09f84a3f82389f075221c8784893238390939d71dd66ac9e8b  /usr/local/bin/nimby" | sha256sum -c - && \
    chmod +x /usr/local/bin/nimby && \
    nimby use 2.2.4

ENV PATH="/root/.nimby/nim/bin:$PATH"

WORKDIR /workspace
COPY nimby.lock .
RUN nimby --global sync nimby.lock

COPY replay-viewer/ replay-viewer/
COPY client/ client/
COPY data/ data/
COPY viewer/ viewer/
RUN bash viewer/build_viewer.sh && test -f viewer/dist/index.html


# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /workspace

# Locked runtime deps only (aiohttp / anthropic / numpy). uv is bind-mounted
# from its distribution image for this RUN only, so it never becomes a layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.8.17,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-project

ENV PATH="/workspace/.venv/bin:$PATH" \
    PYTHONPATH="/workspace/server:/workspace" \
    PYTHONUNBUFFERED=1

COPY vendor/ vendor/
COPY sim/ sim/
COPY server/ server/
COPY players/ players/
COPY data/ data/
COPY --from=wasm-builder /workspace/viewer/dist/ viewer/dist/

# Assemble vendor + shim into build/khalite at BUILD time so the running
# container never writes to its own image layer.
RUN python sim/assemble.py --check

# The raw-docker smoke (tools/ci/docker_smoke.sh) runs `<image> /bin/halite`
# and `<image> /bin/halite-player` by default. Give it exactly that.
RUN printf '#!/bin/sh\nexec python -m cogame_halite.server "$@"\n' > /bin/halite && \
    printf '#!/bin/sh\nexec python -m players.halite_player "$@"\n' > /bin/halite-player && \
    chmod +x /bin/halite /bin/halite-player

CMD ["python", "-m", "cogame_halite.server"]
