# ---------- Builder: resolve & install dependencies into /app/.venv ----------
# Build the venv on the SAME base image it will run on, so the venv's interpreter
# matches the runtime exactly (no "incompatible environment" patch-version drift).
FROM python:3.12-slim-bookworm AS builder

# Vendor a pinned uv (the binary lives at /uv in the distroless uv image).
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

# - UV_COMPILE_BYTECODE: precompile .pyc at install time for faster cold starts
# - UV_LINK_MODE=copy:    copy from uv's cache into the venv (no hardlink warnings)
# - UV_PYTHON_DOWNLOADS=0: use the image's system CPython (never download a managed
#   Python), so the venv targets /usr/local/bin/python3.12 — valid in the runtime stage
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install ONLY third-party dependencies first, keyed on the lockfile + manifest.
# This layer is cached and reused on every build unless pyproject.toml/uv.lock
# change, so editing source no longer triggers a full dependency reinstall.
# --no-dev drops black/ruff; --no-install-project keeps the project itself out of
# this layer. Plain COPY (not BuildKit mounts) so it builds on any builder.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

# Bring in the application source and finalize the environment.
COPY . /app
RUN uv sync --locked --no-dev

# ---------- Runtime: same base, with Python, uv, the venv + app code ----------
FROM python:3.12-slim-bookworm

# No .pyc at runtime (already compiled in the venv); unbuffered logs; never let
# uv try to fetch a managed Python at startup (use the one in the copied venv).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=0

# Vendor the same pinned uv so the app can still start "the uv way" (uv run).
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Copy the prebuilt virtualenv AND the application source from the builder stage.
# Both are required: "settledown" is a virtual (non-packaged) project, so the
# source must be present at /app and started via `uv run` for it to be importable.
COPY --from=builder --chown=appuser:appuser /app /app

USER appuser

# Django's default port (docker-compose maps 8000:8000).
EXPOSE 8000

# Start the Django ASGI app the uv way. --no-sync: the venv is already built into
# the image, so don't re-sync or hit the network at startup (also avoids pulling in
# the dev group). JSON/exec form keeps uv as PID 1; modern uv forwards SIGTERM to
# hypercorn, so `docker stop` / redeploys shut down gracefully.
CMD ["uv", "run", "--no-sync", "hypercorn", "--bind", "0.0.0.0:8000", "settledown.asgi:application"]
