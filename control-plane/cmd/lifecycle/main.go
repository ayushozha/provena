// Command lifecycle runs the Provena lifecycle governance service on :8092.
// It proxies governance APIs to the live store and periodically enforces retention.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/altrixy/provena/control-plane/internal/observability"
)

type retentionPolicy struct {
	PolicyID string `json:"policy_id"`
	TenantID string `json:"tenant_id"`
}

type retentionResponse struct {
	ExpiredMemoryIDs []string `json:"expired_memory_ids"`
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	listenAddr := env("PROVENA_LIFECYCLE_LISTEN_ADDR", ":8092")
	storeURL := env("PROVENA_STORE_URL", "http://localhost:8000")
	client := &http.Client{Timeout: 30 * time.Second}

	ctx, cancel := context.WithCancel(context.Background())

	go func() {
		ticker := time.NewTicker(60 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := enforceAllTenants(ctx, client, storeURL, logger); err != nil {
					logger.Warn("retention enforcement failed", "error", err)
				}
			}
		}
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})
	mux.Handle("POST /v1/admin/retention-policies", proxy(client, storeURL, "/v1/admin/retention-policies"))
	mux.Handle("GET /v1/admin/retention-policies", proxy(client, storeURL, "/v1/admin/retention-policies"))
	mux.Handle("POST /v1/admin/legal-hold", proxy(client, storeURL, "/v1/admin/legal-hold"))
	mux.Handle("DELETE /v1/admin/legal-hold/{hold_id}", proxy(client, storeURL, ""))
	mux.Handle("POST /v1/admin/rtbf", proxy(client, storeURL, "/v1/admin/rtbf"))
	mux.Handle("POST /v1/admin/retention/enforce", proxy(client, storeURL, "/v1/admin/retention/enforce"))

	srv := &http.Server{
		Addr:         listenAddr,
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		logger.Info("shutting down lifecycle service")
		cancel()
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer shutdownCancel()
		srv.Shutdown(shutdownCtx)
	}()

	logger.Info("lifecycle service starting", "addr", listenAddr)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		logger.Error("lifecycle service failed", "error", err)
		os.Exit(1)
	}
}

func proxy(client *http.Client, storeURL, rewritePath string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		targetPath := rewritePath
		if targetPath == "" {
			targetPath = r.URL.Path
		}
		target, err := url.Parse(storeURL + targetPath)
		if err != nil {
			http.Error(w, `{"error":"invalid upstream URL"}`, http.StatusBadGateway)
			return
		}
		target.RawQuery = r.URL.RawQuery

		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, `{"error":"failed to read request body"}`, http.StatusBadRequest)
			return
		}
		req, err := http.NewRequestWithContext(r.Context(), r.Method, target.String(), bytes.NewReader(body))
		if err != nil {
			http.Error(w, `{"error":"failed to build upstream request"}`, http.StatusBadGateway)
			return
		}
		for key, values := range r.Header {
			for _, value := range values {
				req.Header.Add(key, value)
			}
		}
		resp, err := client.Do(req)
		if err != nil {
			http.Error(w, `{"error":"upstream unavailable"}`, http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()

		for key, values := range resp.Header {
			for _, value := range values {
				w.Header().Add(key, value)
			}
		}
		w.WriteHeader(resp.StatusCode)
		io.Copy(w, resp.Body)
	})
}

func enforceAllTenants(ctx context.Context, client *http.Client, storeURL string, logger *slog.Logger) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, storeURL+"/v1/admin/retention-policies", nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil
	}

	var policies []retentionPolicy
	if err := json.NewDecoder(resp.Body).Decode(&policies); err != nil {
		return err
	}

	seen := map[string]struct{}{}
	for _, policy := range policies {
		if policy.TenantID == "" {
			continue
		}
		if _, ok := seen[policy.TenantID]; ok {
			continue
		}
		seen[policy.TenantID] = struct{}{}

		payload, _ := json.Marshal(map[string]string{"tenant_id": policy.TenantID})
		enforceReq, err := http.NewRequestWithContext(
			ctx,
			http.MethodPost,
			storeURL+"/v1/admin/retention/enforce",
			bytes.NewReader(payload),
		)
		if err != nil {
			return err
		}
		enforceReq.Header.Set("Content-Type", "application/json")
		enforceResp, err := client.Do(enforceReq)
		if err != nil {
			return err
		}

		var result retentionResponse
		if enforceResp.StatusCode < 400 {
			_ = json.NewDecoder(enforceResp.Body).Decode(&result)
			if len(result.ExpiredMemoryIDs) > 0 {
				logger.Info("retention enforcement", "tenant_id", policy.TenantID, "expired_count", len(result.ExpiredMemoryIDs))
				observability.MemoryOperationsTotal.Add(int64(len(result.ExpiredMemoryIDs)))
			}
		}
		enforceResp.Body.Close()
	}
	return nil
}
