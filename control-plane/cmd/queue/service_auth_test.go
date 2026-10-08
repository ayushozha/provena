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

	queuepkg "github.com/altrixy/provena/control-plane/internal/queue"
)

const (
	serviceTokenSentinel = "service-token-sentinel-never-log"
	callerTokenSentinel  = "caller-token-sentinel-never-queue"
	ingressTokenSentinel = "queue-ingress-sentinel-never-log"
)

func envLookup(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

func completeServiceEnv() map[string]string {
	return map[string]string{
		"PROVENA_ENVIRONMENT":                 "production",
		"PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "false",
		"PROVENA_QUEUE_INGRESS_TOKEN":         ingressTokenSentinel,
		"PROVENA_SERVICE_TOKEN":               serviceTokenSentinel,
		"PROVENA_SERVICE_TENANT_ID":           "tenant-a",
		"PROVENA_SERVICE_PRINCIPAL_ID":        "queue-principal-a",
		"PROVENA_SERVICE_ROLE":                "editor",
	}
}

func completeServiceAuth() queueServiceAuth {
	return queueServiceAuth{
		ingressToken: ingressTokenSentinel,
		enabled:      true,
		token:        serviceTokenSentinel,
		tenantID:     "tenant-a",
		principalID:  "queue-principal-a",
		role:         "editor",
	}
}

func TestLoadQueueServiceAuth(t *testing.T) {
	tests := []struct {
		name        string
		env         map[string]string
		wantEnabled bool
		wantRole    string
		wantErr     bool
	}{
		{name: "explicit local bypass", env: map[string]string{"PROVENA_ENVIRONMENT": "development", "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "true", "PROVENA_QUEUE_INGRESS_TOKEN": ingressTokenSentinel}},
		{name: "local store identity defaults without credential", env: map[string]string{"PROVENA_ENVIRONMENT": "development", "PROVENA_SERVICE_PRINCIPAL_ID": "provena-service", "PROVENA_SERVICE_ROLE": "editor", "PROVENA_QUEUE_INGRESS_TOKEN": ingressTokenSentinel}},
		{name: "complete editor", env: completeServiceEnv(), wantEnabled: true, wantRole: "editor"},
		{name: "complete admin", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_SERVICE_ROLE"] = " ADMIN "
			return values
		}(), wantEnabled: true, wantRole: "admin"},
		{name: "default editor role", env: func() map[string]string {
			values := completeServiceEnv()
			delete(values, "PROVENA_SERVICE_ROLE")
			return values
		}(), wantEnabled: true, wantRole: "editor"},
		{name: "production missing identity", env: map[string]string{"PROVENA_ENVIRONMENT": "production"}, wantErr: true},
		{name: "local bypass disabled", env: map[string]string{"PROVENA_ENVIRONMENT": "local", "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "false"}, wantErr: true},
		{name: "missing token", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_SERVICE_TOKEN"] = " "
			return values
		}(), wantErr: true},
		{name: "missing tenant", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_SERVICE_TENANT_ID"] = " "
			return values
		}(), wantErr: true},
		{name: "missing principal", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_SERVICE_PRINCIPAL_ID"] = " "
			return values
		}(), wantErr: true},
		{name: "partial identity in local mode", env: map[string]string{"PROVENA_ENVIRONMENT": "test", "PROVENA_SERVICE_TOKEN": serviceTokenSentinel}, wantErr: true},
		{name: "viewer cannot write", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_SERVICE_ROLE"] = "viewer"
			return values
		}(), wantErr: true},
		{name: "superadmin is not a store service role", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_SERVICE_ROLE"] = "superadmin"
			return values
		}(), wantErr: true},
		{name: "unknown role", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_SERVICE_ROLE"] = "owner"
			return values
		}(), wantErr: true},
		{name: "invalid local bypass flag", env: map[string]string{"PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "sometimes"}, wantErr: true},
		{name: "missing ingress token", env: map[string]string{"PROVENA_ENVIRONMENT": "development", "PROVENA_ALLOW_UNAUTHENTICATED_LOCAL": "true"}, wantErr: true},
		{name: "reused ingress and service token", env: func() map[string]string {
			values := completeServiceEnv()
			values["PROVENA_QUEUE_INGRESS_TOKEN"] = serviceTokenSentinel
			return values
		}(), wantErr: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			auth, err := loadQueueServiceAuth(envLookup(test.env))
			if (err != nil) != test.wantErr {
				t.Fatalf("loadQueueServiceAuth() error = %v, wantErr %v", err, test.wantErr)
			}
			if err != nil {
				if strings.Contains(err.Error(), serviceTokenSentinel) || strings.Contains(err.Error(), ingressTokenSentinel) {
					t.Fatal("configuration error exposed service bearer")
				}
				return
			}
			if auth.enabled != test.wantEnabled {
				t.Fatalf("enabled = %v, want %v", auth.enabled, test.wantEnabled)
			}
			if auth.enabled && auth.role != test.wantRole {
				t.Fatalf("role = %q, want %q", auth.role, test.wantRole)
			}
		})
	}
}

func TestPipelineProcessFnAppliesOnlyServiceIdentity(t *testing.T) {
	type capture struct {
		headers http.Header
		body    []byte
	}
	captured := make(chan capture, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
		}
		captured <- capture{headers: r.Header.Clone(), body: body}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"results":[]}`))
	}))
	defer upstream.Close()

	process := newPipelineProcessFn(upstream.Client(), upstream.URL+"/", completeServiceAuth())
	batch := []queuepkg.WriteItem{{
		ID:        "item-a",
		TenantID:  "tenant-a",
		Payload:   []byte(`{"content":"safe background work"}`),
		CreatedAt: time.Unix(1, 0).UTC(),
	}}
	if err := process(context.Background(), batch); err != nil {
		t.Fatalf("process: %v", err)
	}
	got := <-captured
	expectedHeaders := map[string]string{
		"Authorization":          "Bearer " + serviceTokenSentinel,
		"X-Provena-Tenant-Id":    "tenant-a",
		"X-Provena-Role":         "editor",
		"X-Provena-Key-Id":       queueKeyID,
		"X-Provena-Principal-Id": "queue-principal-a",
		"X-Provena-Groups":       queueGroups,
	}
	for name, want := range expectedHeaders {
		if value := got.headers.Get(name); value != want {
			t.Errorf("%s = %q, want %q", name, value, want)
		}
	}
	if got.headers.Get("Cookie") != "" || got.headers.Get("Traceparent") != "" || got.headers.Get("X-Forwarded-For") != "" {
		t.Fatal("dispatch included unrelated caller headers")
	}
	if bytes.Contains(got.body, []byte(serviceTokenSentinel)) || bytes.Contains(got.body, []byte(callerTokenSentinel)) {
		t.Fatal("dispatch body retained a bearer")
	}
}

func TestPipelineProcessFnRejectsInvalidIdentityOrTenantBeforeUpstream(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	tests := []struct {
		name  string
		auth  queueServiceAuth
		batch []queuepkg.WriteItem
	}{
		{name: "invalid enabled identity", auth: queueServiceAuth{enabled: true, token: serviceTokenSentinel}, batch: []queuepkg.WriteItem{{TenantID: "tenant-a"}}},
		{name: "empty tenant", auth: completeServiceAuth(), batch: []queuepkg.WriteItem{{TenantID: ""}}},
		{name: "foreign tenant", auth: completeServiceAuth(), batch: []queuepkg.WriteItem{{TenantID: "tenant-b"}}},
		{name: "mixed tenants", auth: completeServiceAuth(), batch: []queuepkg.WriteItem{{TenantID: "tenant-a"}, {TenantID: "tenant-b"}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			before := calls.Load()
			err := newPipelineProcessFn(upstream.Client(), upstream.URL, test.auth)(context.Background(), test.batch)
			if err == nil {
				t.Fatal("expected dispatch rejection")
			}
			if calls.Load() != before {
				t.Fatal("invalid dispatch reached upstream")
			}
			if strings.Contains(err.Error(), serviceTokenSentinel) {
				t.Fatal("dispatch error exposed service bearer")
			}
		})
	}
}

func TestPipelineProcessFnLocalModeEmitsNoServiceHeaders(t *testing.T) {
	captured := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	process := newPipelineProcessFn(upstream.Client(), upstream.URL, queueServiceAuth{ingressToken: ingressTokenSentinel})
	if err := process(context.Background(), []queuepkg.WriteItem{{ID: "local"}}); err != nil {
		t.Fatalf("local process: %v", err)
	}
	headers := <-captured
	for _, name := range []string{
		"Authorization",
		"X-Provena-Tenant-Id",
		"X-Provena-Role",
		"X-Provena-Key-Id",
		"X-Provena-Principal-Id",
		"X-Provena-Groups",
	} {
		if value := headers.Get(name); value != "" {
			t.Errorf("local dispatch emitted %s=%q", name, value)
		}
	}
}

func TestEnqueueHandlerIgnoresCallerHeadersAndFencesTenant(t *testing.T) {
	var logs bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logs, nil))
	captured := make(chan []queuepkg.WriteItem, 1)
	wq := queuepkg.NewWriteQueue(4, 1, 1, func(_ context.Context, batch []queuepkg.WriteItem) error {
		copyOfBatch := append([]queuepkg.WriteItem(nil), batch...)
		captured <- copyOfBatch
		return nil
	}, logger)
	ctx, cancel := context.WithCancel(context.Background())
	wq.Start(ctx)
	defer func() {
		cancel()
		wq.Drain(time.Second)
	}()
	handler := newEnqueueHandler(wq, completeServiceAuth())

	body, err := json.Marshal(queuepkg.WriteItem{ID: "item-a", TenantID: "tenant-a", Payload: []byte("safe")})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/enqueue", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+ingressTokenSentinel)
	req.Header.Set("Cookie", "session=caller-cookie")
	req.Header.Set("Traceparent", "caller-trace")
	req.Header.Set("X-Forwarded-For", "203.0.113.8")
	req.Header.Set("X-Provena-Tenant-Id", "tenant-b")
	req.Header.Set("X-Provena-Role", "superadmin")
	req.Header.Set("X-Provena-Key-Id", "caller-key")
	req.Header.Set("X-Provena-Principal-Id", "caller-principal")
	req.Header.Set("X-Provena-Groups", "caller-group")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	if response.Code != http.StatusAccepted {
		t.Fatalf("enqueue status = %d, body = %s", response.Code, response.Body.String())
	}

	var batch []queuepkg.WriteItem
	select {
	case batch = <-captured:
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for queued batch")
	}
	serialized, err := json.Marshal(batch)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{callerTokenSentinel, serviceTokenSentinel, ingressTokenSentinel, "caller-principal", "caller-group", "caller-key", "caller-cookie", "caller-trace", "203.0.113.8"} {
		if bytes.Contains(serialized, []byte(forbidden)) || strings.Contains(response.Body.String(), forbidden) || strings.Contains(logs.String(), forbidden) {
			t.Fatalf("caller or service credential data leaked: %q", forbidden)
		}
	}

	for _, tenantID := range []string{"", "tenant-b"} {
		rejectedBody, _ := json.Marshal(queuepkg.WriteItem{ID: "rejected", TenantID: tenantID, Payload: []byte("safe")})
		rejected := httptest.NewRecorder()
		rejectedRequest := httptest.NewRequest(http.MethodPost, "/enqueue", bytes.NewReader(rejectedBody))
		rejectedRequest.Header.Set("Authorization", "Bearer "+ingressTokenSentinel)
		handler.ServeHTTP(rejected, rejectedRequest)
		if rejected.Code != http.StatusForbidden {
			t.Errorf("tenant %q status = %d, want 403", tenantID, rejected.Code)
		}
	}
}

type readCountingBody struct {
	reads atomic.Int64
}

func (body *readCountingBody) Read([]byte) (int, error) {
	body.reads.Add(1)
	return 0, io.EOF
}

func TestEnqueueHandlerAuthenticatesBeforeReadingOrEnqueueing(t *testing.T) {
	var logs bytes.Buffer
	wq := queuepkg.NewWriteQueue(2, 1, 1, func(context.Context, []queuepkg.WriteItem) error {
		t.Fatal("unauthenticated item reached queue processor")
		return nil
	}, slog.New(slog.NewJSONHandler(&logs, nil)))
	handler := newEnqueueHandler(wq, completeServiceAuth())

	for _, authorization := range []string{"", "Bearer " + callerTokenSentinel} {
		body := &readCountingBody{}
		request := httptest.NewRequest(http.MethodPost, "/enqueue", body)
		if authorization != "" {
			request.Header.Set("Authorization", authorization)
		}
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusUnauthorized {
			t.Fatalf("authorization %q status = %d, want 401", authorization, response.Code)
		}
		if body.reads.Load() != 0 || wq.Depth() != 0 {
			t.Fatal("unauthenticated request body was read or enqueued")
		}
		for _, secret := range []string{callerTokenSentinel, ingressTokenSentinel, serviceTokenSentinel} {
			if strings.Contains(response.Body.String(), secret) || strings.Contains(logs.String(), secret) {
				t.Fatalf("authentication failure exposed secret %q", secret)
			}
		}
	}
}

func TestEnqueueHandlerBoundsWholeBodyBeforeRetainingItem(t *testing.T) {
	wq := queuepkg.NewWriteQueue(2, 1, 1, func(context.Context, []queuepkg.WriteItem) error { return nil }, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	handler := newEnqueueHandler(wq, completeServiceAuth())
	valid := `{"id":"bounded","tenant_id":"tenant-a","payload":"c2FmZQ=="}`
	oversized, err := json.Marshal(queuepkg.WriteItem{ID: "oversized", TenantID: "tenant-a", Payload: bytes.Repeat([]byte("a"), maxEnqueueBodyBytes)})
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, body string
		status     int
	}{
		{"oversized authenticated payload", string(oversized), http.StatusRequestEntityTooLarge},
		{"oversized trailing whitespace", valid + strings.Repeat(" ", maxEnqueueBodyBytes), http.StatusRequestEntityTooLarge},
		{"second JSON payload", valid + valid, http.StatusBadRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, "/enqueue", strings.NewReader(test.body))
			request.Header.Set("Authorization", "Bearer "+ingressTokenSentinel)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			if response.Code != test.status || wq.Depth() != 0 {
				t.Fatalf("status = %d, depth = %d; want %d and no retained item", response.Code, wq.Depth(), test.status)
			}
		})
	}
	boundary := valid + strings.Repeat(" ", maxEnqueueBodyBytes-len(valid))
	request := httptest.NewRequest(http.MethodPost, "/enqueue", strings.NewReader(boundary))
	request.Header.Set("Authorization", "Bearer "+ingressTokenSentinel)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusAccepted || wq.Depth() != 1 {
		t.Fatalf("exact-limit valid request status = %d, depth = %d", response.Code, wq.Depth())
	}
}

func TestQueueErrorLogExcludesServiceBearer(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":"denied"}`, http.StatusForbidden)
	}))
	defer upstream.Close()

	var logs bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logs, nil))
	wq := queuepkg.NewWriteQueue(2, 1, 1, newPipelineProcessFn(upstream.Client(), upstream.URL, completeServiceAuth()), logger)
	ctx, cancel := context.WithCancel(context.Background())
	wq.Start(ctx)
	defer func() {
		cancel()
		wq.Drain(time.Second)
	}()
	if err := wq.Enqueue(queuepkg.WriteItem{ID: "denied", TenantID: "tenant-a", Payload: []byte("safe")}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for wq.Stats().Errors == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if wq.Stats().Errors != 1 {
		t.Fatalf("errors = %d, want 1", wq.Stats().Errors)
	}
	if strings.Contains(logs.String(), serviceTokenSentinel) || strings.Contains(logs.String(), callerTokenSentinel) || strings.Contains(logs.String(), ingressTokenSentinel) {
		t.Fatal("queue error log exposed a bearer")
	}
}
