// Package gateway provides HTTP middleware and proxy utilities for the API gateway.
package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/altrixy/provena/control-plane/internal/auth"
)

// ---------- Request Logger ----------

// RequestLogger is middleware that emits structured JSON log lines via slog.
func RequestLogger(next http.Handler) http.Handler {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &responseWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		logger.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rw.status,
			"duration_ms", time.Since(start).Milliseconds(),
			"remote_addr", r.RemoteAddr,
			"trace_id", r.Header.Get("X-Trace-Id"),
		)
	})
}

type responseWriter struct {
	http.ResponseWriter
	status int
}

func (rw *responseWriter) WriteHeader(code int) {
	rw.status = code
	rw.ResponseWriter.WriteHeader(code)
}

// ---------- Rate Limiter (per-tenant token bucket) ----------

// RateLimiter provides per-tenant token bucket rate limiting.
type RateLimiter struct {
	mu      sync.Mutex
	buckets map[string]*bucket
	rate    float64 // tokens per second
	burst   int     // max tokens
}

type bucket struct {
	tokens    float64
	lastCheck time.Time
}

// NewRateLimiter creates a rate limiter with the given rate (tokens/sec) and burst size.
func NewRateLimiter(rate float64, burst int) *RateLimiter {
	return &RateLimiter{
		buckets: make(map[string]*bucket),
		rate:    rate,
		burst:   burst,
	}
}

// Allow checks whether the given tenant is allowed to proceed.
func (rl *RateLimiter) Allow(tenantID string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()

	b, ok := rl.buckets[tenantID]
	if !ok {
		b = &bucket{tokens: float64(rl.burst), lastCheck: time.Now()}
		rl.buckets[tenantID] = b
	}

	now := time.Now()
	elapsed := now.Sub(b.lastCheck).Seconds()
	b.tokens += elapsed * rl.rate
	if b.tokens > float64(rl.burst) {
		b.tokens = float64(rl.burst)
	}
	b.lastCheck = now

	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// RateLimitMiddleware returns middleware that rate-limits by tenant.
// It extracts tenant from the X-Tenant-Id header.
func RateLimitMiddleware(rl *RateLimiter) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tenant := r.Header.Get("X-Tenant-Id")
			if ac, ok := auth.FromContext(r.Context()); ok && ac.TenantID != "" {
				tenant = ac.TenantID
			}
			if tenant == "" {
				tenant = "default"
			}
			if !rl.Allow(tenant) {
				http.Error(w, `{"error":"rate limit exceeded"}`, http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// ---------- Proxy Handler ----------

// ProxyHandler forwards requests to an upstream URL, propagating context and timeout.
type ProxyHandler struct {
	UpstreamURL        string
	RewritePath        string
	Client             *http.Client
	UpstreamCredential *UpstreamCredential
}

// UpstreamCredential is the gateway's private transport credential for
// authenticated store, intelligence, and lifecycle calls. Caller identity is
// carried separately in the validated AuthContext.
type UpstreamCredential struct {
	token string
}

// LoadUpstreamCredential loads the private gateway-to-service bearer. Explicit
// local no-auth mode does not create a trusted assertion channel; production
// gateway auth fails closed when the credential is absent.
func LoadUpstreamCredential(getenv func(string) string, required bool) (*UpstreamCredential, error) {
	if !required {
		return nil, nil
	}
	token := strings.TrimSpace(getenv("PROVENA_GATEWAY_SERVICE_TOKEN"))
	if token == "" {
		return nil, fmt.Errorf("PROVENA_GATEWAY_SERVICE_TOKEN is required when gateway auth is enabled")
	}
	return &UpstreamCredential{token: token}, nil
}

// ValidateCredentialSeparation rejects an internal bearer that is also a valid
// external gateway API key. That prevents configuration from collapsing the
// caller and service trust boundaries back into one credential.
func (credential *UpstreamCredential) ValidateCredentialSeparation(keyStore *auth.KeyStore) error {
	if credential == nil {
		return nil
	}
	if _, reused := keyStore.Validate(credential.token); reused {
		return fmt.Errorf("PROVENA_GATEWAY_SERVICE_TOKEN must not match an external gateway API key")
	}
	return nil
}

// NewProxyHandler creates a proxy handler with the given upstream URL and timeout.
func NewProxyHandler(upstreamURL string, timeout time.Duration) *ProxyHandler {
	return &ProxyHandler{
		UpstreamURL: upstreamURL,
		Client:      &http.Client{Timeout: timeout},
	}
}

// WithUpstreamCredential returns a shallow copy that authenticates to its
// upstream with the supplied private transport credential.
func (ph *ProxyHandler) WithUpstreamCredential(credential *UpstreamCredential) *ProxyHandler {
	return &ProxyHandler{
		UpstreamURL:        ph.UpstreamURL,
		RewritePath:        ph.RewritePath,
		Client:             ph.Client,
		UpstreamCredential: credential,
	}
}

// WithRewritePath returns a shallow copy of the handler that rewrites the
// forwarded request path to a fixed upstream path.
func (ph *ProxyHandler) WithRewritePath(path string) *ProxyHandler {
	return &ProxyHandler{
		UpstreamURL:        ph.UpstreamURL,
		RewritePath:        path,
		Client:             ph.Client,
		UpstreamCredential: ph.UpstreamCredential,
	}
}

func isCallerCredentialHeader(name string) bool {
	switch http.CanonicalHeaderKey(name) {
	case "Authorization", "Proxy-Authorization", "X-Tenant-Id", "X-Provena-Tenant-Id", "X-Provena-Role", "X-Provena-Key-Id", "X-Provena-Principal-Id", "X-Provena-Groups":
		return true
	default:
		return false
	}
}

func applyAuthContext(header http.Header, ac *auth.AuthContext) {
	header.Set("X-Provena-Tenant-Id", ac.TenantID)
	header.Set("X-Provena-Role", string(ac.Role))
	header.Set("X-Provena-Key-Id", ac.KeyID)
	if ac.PrincipalID != "" {
		header.Set("X-Provena-Principal-Id", ac.PrincipalID)
	}
	if len(ac.Groups) > 0 {
		header.Set("X-Provena-Groups", strings.Join(ac.Groups, ","))
	}
}

func (credential *UpstreamCredential) apply(header http.Header, ac *auth.AuthContext) {
	header.Set("Authorization", "Bearer "+credential.token)
	applyAuthContext(header, ac)
}

// ServeHTTP forwards the request to the upstream service.
func (ph *ProxyHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	upstreamPath := r.URL.Path
	if ph.RewritePath != "" {
		upstreamPath = ph.RewritePath
	}
	upURL := ph.UpstreamURL + upstreamPath
	if r.URL.RawQuery != "" {
		upURL += "?" + r.URL.RawQuery
	}

	req, err := http.NewRequestWithContext(ctx, r.Method, upURL, r.Body)
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusBadGateway)
		return
	}
	for k, vv := range r.Header {
		if isCallerCredentialHeader(k) {
			continue
		}
		for _, v := range vv {
			req.Header.Add(k, v)
		}
	}
	ac, authenticated := auth.FromContext(ctx)
	if ph.UpstreamCredential != nil {
		if !authenticated {
			http.Error(w, `{"error":"authenticated gateway context required"}`, http.StatusInternalServerError)
			return
		}
		ph.UpstreamCredential.apply(req.Header, ac)
	} else if authenticated {
		applyAuthContext(req.Header, ac)
	}

	resp, err := ph.Client.Do(req)
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"upstream error: %s"}`, err.Error()), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for k, vv := range resp.Header {
		for _, v := range vv {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// ---------- Health Aggregator ----------

// ServiceHealth represents the health status of a single service.
type ServiceHealth struct {
	Name    string `json:"name"`
	Status  string `json:"status"`
	Latency int64  `json:"latency_ms"`
}

// HealthAggregator checks multiple upstream services concurrently.
type HealthAggregator struct {
	services map[string]string // name -> healthz URL
	client   *http.Client
}

// NewHealthAggregator creates a health aggregator for the given service URLs.
func NewHealthAggregator(services map[string]string) *HealthAggregator {
	return &HealthAggregator{
		services: services,
		client:   &http.Client{Timeout: 5 * time.Second},
	}
}

// CheckAll runs health checks concurrently and returns results.
func (ha *HealthAggregator) CheckAll(ctx context.Context) []ServiceHealth {
	results := make([]ServiceHealth, 0, len(ha.services))
	var mu sync.Mutex
	var wg sync.WaitGroup

	for name, url := range ha.services {
		wg.Add(1)
		go func(n, u string) {
			defer wg.Done()
			start := time.Now()
			status := "healthy"
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
			if err != nil {
				status = "unhealthy"
			} else {
				resp, err := ha.client.Do(req)
				if err != nil || resp.StatusCode != http.StatusOK {
					status = "unhealthy"
				}
				if resp != nil {
					resp.Body.Close()
				}
			}
			mu.Lock()
			results = append(results, ServiceHealth{
				Name:    n,
				Status:  status,
				Latency: time.Since(start).Milliseconds(),
			})
			mu.Unlock()
		}(name, url)
	}
	wg.Wait()
	return results
}

// HealthHandler returns an HTTP handler that aggregates health checks.
func HealthHandler(ha *HealthAggregator) http.HandlerFunc {
	return aggregateHealthHandler(ha, "healthy", "degraded")
}

// ReadinessHandler returns 503 until every configured upstream is healthy.
func ReadinessHandler(ha *HealthAggregator) http.HandlerFunc {
	return aggregateHealthHandler(ha, "ready", "blocked")
}

func aggregateHealthHandler(ha *HealthAggregator, readyStatus, blockedStatus string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		results := ha.CheckAll(r.Context())
		allHealthy := true
		for _, s := range results {
			if s.Status != "healthy" {
				allHealthy = false
				break
			}
		}
		resp := struct {
			Status   string          `json:"status"`
			Services []ServiceHealth `json:"services"`
		}{
			Status:   readyStatus,
			Services: results,
		}
		if !allHealthy {
			resp.Status = blockedStatus
		}
		w.Header().Set("Content-Type", "application/json")
		if !allHealthy {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		json.NewEncoder(w).Encode(resp)
	}
}
