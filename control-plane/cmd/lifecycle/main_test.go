package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	lifecycleTokenSentinel = "lifecycle-service-token-never-log"
	lifecycleCallerToken   = "external-caller-token-never-forward"
	lifecycleIngressToken  = "gateway-internal-token-never-forward"
)

func lifecycleEnv() map[string]string {
	return map[string]string{
		"PROVENA_ENVIRONMENT":                 "production",
		"PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "false",
		"PROVENA_GATEWAY_SERVICE_TOKEN":       lifecycleIngressToken,
		"PROVENA_LIFECYCLE_SERVICE_TOKEN":     lifecycleTokenSentinel,
		"PROVENA_LIFECYCLE_TENANT_ID":         "tenant-a",
		"PROVENA_LIFECYCLE_PRINCIPAL_ID":      "lifecycle-a",
	}
}

func lifecycleEnvLookup(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

func completeLifecycleAuth() lifecycleServiceAuth {
	return lifecycleServiceAuth{
		ingressToken: lifecycleIngressToken,
		enabled:      true,
		token:        lifecycleTokenSentinel,
		tenantID:     "tenant-a",
		principalID:  "lifecycle-a",
	}
}

func setLifecycleCaller(request *http.Request, role, tenantID string) {
	request.Header.Set("Authorization", "Bearer "+lifecycleIngressToken)
	request.Header.Set("X-Provena-Role", role)
	request.Header.Set("X-Provena-Key-Id", "gateway-key")
	request.Header.Set("X-Provena-Principal-Id", "gateway-principal")
	if tenantID != "" {
		request.Header.Set("X-Provena-Tenant-Id", tenantID)
	}
}

func TestLoadLifecycleServiceAuth(t *testing.T) {
	tests := []struct {
		name        string
		env         map[string]string
		wantEnabled bool
		wantErr     bool
	}{
		{name: "complete production identity", env: lifecycleEnv(), wantEnabled: true},
		{name: "explicit local bypass", env: map[string]string{"PROVENA_ENVIRONMENT": "development", "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "true"}},
		{name: "local principal default", env: map[string]string{"PROVENA_ENVIRONMENT": "development", "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "true", "PROVENA_LIFECYCLE_PRINCIPAL_ID": "provena-lifecycle"}},
		{name: "production missing identity", env: map[string]string{"PROVENA_ENVIRONMENT": "production"}, wantErr: true},
		{name: "partial identity", env: map[string]string{"PROVENA_ENVIRONMENT": "production", "PROVENA_LIFECYCLE_SERVICE_TOKEN": lifecycleTokenSentinel}, wantErr: true},
		{name: "reused ingress and service token", env: map[string]string{
			"PROVENA_ENVIRONMENT":                 "production",
			"PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "false",
			"PROVENA_GATEWAY_SERVICE_TOKEN":       lifecycleTokenSentinel,
			"PROVENA_LIFECYCLE_SERVICE_TOKEN":     lifecycleTokenSentinel,
			"PROVENA_LIFECYCLE_TENANT_ID":         "tenant-a",
			"PROVENA_LIFECYCLE_PRINCIPAL_ID":      "lifecycle-a",
		}, wantErr: true},
		{name: "invalid local flag", env: map[string]string{"PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "sometimes"}, wantErr: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			auth, err := loadLifecycleServiceAuth(lifecycleEnvLookup(test.env))
			if (err != nil) != test.wantErr {
				t.Fatalf("loadLifecycleServiceAuth() error = %v, wantErr %v", err, test.wantErr)
			}
			if err != nil {
				if strings.Contains(err.Error(), lifecycleTokenSentinel) || strings.Contains(err.Error(), lifecycleIngressToken) {
					t.Fatal("configuration error exposed lifecycle bearer")
				}
				return
			}
			if auth.enabled != test.wantEnabled {
				t.Fatalf("enabled = %v, want %v", auth.enabled, test.wantEnabled)
			}
		})
	}
}

func assertLifecycleHeaders(t *testing.T, headers http.Header) {
	t.Helper()
	expected := map[string]string{
		"Authorization":          "Bearer " + lifecycleTokenSentinel,
		"X-Provena-Tenant-Id":    "tenant-a",
		"X-Provena-Role":         lifecycleRole,
		"X-Provena-Key-Id":       lifecycleKeyID,
		"X-Provena-Principal-Id": "lifecycle-a",
		"X-Provena-Groups":       lifecycleGroups,
	}
	for name, want := range expected {
		if got := headers.Get(name); got != want {
			t.Errorf("%s = %q, want %q", name, got, want)
		}
	}
}

func TestProxyReplacesCallerIdentityWithLifecycleServiceIdentity(t *testing.T) {
	type capturedRequest struct {
		headers http.Header
		body    string
	}
	captured := make(chan capturedRequest, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		captured <- capturedRequest{headers: request.Header.Clone(), body: string(body)}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	request := httptest.NewRequest(http.MethodPost, "/v1/admin/retention/enforce", strings.NewReader(`{"tenant_id":"tenant-a"}`))
	setLifecycleCaller(request, "superadmin", "tenant-other")
	request.Header.Set("X-Provena-Tenant-Id", "tenant-other")
	request.Header.Set("X-Provena-Principal-Id", "caller")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	health := &lifecycleHealth{}
	health.healthy.Store(true)
	proxy(upstream.Client(), upstream.URL, "", completeLifecycleAuth(), health).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("proxy status = %d, body = %s", response.Code, response.Body.String())
	}
	got := <-captured
	assertLifecycleHeaders(t, got.headers)
	if strings.Contains(got.headers.Get("Authorization"), lifecycleIngressToken) || strings.Contains(got.body, lifecycleIngressToken) {
		t.Fatal("proxy forwarded gateway ingress bearer")
	}
}

func TestProxyRejectsNonAdminAndForeignTenantBeforeReadingOrCallingUpstream(t *testing.T) {
	tests := []struct {
		name     string
		role     string
		tenantID string
		mutate   func(*http.Request)
	}{
		{name: "same tenant editor", role: "editor", tenantID: "tenant-a"},
		{name: "foreign tenant admin", role: "admin", tenantID: "tenant-other"},
		{name: "missing authoritative principal", role: "admin", tenantID: "tenant-a", mutate: func(request *http.Request) {
			request.Header.Del("X-Provena-Principal-Id")
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var upstreamCalls atomic.Int64
			upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
				upstreamCalls.Add(1)
			}))
			defer upstream.Close()
			body := &lifecycleReadCountingBody{}
			request := httptest.NewRequest(http.MethodPost, "/v1/admin/retention/enforce", body)
			setLifecycleCaller(request, test.role, test.tenantID)
			if test.mutate != nil {
				test.mutate(request)
			}
			response := httptest.NewRecorder()
			proxy(upstream.Client(), upstream.URL, "", completeLifecycleAuth(), &lifecycleHealth{}).ServeHTTP(response, request)
			if response.Code != http.StatusForbidden || body.reads.Load() != 0 || upstreamCalls.Load() != 0 {
				t.Fatalf(
					"status = %d, body reads = %d, upstream calls = %d",
					response.Code,
					body.reads.Load(),
					upstreamCalls.Load(),
				)
			}
			if strings.Contains(response.Body.String(), lifecycleIngressToken) {
				t.Fatal("authorization denial exposed gateway bearer")
			}
		})
	}
}

type lifecycleReadCountingBody struct {
	reads atomic.Int64
}

func (body *lifecycleReadCountingBody) Read([]byte) (int, error) {
	body.reads.Add(1)
	return 0, io.EOF
}

func TestProxyRejectsCallerBearerBeforeReadingOrCallingUpstream(t *testing.T) {
	var upstreamCalls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		upstreamCalls.Add(1)
	}))
	defer upstream.Close()
	health := &lifecycleHealth{}
	health.healthy.Store(true)
	body := &lifecycleReadCountingBody{}
	request := httptest.NewRequest(http.MethodPost, "/v1/admin/retention/enforce", body)
	request.Header.Set("Authorization", "Bearer "+lifecycleCallerToken)
	response := httptest.NewRecorder()
	proxy(upstream.Client(), upstream.URL, "", completeLifecycleAuth(), health).ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized || body.reads.Load() != 0 || upstreamCalls.Load() != 0 {
		t.Fatalf(
			"status = %d, body reads = %d, upstream calls = %d",
			response.Code,
			body.reads.Load(),
			upstreamCalls.Load(),
		)
	}
	if strings.Contains(response.Body.String(), lifecycleCallerToken) || strings.Contains(response.Body.String(), lifecycleIngressToken) {
		t.Fatal("ingress denial exposed a bearer")
	}
}

func TestProxyBoundsAuthorizedBodyBeforeCallingUpstream(t *testing.T) {
	var upstreamCalls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil || len(body) != maxLifecycleBodyBytes {
			t.Errorf("forwarded body length = %d, error = %v", len(body), err)
		}
		upstreamCalls.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	handler := proxy(upstream.Client(), upstream.URL, "", completeLifecycleAuth(), &lifecycleHealth{})
	for _, size := range []int{maxLifecycleBodyBytes + 1, maxLifecycleBodyBytes} {
		request := httptest.NewRequest(http.MethodPost, "/v1/admin/legal-hold", strings.NewReader(strings.Repeat(" ", size)))
		setLifecycleCaller(request, "admin", "tenant-a")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if size > maxLifecycleBodyBytes {
			if response.Code != http.StatusRequestEntityTooLarge || upstreamCalls.Load() != 0 {
				t.Fatalf("oversized status = %d, upstream calls = %d", response.Code, upstreamCalls.Load())
			}
		} else if response.Code != http.StatusNoContent || upstreamCalls.Load() != 1 {
			t.Fatalf("exact-limit status = %d, upstream calls = %d", response.Code, upstreamCalls.Load())
		}
	}
}

func TestProxyAuthorizationFailureMarksLifecycleUnhealthy(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":"denied"}`, http.StatusUnauthorized)
	}))
	defer upstream.Close()
	health := &lifecycleHealth{}
	health.healthy.Store(true)
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/admin/retention/enforce", strings.NewReader(`{"tenant_id":"tenant-a"}`))
	setLifecycleCaller(request, "admin", "tenant-a")
	proxy(upstream.Client(), upstream.URL, "", completeLifecycleAuth(), health).ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized || health.healthy.Load() {
		t.Fatalf("proxy status = %d, healthy = %v", response.Code, health.healthy.Load())
	}
}

func TestEnforceTenantUsesExactIdentityAndOneTenant(t *testing.T) {
	var listCalls atomic.Int64
	var enforceCalls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		assertLifecycleHeaders(t, request.Header)
		switch {
		case request.Method == http.MethodGet && request.URL.Path == "/v1/admin/retention-policies":
			listCalls.Add(1)
			if request.URL.Query().Get("tenant_id") != "tenant-a" {
				t.Error("retention lookup was not tenant-bound")
			}
			_, _ = w.Write([]byte(`[{"policy_id":"policy-a","tenant_id":"tenant-a"}]`))
		case request.Method == http.MethodPost && request.URL.Path == "/v1/admin/retention/enforce":
			enforceCalls.Add(1)
			var payload map[string]string
			if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
				t.Errorf("decode enforcement payload: %v", err)
			}
			if payload["tenant_id"] != "tenant-a" {
				t.Errorf("enforcement tenant = %q", payload["tenant_id"])
			}
			_, _ = w.Write([]byte(`{"expired_memory_ids":[]}`))
		default:
			http.NotFound(w, request)
		}
	}))
	defer upstream.Close()

	if err := enforceTenant(context.Background(), upstream.Client(), upstream.URL, completeLifecycleAuth(), slog.Default()); err != nil {
		t.Fatal(err)
	}
	if listCalls.Load() != 1 || enforceCalls.Load() != 1 {
		t.Fatalf("list calls = %d, enforce calls = %d", listCalls.Load(), enforceCalls.Load())
	}
}

func TestRetentionAuthorizationFailureMarksServiceUnhealthyWithoutLoggingSecret(t *testing.T) {
	called := make(chan struct{}, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called <- struct{}{}
		http.Error(w, `{"error":"denied"}`, http.StatusForbidden)
	}))
	defer upstream.Close()

	var logs bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logs, nil))
	health := &lifecycleHealth{}
	health.healthy.Store(true)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go runRetentionLoop(ctx, upstream.Client(), upstream.URL, completeLifecycleAuth(), health, logger, time.Hour)
	select {
	case <-called:
	case <-time.After(time.Second):
		t.Fatal("retention authorization request was not attempted")
	}
	deadline := time.Now().Add(time.Second)
	for (health.healthy.Load() || !strings.Contains(logs.String(), "returned 403")) && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	response := httptest.NewRecorder()
	health.handler(response, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("health status = %d, want 503", response.Code)
	}
	if !strings.Contains(logs.String(), "returned 403") {
		t.Fatal("authorization failure was not logged as an error")
	}
	if strings.Contains(logs.String(), lifecycleTokenSentinel) || strings.Contains(response.Body.String(), lifecycleTokenSentinel) {
		t.Fatal("lifecycle health failure exposed service bearer")
	}
}
