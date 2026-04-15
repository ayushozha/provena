// Command gateway runs the Provena API gateway on :8080.
// Middleware chain: trace -> metrics -> logging -> auth -> rate-limit.
package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/altrixy/provena/control-plane/internal/auth"
	"github.com/altrixy/provena/control-plane/internal/gateway"
	"github.com/altrixy/provena/control-plane/internal/observability"
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

	listenAddr := env("PROVENA_LISTEN_ADDR", ":8080")
	orchestrationURL := env("PROVENA_ORCHESTRATION_URL", "http://localhost:50051")
	intelligenceURL := env("PROVENA_INTELLIGENCE_URL", "http://localhost:8081")
	storeURL := env("PROVENA_STORE_URL", "http://localhost:8000")
	lifecycleURL := env("PROVENA_LIFECYCLE_URL", "http://localhost:8092")
	authEnabled := strings.EqualFold(env("PROVENA_AUTH_ENABLED", "false"), "true")

	keyStore, err := auth.NewKeyStore()
	if err != nil {
		logger.Error("failed to load API keys", "error", err)
		os.Exit(1)
	}

	rl := gateway.NewRateLimiter(100, 200)
	ha := gateway.NewHealthAggregator(map[string]string{
		"orchestration": orchestrationURL + "/healthz",
		"intelligence":  intelligenceURL + "/healthz",
		"store":         storeURL + "/healthz",
		"lifecycle":     lifecycleURL + "/healthz",
	})

	orchestrationProxy := gateway.NewProxyHandler(orchestrationURL, 30*time.Second)
	intelligenceProxy := gateway.NewProxyHandler(intelligenceURL, 30*time.Second)
	storeProxy := gateway.NewProxyHandler(storeURL, 30*time.Second)
	lifecycleProxy := gateway.NewProxyHandler(lifecycleURL, 30*time.Second)

	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("GET /metrics", observability.MetricsHandler())

	mux.Handle("POST /v1/memories", intelligenceProxy.WithRewritePath("/v1/pipeline/write"))
	mux.Handle("POST /v1/memories/search", intelligenceProxy.WithRewritePath("/v1/pipeline/search"))
	mux.Handle("GET /v1/memories/{id}", storeProxy)
	mux.Handle("POST /v1/memories/relations", storeProxy)
	mux.Handle("DELETE /v1/memories/{id}", storeProxy)

	mux.Handle("POST /v1/project-snapshots", storeProxy)
	mux.Handle("GET /v1/project-snapshots/latest", storeProxy)
	mux.Handle("POST /v1/admin/erase", storeProxy)
	mux.Handle("POST /v1/integrations/connectors", storeProxy)
	mux.Handle("GET /v1/integrations/connectors", storeProxy)
	mux.Handle("GET /v1/integrations/connectors/{connector_id}", storeProxy)
	mux.Handle("POST /v1/integrations/connectors/{connector_id}/sources/batch", storeProxy)
	mux.Handle("GET /v1/integrations/connectors/{connector_id}/sources", storeProxy)
	mux.Handle("POST /v1/integrations/connectors/{connector_id}/principal-mappings/batch", storeProxy)
	mux.Handle("GET /v1/integrations/connectors/{connector_id}/principal-mappings", storeProxy)
	mux.Handle("POST /v1/integrations/connectors/{connector_id}/permissions/batch", storeProxy)
	mux.Handle("GET /v1/integrations/connectors/{connector_id}/permissions", storeProxy)
	mux.Handle("POST /v1/integrations/connectors/{connector_id}/sync-jobs", storeProxy)
	mux.Handle("GET /v1/integrations/connectors/{connector_id}/sync-jobs", storeProxy)
	mux.Handle("GET /v1/integrations/coverage", storeProxy)
	mux.Handle("POST /v1/admin/retention-policies", lifecycleProxy)
	mux.Handle("GET /v1/admin/retention-policies", lifecycleProxy)
	mux.Handle("POST /v1/admin/legal-hold", lifecycleProxy)
	mux.Handle("DELETE /v1/admin/legal-hold/{hold_id}", lifecycleProxy)
	mux.Handle("POST /v1/admin/rtbf", lifecycleProxy)
	mux.Handle("POST /v1/admin/retention/enforce", lifecycleProxy)

	mux.Handle("POST /v1/preflight", orchestrationProxy.WithRewritePath("/preflight"))
	mux.Handle("GET /v1/lifecycle/healthz", lifecycleProxy.WithRewritePath("/healthz"))

	mux.HandleFunc("GET /v1/cold-start", func(w http.ResponseWriter, r *http.Request) {
		results := ha.CheckAll(r.Context())
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"status":   "ready",
			"services": results,
		})
	})

	var handler http.Handler = mux
	handler = gateway.RateLimitMiddleware(rl)(handler)
	handler = auth.AuthMiddleware(keyStore, authEnabled)(handler)
	handler = gateway.RequestLogger(handler)
	handler = observability.MetricsMiddleware(handler)
	handler = observability.TraceMiddleware(handler)

	srv := &http.Server{
		Addr:         listenAddr,
		Handler:      handler,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		logger.Info("shutting down gateway")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
	}()

	logger.Info("gateway starting", "addr", listenAddr, "auth_enabled", authEnabled)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		logger.Error("gateway failed", "error", err)
		os.Exit(1)
	}
}
