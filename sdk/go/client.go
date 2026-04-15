package provena

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	baseURL    string
	httpClient *http.Client
	headers    map[string]string
}

func NewClient(baseURL string) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
		},
		headers: map[string]string{},
	}
}

func NewClientWithAPIKey(baseURL, apiKey string) *Client {
	client := NewClient(baseURL)
	client.SetAPIKey(apiKey)
	return client
}

func (c *Client) SetAPIKey(apiKey string) {
	if apiKey == "" {
		delete(c.headers, "Authorization")
		return
	}
	c.headers["Authorization"] = "Bearer " + apiKey
}

func (c *Client) SetHeader(key, value string) {
	if value == "" {
		delete(c.headers, key)
		return
	}
	c.headers[key] = value
}

func (c *Client) request(method string, path string, payload any, out any) error {
	var body io.Reader
	if payload != nil {
		raw, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		body = bytes.NewReader(raw)
	}

	req, err := http.NewRequest(method, c.baseURL+path, body)
	if err != nil {
		return err
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for key, value := range c.headers {
		req.Header.Set(key, value)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		data, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("provena request failed (%d): %s", resp.StatusCode, string(data))
	}
	if resp.StatusCode == http.StatusNoContent || out == nil {
		return nil
	}

	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) Health() (map[string]any, error) {
	var out map[string]any
	err := c.request(http.MethodGet, "/healthz", nil, &out)
	return out, err
}

func (c *Client) CreateMemory(payload map[string]any) (map[string]any, error) {
	var out map[string]any
	err := c.request(http.MethodPost, "/v1/memories", payload, &out)
	return out, err
}

func (c *Client) SearchMemories(payload map[string]any) (map[string]any, error) {
	var out map[string]any
	err := c.request(http.MethodPost, "/v1/memories/search", payload, &out)
	return out, err
}
