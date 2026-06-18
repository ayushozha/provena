// Command mcp runs the Provena MCP server on :8090.
// Provides HTTP/SSE for LLM clients implementing the Model Context Protocol.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/altrixy/provena/control-plane/internal/mcp"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	listenAddr := env("PROVENA_MCP_LISTEN_ADDR", ":8090")
	upstreamURL := env("PROVENA_GATEWAY_URL", "http://localhost:8080")
	// Service credential the MCP server presents to the gateway as a Bearer
	// token, so its forwarded calls authenticate as a real principal.
	serviceAPIKey := env("PROVENA_SERVICE_API_KEY", "")

	server := mcp.NewMCPServer("provena-mcp", "0.1.0", upstreamURL, serviceAPIKey)

	mux := http.NewServeMux()

	// Health check
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})

	// JSON-RPC endpoint
	mux.HandleFunc("POST /rpc", server.RPCHandler())

	// SSE endpoint for LLM clients
	mux.HandleFunc("GET /sse", server.SSEHandler())

	srv := &http.Server{
		Addr:         listenAddr,
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 0, // SSE needs no write timeout
		IdleTimeout:  120 * time.Second,
	}

	// Graceful shutdown
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		logger.Info("shutting down MCP server")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
	}()

	logger.Info("mcp server starting", "addr", listenAddr)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		logger.Error("mcp server failed", "error", err)
		os.Exit(1)
	}
}
