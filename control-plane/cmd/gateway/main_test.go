package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/altrixy/provena/control-plane/internal/auth"
	"github.com/altrixy/provena/control-plane/internal/gateway"
)

func upstreamRecorder(name string, hits chan string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits <- name + " " + r.Method + " " + r.URL.RequestURI()
		w.WriteHeader(http.StatusNoContent)
	}))
}

// scopedKeyStore loads one editor key bound to ws-1/user-1 for the given raw token.
func scopedKeyStore(t *testing.T, token string) *auth.KeyStore {
	t.Helper()
	sum := sha256.Sum256([]byte(token))
	keys, err := json.Marshal([]auth.APIKey{{
		KeyID:       "local-key",
		TenantID:    "tenant-local",
		Role:        auth.Editor,
		PrincipalID: "person-1",
		Scope:       auth.KeyScope{WorkspaceID: "ws-1", UserID: "user-1"},
		HashedKey:   hex.EncodeToString(sum[:]),
	}})
	if err != nil {
		t.Fatalf("marshal keys: %v", err)
	}
	t.Setenv("PROVENA_API_KEYS", string(keys))
	ks, err := auth.NewKeyStore()
	if err != nil {
		t.Fatalf("new key store: %v", err)
	}
	return ks
}

func serviceCredential(t *testing.T) *gateway.UpstreamCredential {
	t.Helper()
	credential, err := gateway.LoadUpstreamCredential(func(key string) string {
		if key == "PROVENA_GATEWAY_SERVICE_TOKEN" {
			return "internal-service-token"
		}
		return ""
	}, true)
	if err != nil {
		t.Fatalf("LoadUpstreamCredential: %v", err)
	}
	return credential
}

// redirected stands for any 3xx: the mux's answer to a non-canonical path, whose exact code varies by method.
const redirected = 300

func statusMatches(got, want int) bool {
	if want == redirected {
		return got/100 == 3
	}
	return got == want
}

func assertNoUpstreamHit(t *testing.T, hits chan string) {
	t.Helper()
	select {
	case got := <-hits:
		t.Fatalf("a refused request reached an upstream: %q", got)
	default:
	}
}

func TestLocalRouterExposesOnlyMemoryRoutesAndSoftDeletes(t *testing.T) {
	hits := make(chan string, 16)
	store := upstreamRecorder("store", hits)
	defer store.Close()
	mux := localRouter(store.URL, nil)

	reaches := map[string]string{
		"POST /v1/memories":              "store POST /v1/memories",
		"POST /v1/memories/search":       "store POST /v1/memories/search",
		"GET /v1/memories/mem-1":         "store GET /v1/memories/mem-1",
		"PUT /v1/memories/mem-1":         "store PUT /v1/memories/mem-1",
		"GET /v1/memories/mem-1/history": "store GET /v1/memories/mem-1/history",
		"DELETE /v1/memories/mem-1":      "store DELETE /v1/memories/mem-1",
		"GET /v1/memories/mem-1?limit=5": "store GET /v1/memories/mem-1?limit=5",
	}
	for request, want := range reaches {
		method, target, _ := strings.Cut(request, " ")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(method, target, strings.NewReader("{}")))
		if rec.Code != http.StatusNoContent {
			t.Fatalf("%s: got %d, want 204", request, rec.Code)
		}
		if got := <-hits; got != want {
			t.Fatalf("%s: reached %q, want %q", request, got, want)
		}
	}

	// Hard deletes are refused outright; everything outside the memory surface is simply not served (404, or 405
	// where only another method exists on that path). None of it may reach the store.
	for _, request := range []string{
		"DELETE /v1/memories/mem-1?hard_delete=true",
		"DELETE /v1/memories/mem-1?hard_delete=false",
	} {
		method, target, _ := strings.Cut(request, " ")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(method, target, nil))
		if rec.Code != http.StatusForbidden {
			t.Errorf("%s: got %d, want 403", request, rec.Code)
		}
	}
	for _, request := range []string{
		"POST /v1/admin/erase",
		"GET /v1/admin/retention-policies",
		"POST /v1/admin/rtbf",
		"POST /v1/memories/relations",
		"POST /v1/memories/mem-1/feedback",
		"GET /v1/memories/mem-1/feedback",
		"POST /v1/memories/graph/temporal",
		"POST /v1/agent/context",
		"GET /v1/admin/memories/mem-1/inspect",
		"POST /v1/admin/memories/search/explain",
		"POST /v1/integrations/connectors",
		"POST /v1/project-snapshots",
		"POST /v1/procedures/abstract",
		"POST /v1/pipeline/write",
		"POST /v1/pipeline/search",
		"GET /v1/cold-start",
		"GET /readyz",
		"GET /ui/observability",
		"GET /metrics",
		"POST /v1/repositories/r/memory-events/sync",
		"POST /v1/preflight",
	} {
		method, target, _ := strings.Cut(request, " ")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(method, target, strings.NewReader("{}")))
		if rec.Code != http.StatusNotFound && rec.Code != http.StatusMethodNotAllowed {
			t.Errorf("%s: got %d, want 404 or 405", request, rec.Code)
		}
	}
	assertNoUpstreamHit(t, hits)
}

func TestLocalRouterRefusesNonCanonicalPathsBeforeTheStore(t *testing.T) {
	hits := make(chan string, 16)
	store := upstreamRecorder("store", hits)
	defer store.Close()
	mux := localRouter(store.URL, nil)

	// Encoded slashes and dots match a single {id} segment at the mux but would reach the store decoded as a
	// different path; literal traversal is answered by the mux with a redirect to the cleaned path (301 or 307
	// depending on the method and Go version) and must never run a handler.
	for request, want := range map[string]int{
		"GET /v1/memories/..%2Fadmin%2Ferase":    http.StatusNotFound,
		"GET /v1/memories/%2e%2e":                http.StatusNotFound,
		"GET /v1/memories/%2e%2e/admin/erase":    http.StatusNotFound,
		"GET /v1/memories/mem-1%2Fhistory":       http.StatusNotFound,
		"PUT /v1/memories/mem-1%2F..%2Fother":    http.StatusNotFound,
		"DELETE /v1/memories/mem-1%2Fhistory":    http.StatusNotFound,
		"DELETE /v1/memories/..":                 redirected,
		"POST /v1/memories/../admin/erase":       redirected,
		"GET /v1/memories/mem-1/history/..%2F..": http.StatusNotFound,
	} {
		method, target, _ := strings.Cut(request, " ")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(method, target, strings.NewReader("{}")))
		if !statusMatches(rec.Code, want) {
			t.Errorf("%s: got %d, want %d", request, rec.Code, want)
		}
	}
	assertNoUpstreamHit(t, hits)

	// A plain id and a plain id with a query are untouched.
	for _, request := range []string{"GET /v1/memories/0b8f3a2e-1c4d-4f0e-9a7b-2d3e4f5a6b7c", "GET /v1/memories/mem-1/history?limit=50"} {
		method, target, _ := strings.Cut(request, " ")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(method, target, nil))
		if rec.Code != http.StatusNoContent {
			t.Fatalf("%s: got %d, want 204", request, rec.Code)
		}
		<-hits
	}
}

func TestLocalRouterAuthenticatesToTheStoreWithTheGatewayCredentialAndBoundScope(t *testing.T) {
	upstreamHeaders := make(chan http.Header, 1)
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamHeaders <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer store.Close()

	handler := auth.AuthMiddleware(scopedKeyStore(t, "scoped-token"), true)(localRouter(store.URL, serviceCredential(t)))
	req := httptest.NewRequest(http.MethodPost, "/v1/memories", strings.NewReader("{}"))
	req.Header.Set("Authorization", "Bearer scoped-token")
	req.Header.Set("Origin", "http://localhost:3000")
	req.Header.Set("X-Provena-Tenant-Id", "spoofed-tenant")
	req.Header.Set("X-Provena-Role", "superadmin")
	req.Header.Set("X-Provena-Scope-Workspace-Id", "spoofed-workspace")
	req.Header.Set("X-Provena-Scope-User-Id", "spoofed-user")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("proxy response: got %d, want 204", rec.Code)
	}
	got := <-upstreamHeaders
	for header, want := range map[string]string{
		"Authorization":                "Bearer internal-service-token",
		"X-Provena-Tenant-Id":          "tenant-local",
		"X-Provena-Role":               "editor",
		"X-Provena-Key-Id":             "local-key",
		"X-Provena-Principal-Id":       "person-1",
		"X-Provena-Scope-Workspace-Id": "ws-1",
		"X-Provena-Scope-User-Id":      "user-1",
		"Origin":                       "",
	} {
		if got.Get(header) != want {
			t.Errorf("%s: got %q, want %q", header, got.Get(header), want)
		}
	}

	// Without a verified key nothing is forwarded at all.
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/memories", strings.NewReader("{}")))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("missing key: got %d, want 401", rec.Code)
	}
	select {
	case <-upstreamHeaders:
		t.Fatal("an unauthenticated request reached the store")
	default:
	}
}

func TestPipelineRouterStillUsesTheIntelligencePipeline(t *testing.T) {
	hits := make(chan string, 8)
	store, intelligence := upstreamRecorder("store", hits), upstreamRecorder("intelligence", hits)
	defer store.Close()
	defer intelligence.Close()
	mux := newRouter(routerConfig{
		storeURL:         store.URL,
		intelligenceURL:  intelligence.URL,
		orchestrationURL: "http://127.0.0.1:1",
		lifecycleURL:     "http://127.0.0.1:1",
		health:           gateway.NewHealthAggregator(nil),
	})
	for request, want := range map[string]string{
		"POST /v1/memories":                          "intelligence POST /v1/pipeline/write",
		"POST /v1/memories/search":                   "intelligence POST /v1/pipeline/search",
		"POST /v1/procedures/abstract":               "intelligence POST /v1/procedures/abstract",
		"DELETE /v1/memories/mem-1?hard_delete=true": "store DELETE /v1/memories/mem-1?hard_delete=true",
	} {
		method, target, _ := strings.Cut(request, " ")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(method, target, strings.NewReader("{}")))
		if rec.Code != http.StatusNoContent {
			t.Fatalf("%s: got %d, want 204", request, rec.Code)
		}
		if got := <-hits; got != want {
			t.Fatalf("%s: reached %q, want %q", request, got, want)
		}
	}
}

func TestPipelineRouterTraversalNeverReachesAdminRoutes(t *testing.T) {
	hits := make(chan string, 8)
	store := upstreamRecorder("store", hits)
	defer store.Close()
	mux := newRouter(routerConfig{
		storeURL:         store.URL,
		intelligenceURL:  "http://127.0.0.1:1",
		orchestrationURL: "http://127.0.0.1:1",
		lifecycleURL:     "http://127.0.0.1:1",
		credential:       serviceCredential(t),
		health:           gateway.NewHealthAggregator(nil),
	})
	// The shipped chain order: auth populates the context, then the write-permission check reads the raw path.
	var handler http.Handler = auth.WritePermissionMiddleware(mux)
	handler = auth.AuthMiddleware(scopedKeyStore(t, "editor-token"), true)(handler)

	for request, want := range map[string]int{
		"POST /v1/admin/erase":                 http.StatusForbidden,
		"POST /v1/memories/../admin/erase":     redirected,
		"POST /v1/memories/%2e%2e/admin/erase": http.StatusNotFound,
		"POST /v1/memories/..%2Fadmin%2Ferase": http.StatusMethodNotAllowed,
	} {
		method, target, _ := strings.Cut(request, " ")
		req := httptest.NewRequest(method, target, strings.NewReader("{}"))
		req.Header.Set("Authorization", "Bearer editor-token")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if !statusMatches(rec.Code, want) {
			t.Errorf("%s: got %d, want %d", request, rec.Code, want)
		}
	}
	assertNoUpstreamHit(t, hits)
}

func TestIsLoopback(t *testing.T) {
	for addr, want := range map[string]bool{
		"127.0.0.1:8787": true,
		"localhost:8787": true,
		"[::1]:8787":     true,
		"127.0.0.2:8787": true,
		":8080":          false,
		"0.0.0.0:8080":   false,
		"192.168.1.5:80": false,
		"[::]:8080":      false,
		"8080":           false,
		"":               false,
	} {
		if got := isLoopback(addr); got != want {
			t.Errorf("isLoopback(%q) = %v, want %v", addr, got, want)
		}
	}
}

func TestIsLoopbackURL(t *testing.T) {
	for raw, want := range map[string]bool{
		"http://127.0.0.1:8700":            true,
		"http://localhost:8700":            true,
		"http://[::1]:8700":                true,
		"https://127.0.0.1:8700":           false,
		"http://127.0.0.1":                 false,
		"http://127.0.0.1:8700/":           false,
		"http://127.0.0.1:8700/v1":         false,
		"http://127.0.0.1:8700?x=1":        false,
		"http://127.0.0.1:8700#f":          false,
		"http://user:pw@127.0.0.1:8700":    false,
		"http://10.0.0.5:8700":             false,
		"http://store:8000":                false,
		"http://evil.example@127.0.0.1:80": false,
		"":                                 false,
	} {
		if got := isLoopbackURL(raw); got != want {
			t.Errorf("isLoopbackURL(%q) = %v, want %v", raw, got, want)
		}
	}
}
