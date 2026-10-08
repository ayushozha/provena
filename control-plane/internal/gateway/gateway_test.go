package gateway

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/altrixy/provena/control-plane/internal/auth"
)

func TestProxyForwardsOnlyVerifiedIdentityClaims(t *testing.T) {
	token := "editor-token"
	sum := sha256.Sum256([]byte(token))
	keys, err := json.Marshal([]auth.APIKey{
		{
			KeyID:       "key-1",
			TenantID:    "tenant-1",
			Role:        auth.Editor,
			PrincipalID: "verified-principal",
			Groups:      []string{"verified-group", "platform"},
			HashedKey:   hex.EncodeToString(sum[:]),
		},
	})
	if err != nil {
		t.Fatalf("marshal keys: %v", err)
	}
	t.Setenv("PROVENA_API_KEYS", string(keys))
	keyStore, err := auth.NewKeyStore()
	if err != nil {
		t.Fatalf("new key store: %v", err)
	}

	upstreamHeaders := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamHeaders <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	proxy := NewProxyHandler(upstream.URL, time.Second)
	handler := auth.AuthMiddleware(keyStore, true)(proxy)
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/memory-1", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Proxy-Authorization", "Basic downstream-secret")
	req.Header.Set("Cookie", "session=downstream-secret")
	req.Header.Set("Connection", "X-Remove-Me")
	req.Header.Set("X-Remove-Me", "hop-by-hop-secret")
	req.Header.Set("X-Provena-Tenant-Id", "spoofed-tenant")
	req.Header.Set("X-Provena-Role", "superadmin")
	req.Header.Set("X-Provena-Key-Id", "spoofed-key")
	req.Header.Set("X-Provena-Principal-Id", "spoofed-principal")
	req.Header.Set("X-Provena-Groups", "spoofed-group")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("proxy response: got %d, want 204", rec.Code)
	}
	got := <-upstreamHeaders
	want := map[string]string{
		"X-Provena-Tenant-Id":    "tenant-1",
		"X-Provena-Role":         "editor",
		"X-Provena-Key-Id":       "key-1",
		"X-Provena-Principal-Id": "verified-principal",
		"X-Provena-Groups":       "verified-group,platform",
	}
	for header, value := range want {
		if got.Get(header) != value {
			t.Errorf("%s: got %q, want %q", header, got.Get(header), value)
		}
	}
	for _, header := range []string{
		"Authorization",
		"Proxy-Authorization",
		"Cookie",
		"Connection",
		"X-Remove-Me",
	} {
		if got.Get(header) != "" {
			t.Errorf("%s leaked to upstream: %q", header, got.Get(header))
		}
	}
}

func TestTenantRateLimitIgnoresSpoofedTenantHeader(t *testing.T) {
	token := "rate-limit-token"
	sum := sha256.Sum256([]byte(token))
	keys, err := json.Marshal([]auth.APIKey{{
		KeyID:     "rate-key",
		TenantID:  "verified-tenant",
		Role:      auth.Viewer,
		HashedKey: hex.EncodeToString(sum[:]),
	}})
	if err != nil {
		t.Fatalf("marshal keys: %v", err)
	}
	t.Setenv("PROVENA_API_KEYS", string(keys))
	keyStore, err := auth.NewKeyStore()
	if err != nil {
		t.Fatalf("new key store: %v", err)
	}

	inner := RateLimitMiddleware(NewRateLimiter(0, 1))(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	handler := auth.AuthMiddleware(keyStore, true)(inner)
	for index, spoofedTenant := range []string{"spoof-a", "spoof-b"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/memories/memory-1", nil)
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("X-Tenant-Id", spoofedTenant)
		req.Header.Set("X-Provena-Tenant-Id", spoofedTenant)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		want := http.StatusNoContent
		if index == 1 {
			want = http.StatusTooManyRequests
		}
		if rec.Code != want {
			t.Fatalf("request %d: got %d, want %d", index+1, rec.Code, want)
		}
	}
}

func TestClientIPRateLimitIgnoresSpoofedIdentityHeadersAndSourcePort(t *testing.T) {
	handler := ClientIPRateLimitMiddleware(NewRateLimiter(0, 1))(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	for index, remoteAddr := range []string{"203.0.113.10:41001", "203.0.113.10:41002"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/memories/memory-1", nil)
		req.RemoteAddr = remoteAddr
		req.Header.Set("X-Tenant-Id", "spoofed-tenant")
		req.Header.Set("X-Provena-Tenant-Id", "spoofed-tenant")
		req.Header.Set("X-Provena-Principal-Id", "spoofed-principal")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)

		want := http.StatusNoContent
		if index == 1 {
			want = http.StatusTooManyRequests
		}
		if rec.Code != want {
			t.Fatalf("request %d: got %d, want %d", index+1, rec.Code, want)
		}
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/memories/memory-1", nil)
	req.RemoteAddr = "203.0.113.11:41001"
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("independent client IP: got %d, want 204", rec.Code)
	}
}

func TestProxyDropsSpoofedGroupsWhenKeyHasNoGroupClaims(t *testing.T) {
	token := "editor-token"
	sum := sha256.Sum256([]byte(token))
	keys, err := json.Marshal([]auth.APIKey{
		{
			KeyID:     "key-1",
			TenantID:  "tenant-1",
			Role:      auth.Editor,
			HashedKey: hex.EncodeToString(sum[:]),
		},
	})
	if err != nil {
		t.Fatalf("marshal keys: %v", err)
	}
	t.Setenv("PROVENA_API_KEYS", string(keys))
	keyStore, err := auth.NewKeyStore()
	if err != nil {
		t.Fatalf("new key store: %v", err)
	}

	upstreamHeaders := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamHeaders <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	proxy := NewProxyHandler(upstream.URL, time.Second)
	handler := auth.AuthMiddleware(keyStore, true)(proxy)
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/memory-1", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("X-Provena-Principal-Id", "spoofed-principal")
	req.Header.Set("X-Provena-Groups", "spoofed-group")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("proxy response: got %d, want 204", rec.Code)
	}
	got := <-upstreamHeaders
	if got.Get("X-Provena-Principal-Id") != "key-1" {
		t.Fatalf("principal: got %q, want key-1", got.Get("X-Provena-Principal-Id"))
	}
	if got.Get("X-Provena-Groups") != "" {
		t.Fatalf("groups: got %q, want empty", got.Get("X-Provena-Groups"))
	}
}

func loadTestCredential(t *testing.T, required bool) *UpstreamCredential {
	t.Helper()
	credential, err := LoadUpstreamCredential(func(key string) string {
		if key == "PROVENA_GATEWAY_SERVICE_TOKEN" {
			return "internal-service-token"
		}
		return ""
	}, required)
	if err != nil {
		t.Fatalf("LoadUpstreamCredential: %v", err)
	}
	return credential
}

func keyStoreForToken(t *testing.T, token, tenantID string) *auth.KeyStore {
	t.Helper()
	sum := sha256.Sum256([]byte(token))
	keys, err := json.Marshal([]auth.APIKey{{
		KeyID:       "external-key",
		TenantID:    tenantID,
		Role:        auth.Editor,
		PrincipalID: "configured-principal",
		Groups:      []string{"team-a", "team-b"},
		HashedKey:   hex.EncodeToString(sum[:]),
	}})
	if err != nil {
		t.Fatalf("marshal API keys: %v", err)
	}
	t.Setenv("PROVENA_API_KEYS", string(keys))
	keyStore, err := auth.NewKeyStore()
	if err != nil {
		t.Fatalf("NewKeyStore: %v", err)
	}
	return keyStore
}

func authenticatedProxy(t *testing.T, next http.Handler, token, tenantID string) http.Handler {
	t.Helper()
	return auth.AuthMiddleware(keyStoreForToken(t, token, tenantID), true)(next)
}

func TestLoadUpstreamCredentialFailsClosed(t *testing.T) {
	empty := func(string) string { return "" }
	if credential, err := LoadUpstreamCredential(empty, false); err != nil || credential != nil {
		t.Fatalf("optional credential = (%v, %v), want (nil, nil)", credential, err)
	}
	if _, err := LoadUpstreamCredential(empty, true); err == nil {
		t.Fatal("required empty credential did not fail")
	}
	if credential := loadTestCredential(t, false); credential != nil {
		t.Fatal("explicit local no-auth mode created a trusted upstream credential")
	}
}

func TestUpstreamCredentialRejectsExternalCredentialReuse(t *testing.T) {
	credential := loadTestCredential(t, true)
	if err := credential.ValidateCredentialSeparation(keyStoreForToken(t, "different-external-token", "tenant-a")); err != nil {
		t.Fatalf("distinct credentials rejected: %v", err)
	}
	if err := credential.ValidateCredentialSeparation(keyStoreForToken(t, "internal-service-token", "tenant-a")); err == nil {
		t.Fatal("reused external/internal credential was accepted")
	}
}

func TestProxyReplacesCallerBearerAndForwardsValidatedContext(t *testing.T) {
	callerToken := "external-caller-token"
	received := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	proxy := NewProxyHandler(upstream.URL, time.Second).
		WithUpstreamCredential(loadTestCredential(t, true)).
		WithRewritePath("/internal/write")
	handler := authenticatedProxy(t, proxy, callerToken, "tenant-a")
	req := httptest.NewRequest(http.MethodPost, "/v1/memories", strings.NewReader(`{"content":"safe"}`))
	req.Header.Set("Authorization", "Bearer "+callerToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Tenant-Id", "attacker-tenant")
	req.Header.Set("X-Provena-Tenant-Id", "attacker-tenant")
	req.Header.Set("X-Provena-Role", "admin")
	req.Header.Set("X-Provena-Key-Id", "attacker-key")
	req.Header.Set("X-Provena-Principal-Id", "attacker-principal")
	req.Header.Set("X-Provena-Groups", "attacker-group")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("proxy status = %d, want 204; body=%s", rec.Code, rec.Body.String())
	}
	header := <-received
	if got := header.Get("Authorization"); got != "Bearer internal-service-token" {
		t.Fatalf("Authorization = %q, want internal service bearer", got)
	}
	if got := header.Get("X-Provena-Tenant-Id"); got != "tenant-a" {
		t.Fatalf("tenant = %q, want validated tenant-a", got)
	}
	if got := header.Get("X-Provena-Principal-Id"); got != "configured-principal" {
		t.Fatalf("principal = %q, want configured principal", got)
	}
	if got := header.Get("X-Provena-Role"); got != "editor" {
		t.Fatalf("role = %q, want validated editor", got)
	}
	if got := header.Get("X-Provena-Key-Id"); got != "external-key" {
		t.Fatalf("key id = %q, want validated external-key", got)
	}
	if got := header.Get("X-Provena-Groups"); got != "team-a,team-b" {
		t.Fatalf("groups = %q, want configured groups", got)
	}
	for name, values := range header {
		for _, value := range values {
			if strings.Contains(value, callerToken) {
				t.Fatalf("caller bearer leaked in %s", name)
			}
		}
	}
}

func TestProxyPreservesValidatedMultiTenantContext(t *testing.T) {
	received := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	proxy := NewProxyHandler(upstream.URL, time.Second).WithUpstreamCredential(loadTestCredential(t, true))
	handler := authenticatedProxy(t, proxy, "tenant-b-token", "tenant-b")
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/id", nil)
	req.Header.Set("Authorization", "Bearer tenant-b-token")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", rec.Code)
	}
	header := <-received
	if header.Get("Authorization") != "Bearer internal-service-token" || header.Get("X-Provena-Tenant-Id") != "tenant-b" {
		t.Fatalf("unexpected upstream identity: auth=%q tenant=%q", header.Get("Authorization"), header.Get("X-Provena-Tenant-Id"))
	}
}

func TestProxyStillRequiresValidExternalAPIKey(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	proxy := NewProxyHandler(upstream.URL, time.Second).WithUpstreamCredential(loadTestCredential(t, true))
	handler := authenticatedProxy(t, proxy, "valid-external-token", "tenant-a")
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/id", nil)
	req.Header.Set("Authorization", "Bearer invalid-external-token")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	if calls.Load() != 0 {
		t.Fatalf("upstream called %d times after invalid external auth", calls.Load())
	}
}

func TestAuthenticatedUpstreamRequiresAuthContext(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	req := httptest.NewRequest(http.MethodGet, "/v1/memories/id", nil)
	rec := httptest.NewRecorder()
	NewProxyHandler(upstream.URL, time.Second).
		WithUpstreamCredential(loadTestCredential(t, true)).
		ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	if calls.Load() != 0 {
		t.Fatalf("upstream called %d times without authenticated context", calls.Load())
	}
}

func TestReadinessHandlerReflectsUpstreamReadiness(t *testing.T) {
	for _, test := range []struct {
		name           string
		upstreamStatus int
		wantStatus     int
		wantBodyStatus string
	}{
		{
			name:           "all healthy",
			upstreamStatus: http.StatusOK,
			wantStatus:     http.StatusOK,
			wantBodyStatus: "ready",
		},
		{
			name:           "upstream blocked",
			upstreamStatus: http.StatusServiceUnavailable,
			wantStatus:     http.StatusServiceUnavailable,
			wantBodyStatus: "blocked",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(test.upstreamStatus)
			}))
			defer upstream.Close()

			recorder := httptest.NewRecorder()
			ReadinessHandler(NewHealthAggregator(map[string]string{
				"store": upstream.URL,
			})).ServeHTTP(
				recorder,
				httptest.NewRequest(http.MethodGet, "/readyz", nil),
			)

			if recorder.Code != test.wantStatus {
				t.Fatalf("status = %d, want %d", recorder.Code, test.wantStatus)
			}
			var body struct {
				Status   string          `json:"status"`
				Services []ServiceHealth `json:"services"`
			}
			if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
				t.Fatalf("decode response: %v", err)
			}
			if body.Status != test.wantBodyStatus {
				t.Fatalf("body status = %q, want %q", body.Status, test.wantBodyStatus)
			}
			if len(body.Services) != 1 {
				t.Fatalf("services = %d, want 1", len(body.Services))
			}
		})
	}
}

func TestProxyWithoutInternalCredentialStripsCallerAuthorization(t *testing.T) {
	received := make(chan string, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received <- r.Header.Get("Authorization")
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	req := httptest.NewRequest(http.MethodPost, "/preflight", nil)
	req.Header.Set("Authorization", "Bearer must-not-cross-boundary")
	rec := httptest.NewRecorder()
	NewProxyHandler(upstream.URL, time.Second).ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", rec.Code)
	}
	if got := <-received; got != "" {
		t.Fatalf("caller Authorization reached unauthenticated upstream: %q", got)
	}
}
