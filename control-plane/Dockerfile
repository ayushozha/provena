FROM golang:1.23-bookworm AS builder

ARG CMD=gateway
WORKDIR /src

COPY go.mod /src/go.mod
COPY cmd /src/cmd
COPY internal /src/internal

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/service ./cmd/${CMD}

FROM debian:bookworm-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/service /usr/local/bin/provena-control-plane

EXPOSE 8080 8090 8091 8092

ENTRYPOINT ["/usr/local/bin/provena-control-plane"]
