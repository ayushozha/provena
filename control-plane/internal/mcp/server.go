// Package mcp implements a Model Context Protocol (MCP) server for LLM clients.
// It supports JSON-RPC 2.0 over HTTP and SSE for streaming.
package mcp

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

type RPCRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

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

func (s *MCPServer) HandleToolsCall(req *RPCRequest) *RPCResponse {
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

	result, err := s.executeTool(params)
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

func (s *MCPServer) executeTool(params ToolCallParams) (ToolResult, error) {
	switch params.Name {
	case "memory_create":
		body, err := s.request(http.MethodPost, "/v1/memories", params.Arguments)
		return s.toolResult(body, err)
	case "memory_search":
		body, err := s.request(http.MethodPost, "/v1/memories/search", params.Arguments)
		return s.toolResult(body, err)
	case "memory_get":
		id, _ := params.Arguments["id"].(string)
		if id == "" {
			return ToolResult{}, fmt.Errorf("memory_get requires id")
		}
		body, err := s.request(http.MethodGet, "/v1/memories/"+id, nil)
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
		body, err := s.request(http.MethodDelete, path, nil)
		return s.toolResult(body, err)
	default:
		return ToolResult{}, fmt.Errorf("unsupported tool: %s", params.Name)
	}
}

func (s *MCPServer) request(method, path string, payload map[string]any) ([]byte, error) {
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

func (s *MCPServer) HandleRPC(req *RPCRequest) *RPCResponse {
	var resp *RPCResponse
	switch req.Method {
	case "initialize":
		resp = s.HandleInitialize(req)
	case "tools/list":
		resp = s.HandleToolsList(req)
	case "tools/call":
		resp = s.HandleToolsCall(req)
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
		var req RPCRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(RPCResponse{
				JSONRPC: "2.0",
				Error:   &RPCError{Code: -32700, Message: "parse error"},
			})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(s.HandleRPC(&req))
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
