package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	provenamcp "github.com/altrixy/provena/control-plane/internal/mcp"
)

func TestBuildHandlerSecuresBothNetworkMCPTransports(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/auth/validate" || r.Header.Get("Authorization") != "Bearer valid-key" {
			http.Error(w, `{"error":"invalid"}`, http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	server := provenamcp.NewMCPServer("test", "0.0.0", upstream.URL)
	handler := buildHandler(server, true)
	for _, request := range []*http.Request{
		httptest.NewRequest(http.MethodPost, "/rpc", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)),
		httptest.NewRequest(http.MethodGet, "/sse", nil),
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusUnauthorized {
			t.Fatalf("%s %s: got %d, want 401", request.Method, request.URL.Path, recorder.Code)
		}
	}

	request := httptest.NewRequest(http.MethodPost, "/rpc", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`))
	request.Header.Set("Authorization", "Bearer valid-key")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("authenticated RPC: got %d, want 200", recorder.Code)
	}

	for _, path := range []string{"/rpc", "/sse"} {
		method := http.MethodGet
		body := strings.NewReader("")
		if path == "/rpc" {
			method = http.MethodPost
			body = strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)
		}
		request := httptest.NewRequest(method, path, body)
		request.Header.Set("Authorization", "Bearer invalid-key")
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusUnauthorized {
			t.Fatalf("invalid-token %s: got %d, want 401", path, recorder.Code)
		}
	}
}

func TestComposeAuthExceptionIsLoopbackOnly(t *testing.T) {
	raw, err := os.ReadFile("../../../docker-compose.yml")
	if err != nil {
		t.Fatalf("read docker-compose.yml: %v", err)
	}
	compose := string(raw)
	if !strings.Contains(compose, `${PROVENA_MCP_HOST_BIND:-127.0.0.1}:${PROVENA_MCP_HOST_PORT:-8090}:8090`) {
		t.Fatal("MCP Compose port must remain bound to loopback")
	}
	if strings.Contains(compose, "PROVENA_MCP_REQUIRE_AUTH=false") {
		t.Fatal("Compose must preserve authenticated MCP by default")
	}
}
