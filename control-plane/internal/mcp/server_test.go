package mcp

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer(upstream *httptest.Server) *MCPServer {
	return NewMCPServer("provena-test", "0.0.0", upstream.URL, "")
}

func callTool(t *testing.T, server *MCPServer, name string, args map[string]any) ToolResult {
	t.Helper()
	params := ToolCallParams{Name: name, Arguments: args}
	result, err := server.executeTool(params, "")
	if err != nil {
		t.Fatalf("executeTool(%s) error: %v", name, err)
	}
	return result
}

func TestToolsListHasNineTools(t *testing.T) {
	server := NewMCPServer("provena-test", "0.0.0", "http://example.com", "")
	resp := server.HandleToolsList(&RPCRequest{})
	raw, err := json.Marshal(resp.Result)
	if err != nil {
		t.Fatalf("marshal tools list: %v", err)
	}
	var payload struct {
		Tools []ToolDefinition `json:"tools"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("unmarshal tools list: %v", err)
	}
	if len(payload.Tools) != 9 {
		t.Fatalf("expected 9 tools, got %d", len(payload.Tools))
	}
	names := make(map[string]bool, len(payload.Tools))
	for _, tool := range payload.Tools {
		names[tool.Name] = true
	}
	for _, expected := range []string{
		"memory_create",
		"memory_search",
		"memory_get",
		"memory_delete",
		"memory_update",
		"memory_list",
		"delete_all_memories",
		"list_entities",
		"get_event_status",
	} {
		if !names[expected] {
			t.Fatalf("missing tool %s", expected)
		}
	}
}

func TestMemoryUpdateProxiesPut(t *testing.T) {
	var method, path string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		path = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"updated":true}`))
	}))
	defer upstream.Close()

	result := callTool(t, newTestServer(upstream), "memory_update", map[string]any{
		"id":      "mem_123",
		"content": "updated body",
	})
	if method != http.MethodPut || path != "/v1/memories/mem_123" {
		t.Fatalf("unexpected upstream call: %s %s", method, path)
	}
	if !strings.Contains(result.Content[0].Text, `"updated":true`) {
		t.Fatalf("unexpected tool result: %s", result.Content[0].Text)
	}
}

func TestMemoryListUsesSearch(t *testing.T) {
	var body map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memories/search" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"results":[]}`))
	}))
	defer upstream.Close()

	callTool(t, newTestServer(upstream), "memory_list", map[string]any{
		"scope": map[string]any{"tenant_id": "t1", "project_id": "p1"},
		"limit": float64(25),
	})
	if body["query"] != "*" {
		t.Fatalf("expected default query *, got %#v", body["query"])
	}
	if int(body["limit"].(float64)) != 25 {
		t.Fatalf("expected limit 25, got %#v", body["limit"])
	}
}

func TestDeleteAllMemoriesRequiresScope(t *testing.T) {
	upstream := httptest.NewServer(http.NotFoundHandler())
	defer upstream.Close()
	server := newTestServer(upstream)

	_, err := server.executeTool(ToolCallParams{
		Name: "delete_all_memories",
		Arguments: map[string]any{
			"tenant_id": "tenant-a",
		},
	}, "")
	if err == nil || !strings.Contains(err.Error(), "workspace_id, project_id, or user_id") {
		t.Fatalf("expected scoped erase validation error, got %v", err)
	}
}

func TestDeleteAllMemoriesProxiesErase(t *testing.T) {
	var body map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/admin/erase" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"deleted_memories":3}`))
	}))
	defer upstream.Close()

	callTool(t, newTestServer(upstream), "delete_all_memories", map[string]any{
		"tenant_id":  "tenant-a",
		"project_id": "proj-b",
	})
	if body["tenant_id"] != "tenant-a" || body["project_id"] != "proj-b" {
		t.Fatalf("unexpected erase payload: %#v", body)
	}
}

func TestListEntitiesUsesSnapshot(t *testing.T) {
	var path string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.String()
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"entity_summary":{"Team":2}}`))
	}))
	defer upstream.Close()

	callTool(t, newTestServer(upstream), "list_entities", map[string]any{
		"tenant_id":  "tenant-a",
		"project_id": "proj-b",
	})
	if !strings.Contains(path, "tenant_id=tenant-a") || !strings.Contains(path, "project_id=proj-b") {
		t.Fatalf("unexpected snapshot path: %s", path)
	}
}

func TestGetEventStatusUsesHistory(t *testing.T) {
	var path string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.String()
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`[{"event":"add"}]`))
	}))
	defer upstream.Close()

	callTool(t, newTestServer(upstream), "get_event_status", map[string]any{
		"memory_id": "mem_42",
		"limit":     float64(10),
	})
	if path != "/v1/memories/mem_42/history?limit=10" {
		t.Fatalf("unexpected history path: %s", path)
	}
}