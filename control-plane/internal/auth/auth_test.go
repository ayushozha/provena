package auth

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// keyStoreWith builds a KeyStore from the given keys via the env loader.

func keyStoreWith(t *testing.T, keys []APIKey) *KeyStore {
	t.Helper()
	raw, err := json.Marshal(keys)
	if err != nil {
		t.Fatalf("marshal keys: %v", err)
	}
	t.Setenv("PROVENA_API_KEYS", string(raw))
	ks, err := NewKeyStore()
	if err != nil {
		t.Fatalf("NewKeyStore: %v", err)
	}
	return ks
}

// chain replicates the gateway's wrap order: Auth is the outer middleware so it
// populates the AuthContext before WritePermission reads it. If the two are
// reversed, WritePermission sees no context and silently allows every write --
// the regression this test guards against.

func chain(ks *KeyStore) http.Handler {
	final := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("reached"))
	})
	var h http.Handler = final
	h = WritePermissionMiddleware(h)
	h = AuthMiddleware(ks, true)(h)
	return h
}

func doRequest(h http.Handler, method, path, token string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestWritePermissionBlocksViewerWrite(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{
		{KeyID: "v1", TenantID: "t1", Role: Viewer, HashedKey: hashToken("viewer-token")},
		{KeyID: "e1", TenantID: "t1", Role: Editor, HashedKey: hashToken("editor-token")},
	})
	h := chain(ks)

	// A viewer key must be rejected on a mutating route.
	if rec := doRequest(h, http.MethodPost, "/v1/memories", "viewer-token"); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer POST /v1/memories: got %d, want 403 (write-permission not enforced)", rec.Code)
	}

	// A viewer key may still read.
	if rec := doRequest(h, http.MethodGet, "/v1/memories/abc", "viewer-token"); rec.Code != http.StatusOK {
		t.Fatalf("viewer GET: got %d, want 200", rec.Code)
	}
	for _, path := range []string{
		"/v1/memories/search",
		"/v1/agent/context",
		"/v1/memories/graph/temporal",
	} {
		if rec := doRequest(h, http.MethodPost, path, "viewer-token"); rec.Code != http.StatusOK {
			t.Fatalf("viewer POST %s: got %d, want 200", path, rec.Code)
		}
	}
	if rec := doRequest(h, http.MethodPost, "/v1/memories/search/refresh", "viewer-token"); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer POST nested memory route: got %d, want 403", rec.Code)
	}
	if rec := doRequest(h, http.MethodPost, "/v1/repositories/repo-1/memory-events/sync", "viewer-token"); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer repository sync: got %d, want 403", rec.Code)
	}

	// An editor key may write.
	if rec := doRequest(h, http.MethodPost, "/v1/memories", "editor-token"); rec.Code != http.StatusOK {
		t.Fatalf("editor POST /v1/memories: got %d, want 200", rec.Code)
	}
	if rec := doRequest(h, http.MethodPost, "/v1/repositories/repo-1/memory-events/sync", "editor-token"); rec.Code != http.StatusOK {
		t.Fatalf("editor repository sync: got %d, want 200", rec.Code)
	}
}

func TestAuthMiddlewareRejectsMissingAndBadKeys(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{
		{KeyID: "e1", TenantID: "t1", Role: Editor, HashedKey: hashToken("editor-token")},
	})
	h := chain(ks)

	if rec := doRequest(h, http.MethodGet, "/v1/memories/abc", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("missing key: got %d, want 401", rec.Code)
	}
	if rec := doRequest(h, http.MethodGet, "/v1/memories/abc", "not-a-real-key"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("bad key: got %d, want 401", rec.Code)
	}
}

func TestAuthMiddlewareUsesVerifiedIdentityClaims(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{
		{
			KeyID:       "e1",
			TenantID:    "t1",
			Role:        Editor,
			PrincipalID: "verified-principal",
			Groups:      []string{"verified-group", "platform"},
			HashedKey:   hashToken("editor-token"),
		},
	})

	var got *AuthContext
	h := AuthMiddleware(ks, true)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got, _ = FromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	}))
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/abc", nil)
	req.Header.Set("Authorization", "Bearer editor-token")
	req.Header.Set("X-Provena-Principal-Id", "spoofed-principal")
	req.Header.Set("X-Provena-Groups", "spoofed-group")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("request: got %d, want 200", rec.Code)
	}
	if got == nil {
		t.Fatal("authenticated context missing")
	}
	if got.PrincipalID != "verified-principal" {
		t.Fatalf("principal: got %q, want verified-principal", got.PrincipalID)
	}
	if len(got.Groups) != 2 || got.Groups[0] != "verified-group" || got.Groups[1] != "platform" {
		t.Fatalf("groups: got %v, want verified key claims", got.Groups)
	}
}

func TestAuthMiddlewareDefaultsPrincipalToKeyID(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{
		{KeyID: "e1", TenantID: "t1", Role: Editor, HashedKey: hashToken("editor-token")},
	})

	var got *AuthContext
	h := AuthMiddleware(ks, true)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got, _ = FromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	}))
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/abc", nil)
	req.Header.Set("Authorization", "Bearer editor-token")
	req.Header.Set("X-Provena-Principal-Id", "spoofed-principal")
	req.Header.Set("X-Provena-Groups", "spoofed-group")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("request: got %d, want 200", rec.Code)
	}
	if got == nil {
		t.Fatal("authenticated context missing")
	}
	if got.PrincipalID != "e1" {
		t.Fatalf("principal: got %q, want key id e1", got.PrincipalID)
	}
	if len(got.Groups) != 0 {
		t.Fatalf("groups: got %v, want no groups", got.Groups)
	}
}

func TestVerifiedKeyHandlerFailsClosedWhenAuthIsDisabled(t *testing.T) {
	keyStore, err := NewKeyStore()
	if err != nil {
		t.Fatalf("new key store: %v", err)
	}
	handler := AuthMiddleware(keyStore, false)(VerifiedKeyHandler())
	recorder := doRequest(handler, http.MethodGet, "/v1/auth/validate", "arbitrary")
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("auth-disabled validation got %d, want 401", recorder.Code)
	}
}

func TestAuthDisabledPreservesLocalIdentityHeaders(t *testing.T) {
	ks := keyStoreWith(t, nil)

	var got *AuthContext
	h := AuthMiddleware(ks, false)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got, _ = FromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	}))
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/abc", nil)
	req.Header.Set("X-Provena-Principal-Id", "local-principal")
	req.Header.Set("X-Provena-Groups", "local-group,developers")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("request: got %d, want 200", rec.Code)
	}
	if got == nil {
		t.Fatal("local context missing")
	}
	if got.PrincipalID != "local-principal" {
		t.Fatalf("principal: got %q, want local-principal", got.PrincipalID)
	}
	if len(got.Groups) != 2 || got.Groups[0] != "local-group" || got.Groups[1] != "developers" {
		t.Fatalf("groups: got %v, want local-mode headers", got.Groups)
	}
}

func TestAdminRoutesRequireAdminPermissionForEveryMethod(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{
		{KeyID: "v1", TenantID: "t1", Role: Viewer, HashedKey: hashToken("viewer-token")},
		{KeyID: "e1", TenantID: "t1", Role: Editor, HashedKey: hashToken("editor-token")},
		{KeyID: "a1", TenantID: "t1", Role: Admin, HashedKey: hashToken("admin-token")},
	})
	h := chain(ks)
	for _, method := range []string{http.MethodGet, http.MethodPost} {
		for _, token := range []string{"viewer-token", "editor-token"} {
			if rec := doRequest(h, method, "/v1/admin/retention-policies", token); rec.Code != http.StatusForbidden {
				t.Fatalf("%s admin route with %s: got %d, want 403", method, token, rec.Code)
			}
		}
		if rec := doRequest(h, method, "/v1/admin/retention-policies", "admin-token"); rec.Code != http.StatusOK {
			t.Fatalf("admin %s: got %d, want 200", method, rec.Code)
		}
	}
}

func TestAuthMiddlewareUsesAPIKeyIdentityMetadata(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{{
		KeyID:       "key-a",
		TenantID:    "tenant-a",
		Role:        Editor,
		PrincipalID: "configured-principal",
		Groups:      []string{" team-a ", "", "team-b"},
		HashedKey:   hashToken("external-token"),
	}})
	var got AuthContext
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ac, ok := FromContext(r.Context())
		if !ok {
			t.Fatal("missing auth context")
		}
		got = *ac
		got.Groups = append([]string(nil), ac.Groups...)
		w.WriteHeader(http.StatusNoContent)
	})
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/id", nil)
	req.Header.Set("Authorization", "Bearer external-token")
	req.Header.Set("X-Provena-Principal-Id", "attacker-principal")
	req.Header.Set("X-Provena-Groups", "attacker-group")
	rec := httptest.NewRecorder()
	AuthMiddleware(ks, true)(next).ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", rec.Code)
	}
	if got.PrincipalID != "configured-principal" {
		t.Fatalf("principal = %q, want configured-principal", got.PrincipalID)
	}
	if len(got.Groups) != 2 || got.Groups[0] != "team-a" || got.Groups[1] != "team-b" {
		t.Fatalf("groups = %#v, want configured metadata", got.Groups)
	}
}

func TestDisabledAuthRetainsExplicitLocalIdentityHeaders(t *testing.T) {
	ks := keyStoreWith(t, nil)
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ac, _ := FromContext(r.Context())
		if ac.PrincipalID != "local-principal" || len(ac.Groups) != 1 || ac.Groups[0] != "local-group" {
			t.Fatalf("local identity = principal %q groups %#v", ac.PrincipalID, ac.Groups)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	req := httptest.NewRequest(http.MethodGet, "/v1/memories/id", nil)
	req.Header.Set("X-Provena-Principal-Id", "local-principal")
	req.Header.Set("X-Provena-Groups", "local-group")
	rec := httptest.NewRecorder()
	AuthMiddleware(ks, false)(next).ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", rec.Code)
	}
}

func TestAuthMiddlewareLeavesHealthAndReadinessProbesPublic(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{
		{KeyID: "e1", TenantID: "t1", Role: Editor, HashedKey: hashToken("editor-token")},
	})
	h := chain(ks)

	for _, path := range []string{"/healthz", "/readyz"} {
		if rec := doRequest(h, http.MethodGet, path, ""); rec.Code != http.StatusOK {
			t.Fatalf("GET %s without bearer: got %d, want 200", path, rec.Code)
		}
	}
}
