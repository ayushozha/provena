// Package auth provides API key authentication and role-based access control.
package auth

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// Role represents an authorization level.
type Role string

const (
	Viewer     Role = "viewer"
	Editor     Role = "editor"
	Admin      Role = "admin"
	SuperAdmin Role = "superadmin"
)

// Permission represents a granular access right.
type Permission string

const (
	Read      Permission = "read"
	Write     Permission = "write"
	Delete    Permission = "delete"
	AdminPerm Permission = "admin"
)

// RolePermissions maps each role to its allowed permissions.
var RolePermissions = map[Role][]Permission{
	Viewer:     {Read},
	Editor:     {Read, Write},
	Admin:      {Read, Write, Delete, AdminPerm},
	SuperAdmin: {Read, Write, Delete, AdminPerm},
}

// APIKey represents a stored API key with metadata.
type APIKey struct {
	KeyID       string    `json:"key_id"`
	TenantID    string    `json:"tenant_id"`
	Role        Role      `json:"role"`
	PrincipalID string    `json:"principal_id,omitempty"`
	Groups      []string  `json:"groups,omitempty"`
	HashedKey   string    `json:"hashed_key"`
	Description string    `json:"description"`
	CreatedAt   time.Time `json:"created_at"`
	ExpiresAt   time.Time `json:"expires_at"`
}

// AuthContext carries authenticated identity through the request context.
type AuthContext struct {
	KeyID       string
	TenantID    string
	Role        Role
	PrincipalID string
	Groups      []string
}

type contextKey string

const authContextKey contextKey = "auth"

// FromContext extracts the AuthContext from a request context.
func FromContext(ctx context.Context) (*AuthContext, bool) {
	ac, ok := ctx.Value(authContextKey).(*AuthContext)
	return ac, ok
}

// HasPermission checks whether the given role has the specified permission.
func HasPermission(r Role, p Permission) bool {
	perms, ok := RolePermissions[r]
	if !ok {
		return false
	}
	for _, pp := range perms {
		if pp == p {
			return true
		}
	}
	return false
}

// KeyStore holds API keys in memory, loaded from PROVENA_API_KEYS env var.
type KeyStore struct {
	mu   sync.RWMutex
	keys map[string]APIKey // keyed by hashed key hex
}

// NewKeyStore creates a key store and loads keys from the environment.
// The PROVENA_API_KEYS env var should contain a JSON array of APIKey objects.
func NewKeyStore() (*KeyStore, error) {
	ks := &KeyStore{keys: make(map[string]APIKey)}
	raw := os.Getenv("PROVENA_API_KEYS")
	if raw == "" {
		return ks, nil
	}
	var keys []APIKey
	if err := json.Unmarshal([]byte(raw), &keys); err != nil {
		return nil, fmt.Errorf("auth: parsing PROVENA_API_KEYS: %w", err)
	}
	for _, k := range keys {
		ks.keys[k.HashedKey] = k
	}
	return ks, nil
}

// Validate checks a raw bearer token against the store using constant-time comparison.
func (ks *KeyStore) Validate(rawToken string) (*AuthContext, bool) {
	h := sha256.Sum256([]byte(rawToken))
	hashed := hex.EncodeToString(h[:])

	ks.mu.RLock()
	defer ks.mu.RUnlock()

	for storedHash, key := range ks.keys {
		if subtle.ConstantTimeCompare([]byte(hashed), []byte(storedHash)) == 1 {
			if !key.ExpiresAt.IsZero() && time.Now().After(key.ExpiresAt) {
				return nil, false
			}
			principalID := strings.TrimSpace(key.PrincipalID)
			if principalID == "" {
				principalID = key.KeyID
			}
			groups := make([]string, 0, len(key.Groups))
			for _, group := range key.Groups {
				if group = strings.TrimSpace(group); group != "" {
					groups = append(groups, group)
				}
			}
			return &AuthContext{
				KeyID:       key.KeyID,
				TenantID:    key.TenantID,
				Role:        key.Role,
				PrincipalID: principalID,
				Groups:      groups,
			}, true
		}
	}
	return nil, false
}

// AuthMiddleware returns HTTP middleware that enforces API key authentication.
// If authEnabled is false, it injects a default super-admin context.
func AuthMiddleware(ks *KeyStore, authEnabled bool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/healthz" || r.URL.Path == "/readyz" {
				next.ServeHTTP(w, r)
				return
			}
			if !authEnabled {
				ctx := context.WithValue(r.Context(), authContextKey, &AuthContext{
					KeyID:       "anonymous",
					TenantID:    "default",
					Role:        SuperAdmin,
					PrincipalID: r.Header.Get("X-Provena-Principal-Id"),
					Groups:      splitCSV(r.Header.Get("X-Provena-Groups")),
				})
				next.ServeHTTP(w, r.WithContext(ctx))
				return
			}

			header := r.Header.Get("Authorization")
			if header == "" || !strings.HasPrefix(header, "Bearer ") {
				http.Error(w, `{"error":"missing or invalid Authorization header"}`, http.StatusUnauthorized)
				return
			}
			token := strings.TrimPrefix(header, "Bearer ")

			ac, ok := ks.Validate(token)
			if !ok {
				http.Error(w, `{"error":"invalid or expired API key"}`, http.StatusUnauthorized)
				return
			}

			ctx := context.WithValue(r.Context(), authContextKey, ac)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

func splitCSV(value string) []string {
	if value == "" {
		return nil
	}
	parts := strings.Split(value, ",")
	groups := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			groups = append(groups, part)
		}
	}
	return groups
}

// WritePermissionMiddleware rejects viewer keys on mutating memory routes.
// POST /v1/memories/search is explicitly read-only despite its HTTP method.
func WritePermissionMiddleware(next http.Handler) http.Handler {
	writePrefixes := []string{
		"/v1/memories",
		"/v1/project-snapshots",
		"/v1/admin/",
		"/v1/integrations/",
		"/v1/agent/context",
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if strings.HasPrefix(path, "/v1/admin/") {
			ac, ok := FromContext(r.Context())
			if !ok || !HasPermission(ac.Role, AdminPerm) {
				http.Error(w, `{"error":"admin access required"}`, http.StatusForbidden)
				return
			}
			next.ServeHTTP(w, r)
			return
		}
		if r.Method == http.MethodGet || r.Method == http.MethodHead || r.Method == http.MethodOptions {
			next.ServeHTTP(w, r)
			return
		}
		if r.Method == http.MethodPost && path == "/v1/memories/search" {
			next.ServeHTTP(w, r)
			return
		}
		needsWrite := false
		for _, prefix := range writePrefixes {
			if strings.HasPrefix(path, prefix) {
				needsWrite = true
				break
			}
		}
		if needsWrite {
			if ac, ok := FromContext(r.Context()); ok && !HasPermission(ac.Role, Write) {
				http.Error(w, `{"error":"write access required"}`, http.StatusForbidden)
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}
