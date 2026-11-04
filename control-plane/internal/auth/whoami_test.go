package auth

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestWhoAmIReportsVerifiedClaimsOnly(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{{
		KeyID:       "local-key",
		TenantID:    "tenant-local",
		Role:        Editor,
		PrincipalID: "person-1",
		Scope:       KeyScope{WorkspaceID: "ws-1", UserID: "user-1"},
		HashedKey:   hashToken("local-token"),
	}})
	handler := AuthMiddleware(ks, true)(WhoAmIHandler())

	req := httptest.NewRequest(http.MethodGet, "/v1/auth/whoami", nil)
	req.Header.Set("Authorization", "Bearer local-token")
	// Caller-supplied identity headers must not influence the answer.
	req.Header.Set("X-Provena-Tenant-Id", "spoofed-tenant")
	req.Header.Set("X-Provena-Principal-Id", "spoofed-principal")
	req.Header.Set("X-Provena-Role", "superadmin")
	req.Header.Set("X-Provena-Scope-Workspace-Id", "spoofed-workspace")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("verified key: got %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control: got %q, want no-store", got)
	}
	var claims struct {
		KeyID       string   `json:"key_id"`
		TenantID    string   `json:"tenant_id"`
		Role        string   `json:"role"`
		PrincipalID string   `json:"principal_id"`
		Scope       KeyScope `json:"scope"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &claims); err != nil {
		t.Fatalf("decode: %v", err)
	}
	want := map[string]string{"key_id": "local-key", "tenant_id": "tenant-local", "role": "editor", "principal_id": "person-1"}
	got := map[string]string{"key_id": claims.KeyID, "tenant_id": claims.TenantID, "role": claims.Role, "principal_id": claims.PrincipalID}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("claims: got %v, want %v", got, want)
	}
	if claims.Scope != (KeyScope{WorkspaceID: "ws-1", UserID: "user-1"}) {
		t.Errorf("scope: got %+v, want the key's bound scope", claims.Scope)
	}

	for name, h := range map[string]http.Handler{
		"missing token": handler,
		"auth disabled": AuthMiddleware(ks, false)(WhoAmIHandler()),
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/auth/whoami", nil))
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("%s: got %d, want 401", name, rec.Code)
		}
	}
}

func TestUnboundListsKeysWithoutACanonicalFullScope(t *testing.T) {
	ks := keyStoreWith(t, []APIKey{
		{KeyID: "bound", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "ws", UserID: "u"}, HashedKey: hashToken("a")},
		{KeyID: "bound-opaque", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "a2cb316e-f9af-460a-b773-1729bcb3dccd", UserID: "person 1"}, HashedKey: hashToken("b")},
		{KeyID: "no-user", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "ws"}, HashedKey: hashToken("c")},
		{KeyID: "no-scope", TenantID: "t", Role: Editor, HashedKey: hashToken("d")},
		// Values that look set but would be stripped to nothing, or altered, by the store.
		{KeyID: "spaces-user", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "ws", UserID: "   "}, HashedKey: hashToken("e")},
		{KeyID: "tab-workspace", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "\t", UserID: "u"}, HashedKey: hashToken("f")},
		{KeyID: "newline-user", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "ws", UserID: "\n"}, HashedKey: hashToken("g")},
		{KeyID: "padded-workspace", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: " ws ", UserID: "u"}, HashedKey: hashToken("h")},
		{KeyID: "trailing-newline", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "ws", UserID: "u\n"}, HashedKey: hashToken("i")},
		{KeyID: "control-inside", TenantID: "t", Role: Editor, Scope: KeyScope{WorkspaceID: "ws", UserID: "u\x00v"}, HashedKey: hashToken("j")},
	})
	want := []string{"control-inside", "newline-user", "no-scope", "no-user", "padded-workspace", "spaces-user", "tab-workspace", "trailing-newline"}
	if got := ks.Unbound(); !reflect.DeepEqual(got, want) {
		t.Fatalf("Unbound: got %v, want %v", got, want)
	}
	for _, scope := range []KeyScope{{WorkspaceID: "ws", UserID: "u"}, {WorkspaceID: "ws 1", UserID: "u-1"}} {
		if !scope.Bound() {
			t.Errorf("%+v must count as bound", scope)
		}
	}
}
