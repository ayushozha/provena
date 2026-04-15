package main

import (
	"fmt"
	"os"

	provena "github.com/ayushozha/portfolio-generator/services/provena/sdk/go"
)

func main() {
	baseURL := os.Getenv("PROVENA_BASE_URL")
	if baseURL == "" {
		panic("PROVENA_BASE_URL is required")
	}

	client := provena.NewClient(baseURL)
	health, err := client.Health()
	if err != nil {
		panic(err)
	}
	if health["status"] != "ok" {
		panic("health check failed")
	}

	created, err := client.CreateMemory(map[string]any{
		"kind": "artifact",
		"scope": map[string]any{
			"tenant_id":    "tenant-e2e",
			"workspace_id": "ws-e2e",
			"user_id":      "go-sdk",
		},
		"title":       "Go SDK smoke memory",
		"content":     "Go SDK can write and search Provena memories.",
		"tags":        []string{"sdk", "go"},
		"entity_keys": []string{"smoke"},
	})
	if err != nil {
		panic(err)
	}

	results, err := client.SearchMemories(map[string]any{
		"query": "Go SDK smoke",
		"scope": map[string]any{
			"tenant_id":    "tenant-e2e",
			"workspace_id": "ws-e2e",
			"user_id":      "go-sdk",
		},
		"limit": 3,
	})
	if err != nil {
		panic(err)
	}

	resultList, ok := results["results"].([]any)
	if !ok {
		panic("unexpected search response")
	}
	createdMemoryID := created["memory"].(map[string]any)["memory_id"]
	found := false
	for _, item := range resultList {
		entry := item.(map[string]any)
		if entry["memory"].(map[string]any)["memory_id"] == createdMemoryID {
			found = true
			break
		}
	}
	if !found {
		panic("search did not return created memory")
	}

	fmt.Println("go-sdk-smoke: ok")
}
