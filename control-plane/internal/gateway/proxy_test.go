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

func TestProxyForwardsKeyScopeAndDropsCallerScopeAndOrigin(t *testing.T) {
	upstreamHeaders := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamHeaders <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	handler := auth.AuthMiddleware(scopedKeyStore(t, "scoped-token"), true)(NewProxyHandler(upstream.URL, time.Second))
	req := httptest.NewRequest(http.MethodPost, "/v1/memories", nil)
	req.Header.Set("Authorization", "Bearer scoped-token")
	req.Header.Set("Origin", "http://localhost:3000")
	req.Header.Set("X-Provena-Scope-Workspace-Id", "spoofed-workspace")
	req.Header.Set("X-Provena-Scope-User-Id", "spoofed-user")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("proxy response: got %d, want 204", rec.Code)
	}
	got := <-upstreamHeaders
	for header, want := range map[string]string{
		"X-Provena-Scope-Workspace-Id": "ws-1",
		"X-Provena-Scope-User-Id":      "user-1",
		"X-Provena-Principal-Id":       "person-1",
		"Origin":                       "",
	} {
		if got.Get(header) != want {
			t.Errorf("%s: got %q, want %q", header, got.Get(header), want)
		}
	}
}

func TestProxyReturnsUpstreamRedirectsInsteadOfFollowingThem(t *testing.T) {
	elsewhereHits := 0
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		elsewhereHits++
		w.WriteHeader(http.StatusOK)
	}))
	defer elsewhere.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+"/collect", http.StatusFound)
	}))
	defer upstream.Close()

	handler := auth.AuthMiddleware(scopedKeyStore(t, "scoped-token"), true)(NewProxyHandler(upstream.URL, time.Second))
	req := httptest.NewRequest(http.MethodPost, "/v1/memories", nil)
	req.Header.Set("Authorization", "Bearer scoped-token")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusFound {
		t.Fatalf("redirecting upstream: got %d, want the 302 passed through", rec.Code)
	}
	if elsewhereHits != 0 {
		t.Fatalf("the proxy followed the redirect %d time(s) with rebuilt identity headers", elsewhereHits)
	}
}
