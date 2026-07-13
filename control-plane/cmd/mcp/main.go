// Command mcp runs the Provena MCP server on :8090.
// Provides HTTP/SSE for LLM clients implementing the Model Context Protocol.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/altrixy/provena/control-plane/internal/gateway"
	"github.com/altrixy/provena/control-plane/internal/mcp"
)

const maxSSEConnections = 64

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
	requireAuth := !strings.EqualFold(env("PROVENA_MCP_REQUIRE_AUTH", "true"), "false")
	server := mcp.NewMCPServer("provena-mcp", "0.1.0", upstreamURL)

	srv := &http.Server{
		Addr:         listenAddr,
		Handler:      buildHandler(server, requireAuth),
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

	logger.Info("mcp server starting", "addr", listenAddr, "auth_required", requireAuth)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		logger.Error("mcp server failed", "error", err)
		os.Exit(1)
	}
}

func buildHandler(server *mcp.MCPServer, requireAuth bool) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})
	var rpcHandler http.Handler = server.RPCHandler()
	var sseHandler http.Handler = mcp.LimitConcurrentConnections(
		server.SSEHandler(),
		maxSSEConnections,
	)
	if requireAuth {
		rpcHandler = server.RequireGatewayBearerAuth(rpcHandler)
		sseHandler = server.RequireGatewayBearerAuth(sseHandler)
	}
	// Reject unauthenticated floods before gateway validation and bound the
	// number of long-lived SSE streams admitted by this process.
	clientRateLimit := gateway.NewRateLimiter(20, 40)
	rpcHandler = gateway.ClientIPRateLimitMiddleware(clientRateLimit)(rpcHandler)
	sseHandler = gateway.ClientIPRateLimitMiddleware(clientRateLimit)(sseHandler)
	mux.Handle("POST /rpc", rpcHandler)
	mux.Handle("GET /sse", sseHandler)
	return mux
}
