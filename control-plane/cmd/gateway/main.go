// Command gateway runs the Provena API gateway on :8080.
// Middleware chain: trace -> metrics -> logging -> client-IP limit -> [browser access] -> auth -> tenant limit -> write-permission.
package main

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path"
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

func splitList(value string) []string {
	var items []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			items = append(items, item)
		}
	}
	return items
}

// isLoopback reports whether a host:port address can only be reached from this machine.
func isLoopback(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
	}
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// isLoopbackURL reports whether an upstream URL is plain http to a loopback host:port with nothing else on it:
// no userinfo, path, query or fragment that could steer the proxy somewhere other than the private store.
func isLoopbackURL(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "http" || u.User != nil || u.Opaque != "" || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return false
	}
	return isLoopback(u.Host)
}

func healthz(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"ok"}`))
}

// canonicalPathOnly refuses a request whose path is not already canonical: an encoded slash or dot (%2F, %2e)
// or a traversal segment would reach the private store under a path this gateway never matched. Memory ids
// are store-generated UUIDs, so no legitimate local-store request is affected.
func canonicalPathOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawPath != "" || path.Clean(r.URL.Path) != r.URL.Path {
			http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// localRouter is the whole surface of local-store mode: health, key identity and the memory routes a person's
// own notes need, all to the private store with the gateway's transport credential. No admin, erase,
// lifecycle, integration, relation, feedback, procedure or pipeline routes, and deletes are always soft: query
// options are refused rather than forwarded.
func localRouter(storeURL string, credential *gateway.UpstreamCredential) *http.ServeMux {
	store := canonicalPathOnly(gateway.NewProxyHandler(storeURL, 30*time.Second).WithUpstreamCredential(credential))
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)
	mux.Handle("GET /v1/auth/validate", auth.VerifiedKeyHandler())
	mux.Handle("GET /v1/auth/whoami", auth.WhoAmIHandler())
	mux.Handle("POST /v1/memories", store)
	mux.Handle("POST /v1/memories/search", store)
	mux.Handle("GET /v1/memories/{id}", store)
	mux.Handle("PUT /v1/memories/{id}", store)
	mux.Handle("GET /v1/memories/{id}/history", store)
	mux.HandleFunc("DELETE /v1/memories/{id}", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawQuery != "" {
			http.Error(w, `{"error":"local-store mode deletes are always soft; query options are not accepted"}`, http.StatusForbidden)
			return
		}
		store.ServeHTTP(w, r)
	})
	return mux
}

type routerConfig struct {
	orchestrationURL string
	intelligenceURL  string
	storeURL         string
	lifecycleURL     string
	credential       *gateway.UpstreamCredential
	health           *gateway.HealthAggregator
}

// newRouter is the shipped pipeline-mode surface, unchanged apart from the whoami route.
func newRouter(cfg routerConfig) *http.ServeMux {
	orchestrationProxy := gateway.NewProxyHandler(cfg.orchestrationURL, 30*time.Second)
	intelligenceProxy := gateway.NewProxyHandler(cfg.intelligenceURL, 30*time.Second).WithUpstreamCredential(cfg.credential)
	storeProxy := gateway.NewProxyHandler(cfg.storeURL, 30*time.Second).WithUpstreamCredential(cfg.credential)
	lifecycleProxy := gateway.NewProxyHandler(cfg.lifecycleURL, 30*time.Second).WithUpstreamCredential(cfg.credential)

	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", healthz)
	mux.Handle("GET /readyz", gateway.ReadinessHandler(cfg.health))
	mux.Handle("GET /v1/auth/validate", auth.VerifiedKeyHandler())
	mux.Handle("GET /v1/auth/whoami", auth.WhoAmIHandler())
	mux.HandleFunc("GET /metrics", observability.MetricsHandler())
	mux.Handle("GET /ui/observability", storeProxy)
	mux.Handle("GET /ui/assets/{file...}", storeProxy)

	mux.Handle("POST /v1/memories", intelligenceProxy.WithRewritePath("/v1/pipeline/write"))
	mux.Handle("POST /v1/memories/search", intelligenceProxy.WithRewritePath("/v1/pipeline/search"))
	mux.Handle("POST /v1/procedures/abstract", intelligenceProxy)
	mux.Handle("POST /v1/pipeline/write", intelligenceProxy)
	mux.Handle("POST /v1/pipeline/search", intelligenceProxy)
	mux.Handle("POST /v1/agent/context", storeProxy)
	mux.Handle("GET /v1/memories/{id}", storeProxy)
	mux.Handle("PUT /v1/memories/{id}", storeProxy)
	mux.Handle("GET /v1/memories/{id}/history", storeProxy)
	mux.Handle("POST /v1/memories/{id}/feedback", storeProxy)
	mux.Handle("GET /v1/memories/{id}/feedback", storeProxy)
	mux.Handle("POST /v1/memories/graph/temporal", storeProxy)
	mux.Handle("POST /v1/memories/relations", storeProxy)
	mux.Handle("DELETE /v1/memories/{id}", storeProxy)
	mux.Handle("POST /v1/repositories/{repository_id}/memory-events/sync", storeProxy)
	mux.Handle("GET /v1/admin/memories/{id}/inspect", storeProxy)
	mux.Handle("POST /v1/admin/memories/search/explain", storeProxy)

	mux.Handle("POST /v1/project-snapshots", storeProxy)
	mux.Handle("GET /v1/project-snapshots/latest", storeProxy)
	mux.Handle("POST /v1/admin/erase", storeProxy)
	mux.Handle("GET /v1/admin/observability/storage-overview", storeProxy)
	mux.Handle("GET /v1/admin/onboarding/status", storeProxy)
	mux.Handle("GET /v1/admin/evidence/production-readiness", storeProxy)
	mux.Handle("GET /v1/integrations/catalog", storeProxy)
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

	mux.Handle("GET /v1/cold-start", gateway.ReadinessHandler(cfg.health))
	return mux
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	listenAddr := env("PROVENA_LISTEN_ADDR", ":8080")
	orchestrationURL := env("PROVENA_ORCHESTRATION_URL", "http://localhost:50051")
	intelligenceURL := env("PROVENA_INTELLIGENCE_URL", "http://localhost:8081")
	storeURL := env("PROVENA_STORE_URL", "http://localhost:8000")
	lifecycleURL := env("PROVENA_LIFECYCLE_URL", "http://localhost:8092")
	// Secure by default: auth is ON unless an operator explicitly opts out.
	// When disabled, AuthMiddleware injects an anonymous super-admin context,
	// so a missing/default value must NOT silently grant full access.
	authEnabled := !strings.EqualFold(env("PROVENA_AUTH_ENABLED", "true"), "false")
	if !authEnabled {
		logger.Warn("PROVENA_AUTH_ENABLED=false — auth is DISABLED; every request runs as anonymous super-admin. Do not run this in production.")
	}

	keyStore, err := auth.NewKeyStore()
	if err != nil {
		logger.Error("failed to load API keys", "error", err)
		os.Exit(1)
	}

	upstreamCredential, err := gateway.LoadUpstreamCredential(os.Getenv, authEnabled)
	if err != nil {
		logger.Error("failed to load gateway upstream credential", "error", err)
		os.Exit(1)
	}
	if err := upstreamCredential.ValidateCredentialSeparation(keyStore); err != nil {
		logger.Error("invalid gateway credential configuration", "error", err)
		os.Exit(1)
	}

	// PROVENA_MEMORY_MODE=local-store is the on-device deployment for a browser app on the same machine: only
	// the memory routes, straight to a private loopback store, with auth on, a loopback listener, exact browser
	// origins and hosts, and every key bound to one workspace and person. "pipeline" is the shipped graph.
	memoryMode := env("PROVENA_MEMORY_MODE", "pipeline")
	localStore := memoryMode == "local-store"
	if !localStore && memoryMode != "pipeline" {
		logger.Error("PROVENA_MEMORY_MODE must be \"pipeline\" or \"local-store\"", "value", memoryMode)
		os.Exit(1)
	}
	if localStore {
		fail := func(msg string, args ...any) {
			logger.Error(msg, args...)
			os.Exit(1)
		}
		if !authEnabled {
			fail("PROVENA_MEMORY_MODE=local-store requires PROVENA_AUTH_ENABLED=true")
		}
		if !isLoopback(listenAddr) {
			fail("PROVENA_MEMORY_MODE=local-store requires a loopback PROVENA_LISTEN_ADDR such as 127.0.0.1:8787", "addr", listenAddr)
		}
		if !isLoopbackURL(storeURL) {
			fail("PROVENA_MEMORY_MODE=local-store requires PROVENA_STORE_URL to be a plain http loopback origin with a port, such as http://127.0.0.1:8700", "url", storeURL)
		}
		if unbound := keyStore.Unbound(); len(unbound) > 0 {
			fail("PROVENA_MEMORY_MODE=local-store requires every key to carry scope.workspace_id and scope.user_id", "unbound_key_ids", unbound)
		}
	}
	browserOrigins := splitList(os.Getenv("PROVENA_BROWSER_ORIGINS"))
	browserHosts := splitList(os.Getenv("PROVENA_BROWSER_HOSTS"))
	var browser *gateway.BrowserAccess
	if localStore || len(browserOrigins) > 0 || len(browserHosts) > 0 {
		access, err := gateway.NewBrowserAccess(browserOrigins, browserHosts)
		if err != nil {
			logger.Error("PROVENA_BROWSER_ORIGINS and PROVENA_BROWSER_HOSTS must both list exact values", "error", err)
			os.Exit(1)
		}
		browser = access
	}

	tenantRL := gateway.NewRateLimiter(100, 200)
	clientIPRL := gateway.NewRateLimiter(200, 400)

	var mux *http.ServeMux
	if localStore {
		mux = localRouter(storeURL, upstreamCredential)
	} else {
		mux = newRouter(routerConfig{
			orchestrationURL: orchestrationURL,
			intelligenceURL:  intelligenceURL,
			storeURL:         storeURL,
			lifecycleURL:     lifecycleURL,
			credential:       upstreamCredential,
			health: gateway.NewHealthAggregator(map[string]string{
				"orchestration": orchestrationURL + "/healthz",
				"intelligence":  intelligenceURL + "/healthz",
				"store":         storeURL + "/readyz",
				"lifecycle":     lifecycleURL + "/healthz",
			}),
		})
	}

	var handler http.Handler = mux
	// Execution order is client-IP limit -> browser access -> auth -> tenant limit -> permission.
	handler = auth.WritePermissionMiddleware(handler)
	handler = gateway.RateLimitMiddleware(tenantRL)(handler)
	handler = auth.AuthMiddleware(keyStore, authEnabled)(handler)
	if browser != nil {
		handler = browser.Middleware(handler)
	}
	handler = gateway.ClientIPRateLimitMiddleware(clientIPRL)(handler)
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

	logger.Info("gateway starting", "addr", listenAddr, "auth_enabled", authEnabled, "memory_mode", memoryMode, "browser_access", browser != nil)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		logger.Error("gateway failed", "error", err)
		os.Exit(1)
	}
}
