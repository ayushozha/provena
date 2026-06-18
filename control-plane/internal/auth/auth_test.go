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

	// An editor key may write.
	if rec := doRequest(h, http.MethodPost, "/v1/memories", "editor-token"); rec.Code != http.StatusOK {
		t.Fatalf("editor POST /v1/memories: got %d, want 200", rec.Code)
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
