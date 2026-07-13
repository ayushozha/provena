package gateway

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
