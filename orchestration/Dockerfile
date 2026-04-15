FROM rust:1.85-bookworm AS builder

WORKDIR /src

COPY Cargo.toml /src/Cargo.toml
COPY src /src/src

RUN cargo build --release

FROM debian:bookworm-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/target/release/provena-orchestration /usr/local/bin/provena-orchestration

EXPOSE 50051

ENTRYPOINT ["/usr/local/bin/provena-orchestration"]
