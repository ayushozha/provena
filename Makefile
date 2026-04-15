.PHONY: all build test clean dev help
.PHONY: build-rust build-go build-python
.PHONY: test-rust test-go test-python test-integration
.PHONY: run-store run-orchestration run-gateway run-intelligence run-mcp run-queue run-lifecycle
.PHONY: docker-up docker-down proto

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

all: build test ## Build and test everything

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build: build-rust build-go build-python ## Build all services

build-rust: ## Build Rust orchestration layer
	cd orchestration && cargo build --release

build-go: ## Build Go control plane
	cd control-plane && go build ./...

build-python: ## Verify Python intelligence layer
	cd intelligence && python -m py_compile app/main.py

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test: test-rust test-go test-python ## Run all tests

test-rust: ## Run Rust tests
	cd orchestration && cargo test

test-go: ## Run Go tests (when tests are added)
	cd control-plane && go vet ./...

test-python: ## Run Python intelligence tests
	cd intelligence && python -m pytest tests/ -v

test-store: ## Run existing store tests
	python -m pytest tests/ -v

test-integration: ## Run cross-service E2E tests
	python scripts/run_e2e.py

# ---------------------------------------------------------------------------
# Run individual services (development)
# ---------------------------------------------------------------------------

run-store: ## Run core memory store (port 8000)
	uvicorn app.main:app --reload --port 8000

run-orchestration: ## Run Rust orchestration (port 50051)
	cd orchestration && cargo run

run-gateway: ## Run Go API gateway (port 8080)
	cd control-plane && go run ./cmd/gateway

run-intelligence: ## Run Python intelligence layer (port 8081)
	cd intelligence && uvicorn app.main:app --reload --port 8081

run-mcp: ## Run MCP server (port 8090)
	cd control-plane && go run ./cmd/mcp

run-queue: ## Run queue consumer (port 8091)
	cd control-plane && go run ./cmd/queue

run-lifecycle: ## Run lifecycle governance (port 8092)
	cd control-plane && go run ./cmd/lifecycle

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

docker-up: ## Start all services via docker-compose
	docker compose up --build -d

docker-down: ## Stop all services
	docker compose down

# ---------------------------------------------------------------------------
# Dev utilities
# ---------------------------------------------------------------------------

dev: ## Start store + orchestration + intelligence for local dev
	@echo "Start these in separate terminals:"
	@echo "  make run-store          # port 8000"
	@echo "  make run-orchestration  # port 50051"
	@echo "  make run-intelligence   # port 8081"
	@echo "  make run-gateway        # port 8080"

clean: ## Clean build artifacts
	cd orchestration && cargo clean
	rm -rf intelligence/app/__pycache__ intelligence/tests/__pycache__
	rm -rf data/

schema: ## Apply storage schema to SQLite
	sqlite3 data/provena.db < storage/migrations/001_initial.sql

openapi: ## Export OpenAPI spec
	python scripts/export_openapi.py
