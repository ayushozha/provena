// Package gateway provides HTTP middleware and proxy utilities for the API gateway.
package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
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

// RateLimitMiddleware rate-limits by the tenant verified by AuthMiddleware.
func RateLimitMiddleware(rl *RateLimiter) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tenant := "unscoped"
			if ac, ok := auth.FromContext(r.Context()); ok && ac.TenantID != "" {
				tenant = ac.TenantID
			}
			if !rl.Allow(tenant) {
				http.Error(w, `{"error":"rate limit exceeded"}`, http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// ClientIPRateLimitMiddleware limits unauthenticated floods before API-key
// verification. It deliberately ignores caller-controlled tenant headers.
func ClientIPRateLimitMiddleware(rl *RateLimiter) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			clientIP := r.RemoteAddr
			if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
				clientIP = host
			}
			if !rl.Allow(clientIP) {
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
	UpstreamURL string
	RewritePath string
	Client      *http.Client
}

// NewProxyHandler creates a proxy handler with the given upstream URL and timeout.
func NewProxyHandler(upstreamURL string, timeout time.Duration) *ProxyHandler {
	return &ProxyHandler{
		UpstreamURL: upstreamURL,
		Client:      &http.Client{Timeout: timeout},
	}
}

// WithRewritePath returns a shallow copy of the handler that rewrites the
// forwarded request path to a fixed upstream path.
func (ph *ProxyHandler) WithRewritePath(path string) *ProxyHandler {
	return &ProxyHandler{
		UpstreamURL: ph.UpstreamURL,
		RewritePath: path,
		Client:      ph.Client,
	}
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
		for _, v := range vv {
			req.Header.Add(k, v)
		}
	}
	for _, connectionHeader := range strings.Split(req.Header.Get("Connection"), ",") {
		if header := strings.TrimSpace(connectionHeader); header != "" {
			req.Header.Del(header)
		}
	}
	for _, header := range []string{
		"Authorization",
		"Proxy-Authorization",
		"Cookie",
		"Connection",
		"Keep-Alive",
		"Proxy-Authenticate",
		"Te",
		"Trailer",
		"Transfer-Encoding",
		"Upgrade",
		"X-Provena-Tenant-Id",
		"X-Provena-Role",
		"X-Provena-Key-Id",
		"X-Provena-Principal-Id",
		"X-Provena-Groups",
	} {
		req.Header.Del(header)
	}
	if ac, ok := auth.FromContext(ctx); ok {
		req.Header.Set("X-Provena-Tenant-Id", ac.TenantID)
		req.Header.Set("X-Provena-Role", string(ac.Role))
		req.Header.Set("X-Provena-Key-Id", ac.KeyID)
		if ac.PrincipalID != "" {
			req.Header.Set("X-Provena-Principal-Id", ac.PrincipalID)
		}
		if len(ac.Groups) > 0 {
			req.Header.Set("X-Provena-Groups", strings.Join(ac.Groups, ","))
		}
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
			Status:   "healthy",
			Services: results,
		}
		if !allHealthy {
			resp.Status = "degraded"
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	}
}
