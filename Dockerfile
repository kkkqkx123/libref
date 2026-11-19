# Multi-stage build: compile deps and frontend separately, ship only runtime files.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

# gcc: vendored tree-sitter grammars are C extensions built by their setup.py.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install locked dependencies into a project venv; --frozen refuses lock drift.
# vendor/ holds the uv workspace members (tree-sitter-groovy, tree-sitter-kotlin)
# and must exist before sync; sync compiles their grammar extensions in place.
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src ./src
COPY vendor ./vendor
# Non-editable install keeps the runtime venv self-contained.
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

FROM node:22-bookworm-slim AS frontend

WORKDIR /webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui ./
RUN npm run build

FROM rust:1-bookworm AS simvec

WORKDIR /app
# Cargo.lock v4 needs a newer toolchain than Debian bookworm's cargo.
ENV CARGO_HTTP_LOW_SPEED_LIMIT=0 \
    CARGO_HTTP_TIMEOUT=600 \
    CARGO_NET_RETRY=10
COPY crates ./crates
RUN cargo build --release --features c_api --manifest-path crates/simvec/Cargo.toml --target-dir /app/target \
    && cp /app/target/release/libsimvec.so /usr/local/lib/libsimvec.so

FROM python:3.14-slim-bookworm AS runtime

# Run as an unprivileged user; data lives in mounted volumes.
RUN useradd --create-home --uid 1000 libref \
    && mkdir -p /data /home/libref/.local/share/libref \
    && chown -R libref:libref /data /home/libref

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=simvec /usr/local/lib/libsimvec.so /usr/local/lib/libsimvec.so
COPY --from=frontend /webui/dist /app/webui/dist

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    LIBREF_WEB_SPA_DIR=/app/webui/dist \
    SIMVEC_LIB=/usr/local/lib/libsimvec.so

# ldconfig cache for the copied simvec shared object.
RUN ldconfig

USER libref
VOLUME ["/home/libref/.local/share/libref"]

# Default serve port matches web_port in config.py; TLS terminates at the reverse proxy, never here.
EXPOSE 8001

CMD ["libref", "serve", "--host", "0.0.0.0", "--port", "8001"]
