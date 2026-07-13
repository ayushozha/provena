// Package mcp implements a Model Context Protocol (MCP) server for LLM clients.
// It supports JSON-RPC 2.0 over HTTP and SSE for streaming.
package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

type RPCRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

const maxRPCRequestBytes int64 = 100 * 1024

type RPCResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  any             `json:"result,omitempty"`
	Error   *RPCError       `json:"error,omitempty"`
}

type RPCError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

type ToolDefinition struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

type ToolCallParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments,omitempty"`
}

type ToolResult struct {
	Content []ToolContent `json:"content"`
	IsError bool          `json:"isError,omitempty"`
}

type ToolContent struct {
	Type string `json:"type"`
	Text string `json:"text,omitempty"`
}

type MCPServer struct {
	name        string
	version     string
	upstreamURL string
	client      *http.Client
	tools       []ToolDefinition
}

func NewMCPServer(name, version, upstreamURL string) *MCPServer {
	server := &MCPServer{
		name:        name,
		version:     version,
		upstreamURL: upstreamURL,
		client:      &http.Client{Timeout: 30 * time.Second},
	}
	server.tools = []ToolDefinition{
		{
			Name:        "memory_create",
			Description: "Create a new memory with content, metadata, and optional relations.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"kind":    map[string]any{"type": "string"},
					"scope":   map[string]any{"type": "object"},
					"content": map[string]any{"type": "string"},
				},
				"required": []string{"content", "scope"},
			},
		},
		{
			Name:        "memory_search",
			Description: "Search memories by semantic query with optional filters.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"query": map[string]any{"type": "string"},
					"scope": map[string]any{"type": "object"},
					"limit": map[string]any{"type": "integer"},
				},
				"required": []string{"query", "scope"},
			},
		},
		{
			Name:        "memory_get",
			Description: "Retrieve a specific memory by its ID.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"id": map[string]any{"type": "string"},
				},
				"required": []string{"id"},
			},
		},
		{
			Name:        "memory_delete",
			Description: "Delete a memory by its ID.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"id":          map[string]any{"type": "string"},
					"hard_delete": map[string]any{"type": "boolean"},
				},
				"required": []string{"id"},
			},
		},
		{
			Name:        "memory_update",
			Description: "Update an existing memory by ID.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"id":      map[string]any{"type": "string"},
					"content": map[string]any{"type": "string"},
					"summary": map[string]any{"type": "string"},
					"tags":    map[string]any{"type": "array"},
					"scope":   map[string]any{"type": "object"},
					"kind":    map[string]any{"type": "string"},
				},
				"required": []string{"id"},
			},
		},
		{
			Name:        "memory_list",
			Description: "List memories for a scope via semantic search.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"query": map[string]any{"type": "string"},
					"scope": map[string]any{"type": "object"},
					"limit": map[string]any{"type": "integer"},
				},
				"required": []string{"scope", "query"},
			},
		},
		{
			Name:        "delete_all_memories",
			Description: "Delete all memories within an explicit scope (tenant plus project, workspace, or user). Never wipes globally.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"tenant_id":    map[string]any{"type": "string"},
					"workspace_id": map[string]any{"type": "string"},
					"project_id":   map[string]any{"type": "string"},
					"user_id":      map[string]any{"type": "string"},
				},
				"required": []string{"tenant_id"},
				"anyOf": []map[string]any{
					{"required": []string{"project_id"}},
					{"required": []string{"workspace_id"}},
					{"required": []string{"user_id"}},
				},
			},
		},
		{
			Name:        "list_entities",
			Description: "List known entities for a tenant/project from the latest project snapshot.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"tenant_id":  map[string]any{"type": "string"},
					"project_id": map[string]any{"type": "string"},
				},
				"required": []string{"tenant_id", "project_id"},
			},
		},
		{
			Name:        "get_event_status",
			Description: "Get memory history events (pipeline audit trail) for a memory ID.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"memory_id": map[string]any{"type": "string"},
					"limit":     map[string]any{"type": "integer"},
				},
				"required": []string{"memory_id"},
			},
		},
	}
	return server
}

func (s *MCPServer) HandleInitialize(_ *RPCRequest) *RPCResponse {
	return &RPCResponse{
		JSONRPC: "2.0",
		Result: map[string]any{
			"protocolVersion": "2024-11-05",
			"capabilities":    map[string]any{"tools": map[string]any{}},
			"serverInfo": map[string]any{
				"name":    s.name,
				"version": s.version,
			},
		},
	}
}

func (s *MCPServer) HandleToolsList(_ *RPCRequest) *RPCResponse {
	return &RPCResponse{
		JSONRPC: "2.0",
		Result:  map[string]any{"tools": s.tools},
	}
}

func (s *MCPServer) HandleToolsCall(req *RPCRequest, authHeader string) *RPCResponse {
	var params ToolCallParams
	if req.Params != nil {
		if err := json.Unmarshal(req.Params, &params); err != nil {
			return s.errorResponse(req.ID, -32602, "invalid params")
		}
	}

	allowed := false
	for _, tool := range s.tools {
		if tool.Name == params.Name {
			allowed = true
			break
		}
	}
	if !allowed {
		return s.errorResponse(req.ID, -32601, fmt.Sprintf("tool not found: %s", params.Name))
	}

	result, err := s.executeTool(params, authHeader)
	if err != nil {
		return &RPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Result: ToolResult{
				Content: []ToolContent{{Type: "text", Text: err.Error()}},
				IsError: true,
			},
		}
	}

	return &RPCResponse{
		JSONRPC: "2.0",
		ID:      req.ID,
		Result:  result,
	}
}

func (s *MCPServer) executeTool(params ToolCallParams, authHeader string) (ToolResult, error) {
	switch params.Name {
	case "memory_create":
		body, err := s.request(http.MethodPost, "/v1/memories", params.Arguments, authHeader)
		return s.toolResult(body, err)
	case "memory_search":
		body, err := s.request(http.MethodPost, "/v1/memories/search", params.Arguments, authHeader)
		return s.toolResult(body, err)
	case "memory_get":
		id, _ := params.Arguments["id"].(string)
		if id == "" {
			return ToolResult{}, fmt.Errorf("memory_get requires id")
		}
		body, err := s.request(http.MethodGet, "/v1/memories/"+id, nil, authHeader)
		return s.toolResult(body, err)
	case "memory_delete":
		id, _ := params.Arguments["id"].(string)
		if id == "" {
			return ToolResult{}, fmt.Errorf("memory_delete requires id")
		}
		path := "/v1/memories/" + id
		if hardDelete, ok := params.Arguments["hard_delete"].(bool); ok && hardDelete {
			path += "?hard_delete=true"
		}
		body, err := s.request(http.MethodDelete, path, nil, authHeader)
		return s.toolResult(body, err)
	case "memory_update":
		id, _ := params.Arguments["id"].(string)
		if id == "" {
			return ToolResult{}, fmt.Errorf("memory_update requires id")
		}
		payload := cloneArgsWithout(params.Arguments, "id")
		body, err := s.request(http.MethodPut, "/v1/memories/"+id, payload, authHeader)
		return s.toolResult(body, err)
	case "memory_list":
		query, _ := params.Arguments["query"].(string)
		if query == "" {
			return ToolResult{}, fmt.Errorf("memory_list requires query")
		}
		limit := 50
		if raw, ok := params.Arguments["limit"].(float64); ok && raw > 0 {
			limit = int(raw)
		}
		scope, _ := params.Arguments["scope"].(map[string]any)
		if scope == nil {
			return ToolResult{}, fmt.Errorf("memory_list requires scope")
		}
		body, err := s.request(http.MethodPost, "/v1/memories/search", map[string]any{
			"query": query,
			"scope": scope,
			"limit": limit,
		}, authHeader)
		return s.toolResult(body, err)
	case "delete_all_memories":
		payload, err := erasePayloadFromArgs(params.Arguments)
		if err != nil {
			return ToolResult{}, err
		}
		body, err := s.request(http.MethodPost, "/v1/admin/erase", payload, authHeader)
		return s.toolResult(body, err)
	case "list_entities":
		tenantID, _ := params.Arguments["tenant_id"].(string)
		projectID, _ := params.Arguments["project_id"].(string)
		if tenantID == "" || projectID == "" {
			return ToolResult{}, fmt.Errorf("list_entities requires tenant_id and project_id")
		}
		path := fmt.Sprintf(
			"/v1/project-snapshots/latest?tenant_id=%s&project_id=%s",
			url.QueryEscape(tenantID),
			url.QueryEscape(projectID),
		)
		body, err := s.request(http.MethodGet, path, nil, authHeader)
		return s.toolResult(body, err)
	case "get_event_status":
		memoryID, _ := params.Arguments["memory_id"].(string)
		if memoryID == "" {
			return ToolResult{}, fmt.Errorf("get_event_status requires memory_id")
		}
		limit := 50
		if raw, ok := params.Arguments["limit"].(float64); ok && raw > 0 {
			limit = int(raw)
		}
		path := fmt.Sprintf("/v1/memories/%s/history?limit=%d", url.PathEscape(memoryID), limit)
		body, err := s.request(http.MethodGet, path, nil, authHeader)
		return s.toolResult(body, err)
	default:
		return ToolResult{}, fmt.Errorf("unsupported tool: %s", params.Name)
	}
}

func cloneArgsWithout(args map[string]any, skip string) map[string]any {
	out := make(map[string]any, len(args))
	for key, value := range args {
		if key == skip {
			continue
		}
		out[key] = value
	}
	return out
}

func erasePayloadFromArgs(args map[string]any) (map[string]any, error) {
	tenantID, _ := args["tenant_id"].(string)
	if tenantID == "" {
		return nil, fmt.Errorf("delete_all_memories requires tenant_id")
	}
	workspaceID, _ := args["workspace_id"].(string)
	projectID, _ := args["project_id"].(string)
	userID, _ := args["user_id"].(string)
	if workspaceID == "" && projectID == "" && userID == "" {
		return nil, fmt.Errorf("delete_all_memories requires workspace_id, project_id, or user_id")
	}
	payload := map[string]any{"tenant_id": tenantID}
	if workspaceID != "" {
		payload["workspace_id"] = workspaceID
	}
	if projectID != "" {
		payload["project_id"] = projectID
	}
	if userID != "" {
		payload["user_id"] = userID
	}
	return payload, nil
}

func (s *MCPServer) request(method, path string, payload map[string]any, authHeader string) ([]byte, error) {
	var body io.Reader
	if payload != nil {
		raw, err := json.Marshal(payload)
		if err != nil {
			return nil, fmt.Errorf("marshal request: %w", err)
		}
		body = bytes.NewReader(raw)
	}

	req, err := http.NewRequest(method, s.upstreamURL+path, body)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if authHeader != "" {
		req.Header.Set("Authorization", authHeader)
	}

	resp, err := s.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("upstream request failed: %w", err)
	}
	defer resp.Body.Close()

	data, readErr := io.ReadAll(resp.Body)
	if readErr != nil {
		return nil, fmt.Errorf("read upstream response: %w", readErr)
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("upstream returned %d: %s", resp.StatusCode, string(data))
	}
	if len(data) == 0 {
		data = []byte(`{"status":"ok"}`)
	}
	return data, nil
}

// RequireBearerAuth rejects anonymous network MCP requests. The gateway still
// performs the authoritative API-key validation; this boundary prevents the
// MCP process from acting as an unauthenticated credential bridge.
func RequireBearerAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Fields(r.Header.Get("Authorization"))
		if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] == "" {
			http.Error(w, `{"error":"missing or invalid Authorization header"}`, http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// RequireGatewayBearerAuth verifies a syntactically valid bearer token at the
// authoritative gateway before admitting any network MCP request. This covers
// initialize, tools/list, and SSE requests that do not otherwise call upstream.
func (s *MCPServer) RequireGatewayBearerAuth(next http.Handler) http.Handler {
	return RequireBearerAuth(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		req, err := http.NewRequestWithContext(
			ctx,
			http.MethodGet,
			strings.TrimRight(s.upstreamURL, "/")+"/v1/auth/validate",
			nil,
		)
		if err == nil {
			req.Header.Set("Authorization", r.Header.Get("Authorization"))
		}
		var resp *http.Response
		if err == nil {
			resp, err = s.client.Do(req)
		}
		if err != nil {
			http.Error(w, `{"error":"authentication service unavailable"}`, http.StatusServiceUnavailable)
			return
		}
		defer resp.Body.Close()
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			http.Error(w, `{"error":"invalid or expired API key"}`, http.StatusUnauthorized)
			return
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			http.Error(w, `{"error":"authentication service unavailable"}`, http.StatusServiceUnavailable)
			return
		}
		next.ServeHTTP(w, r)
	}))
}

// LimitConcurrentConnections bounds long-lived transports such as SSE.
func LimitConcurrentConnections(next http.Handler, maximum int) http.Handler {
	if maximum < 1 {
		panic("maximum concurrent connections must be positive")
	}
	semaphore := make(chan struct{}, maximum)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case semaphore <- struct{}{}:
			defer func() { <-semaphore }()
			next.ServeHTTP(w, r)
		default:
			http.Error(w, `{"error":"too many active connections"}`, http.StatusTooManyRequests)
		}
	})
}

func (s *MCPServer) toolResult(body []byte, err error) (ToolResult, error) {
	if err != nil {
		return ToolResult{}, err
	}
	return ToolResult{
		Content: []ToolContent{{Type: "text", Text: string(body)}},
	}, nil
}

func (s *MCPServer) errorResponse(id json.RawMessage, code int, message string) *RPCResponse {
	return &RPCResponse{
		JSONRPC: "2.0",
		ID:      id,
		Error:   &RPCError{Code: code, Message: message},
	}
}

func (s *MCPServer) HandleRPC(req *RPCRequest, authHeader string) *RPCResponse {
	var resp *RPCResponse
	switch req.Method {
	case "initialize":
		resp = s.HandleInitialize(req)
	case "tools/list":
		resp = s.HandleToolsList(req)
	case "tools/call":
		resp = s.HandleToolsCall(req, authHeader)
	default:
		resp = s.errorResponse(req.ID, -32601, fmt.Sprintf("method not found: %s", req.Method))
	}
	if req.ID != nil {
		resp.ID = req.ID
	}
	return resp
}

func (s *MCPServer) RPCHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		r.Body = http.MaxBytesReader(w, r.Body, maxRPCRequestBytes)
		decoder := json.NewDecoder(r.Body)
		var req RPCRequest
		if err := decoder.Decode(&req); err != nil {
			status := http.StatusBadRequest
			var tooLarge *http.MaxBytesError
			if errors.As(err, &tooLarge) {
				status = http.StatusRequestEntityTooLarge
			}
			w.WriteHeader(status)
			json.NewEncoder(w).Encode(RPCResponse{
				JSONRPC: "2.0",
				Error:   &RPCError{Code: -32700, Message: "parse error"},
			})
			return
		}
		var trailing json.RawMessage
		if err := decoder.Decode(&trailing); err != io.EOF {
			status := http.StatusBadRequest
			var tooLarge *http.MaxBytesError
			if errors.As(err, &tooLarge) {
				status = http.StatusRequestEntityTooLarge
			}
			w.WriteHeader(status)
			json.NewEncoder(w).Encode(RPCResponse{
				JSONRPC: "2.0",
				Error:   &RPCError{Code: -32700, Message: "parse error"},
			})
			return
		}
		json.NewEncoder(w).Encode(s.HandleRPC(&req, r.Header.Get("Authorization")))
	}
}

type SSEWriter struct {
	mu      sync.Mutex
	w       http.ResponseWriter
	flusher http.Flusher
}

func NewSSEWriter(w http.ResponseWriter) (*SSEWriter, error) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		return nil, fmt.Errorf("response writer does not support flushing")
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	return &SSEWriter{w: w, flusher: flusher}, nil
}

func (s *SSEWriter) Send(event string, data any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, err := json.Marshal(data)
	if err != nil {
		return err
	}
	fmt.Fprintf(s.w, "event: %s\ndata: %s\n\n", event, string(raw))
	s.flusher.Flush()
	return nil
}

func (s *MCPServer) SSEHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		sse, err := NewSSEWriter(w)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}

		_ = sse.Send("endpoint", map[string]string{"url": "/rpc"})

		ctx := r.Context()
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := sse.Send("heartbeat", map[string]int64{"ts": time.Now().Unix()}); err != nil {
					return
				}
			}
		}
	}
}
