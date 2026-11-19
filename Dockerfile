# Multi-stage build: compile deps and frontend separately, ship only runtime files.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

# gcc: vendored tree-sitter grammars are C extensions built by their setup.py.
# cargo: simvec (crates/simvec) is a Rust cdylib providing the local vector engine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev cargo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install locked dependencies into a project venv; --frozen refuses lock drift.
# vendor/ holds the uv workspace members (tree-sitter-groovy, tree-sitter-kotlin)
# and must exist before sync; sync compiles their grammar extensions in place.
COPY pyproject.toml uv.lock .python-version ./
COPY vendor ./vendor
RUN uv sync --frozen --no-dev --compile-bytecode

# Build the simvec native library (c_api feature exposes the C ABI used by vec_store).
COPY crates ./crates
RUN cargo build --release --features c_api --manifest-path crates/simvec/Cargo.toml \
    && cp target/release/libsimvec.so /usr/local/lib/libsimvec.so \
    && ldconfig

FROM node:22-bookworm-slim AS frontend

WORKDIR /webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui ./
RUN npm run build

FROM python:3.14-slim-bookworm AS runtime

# Run as an unprivileged user; data lives in mounted volumes.
RUN useradd --create-home --uid 1000 libref

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /usr/local/lib/libsimvec.so /usr/local/lib/libsimvec.so
COPY --from=builder /app/crates/simvec/bindings /app/crates/simvec/bindings
COPY --from=frontend /webui/dist /app/webui/dist
COPY src /app/src
COPY pyproject.toml uv.lock .python-version README.md* ./

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
