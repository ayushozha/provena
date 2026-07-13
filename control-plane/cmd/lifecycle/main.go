// Command lifecycle runs the Provena lifecycle governance service on :8092.
// It proxies governance APIs to the live store and periodically enforces retention.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sync/atomic"
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

type lifecycleHealth struct {
	healthy atomic.Bool
}

func (health *lifecycleHealth) handler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if !health.healthy.Load() {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"status":"unhealthy"}`))
		return
	}
	_, _ = w.Write([]byte(`{"status":"ok"}`))
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
	serviceAuth, err := loadLifecycleServiceAuth(os.Getenv)
	if err != nil {
		logger.Error("invalid lifecycle service identity configuration", "error", err)
		os.Exit(1)
	}

	ctx, cancel := context.WithCancel(context.Background())
	health := &lifecycleHealth{}
	health.healthy.Store(!serviceAuth.enabled)
	if serviceAuth.enabled {
		go runRetentionLoop(ctx, client, storeURL, serviceAuth, health, logger, 60*time.Second)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", health.handler)
	mux.Handle("POST /v1/admin/retention-policies", proxy(client, storeURL, "/v1/admin/retention-policies", serviceAuth, health))
	mux.Handle("GET /v1/admin/retention-policies", proxy(client, storeURL, "/v1/admin/retention-policies", serviceAuth, health))
	mux.Handle("POST /v1/admin/legal-hold", proxy(client, storeURL, "/v1/admin/legal-hold", serviceAuth, health))
	mux.Handle("DELETE /v1/admin/legal-hold/{hold_id}", proxy(client, storeURL, "", serviceAuth, health))
	mux.Handle("POST /v1/admin/rtbf", proxy(client, storeURL, "/v1/admin/rtbf", serviceAuth, health))
	mux.Handle("POST /v1/admin/retention/enforce", proxy(client, storeURL, "/v1/admin/retention/enforce", serviceAuth, health))

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

func proxy(client *http.Client, storeURL, rewritePath string, auth lifecycleServiceAuth, health *lifecycleHealth) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !auth.authenticateIngress(r) {
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, `{"error":"valid gateway service bearer required"}`, http.StatusUnauthorized)
			return
		}
		if !auth.authorizeIngressCaller(r) {
			http.Error(w, `{"error":"lifecycle admin access for the configured tenant required"}`, http.StatusForbidden)
			return
		}
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
		for _, key := range []string{"Content-Type", "Accept", "Traceparent", "Tracestate"} {
			if value := r.Header.Get(key); value != "" {
				req.Header.Set(key, value)
			}
		}
		if !auth.enabled {
			for _, key := range []string{"X-Provena-Tenant-Id", "X-Provena-Role", "X-Provena-Key-Id", "X-Provena-Principal-Id", "X-Provena-Groups"} {
				if value := r.Header.Get(key); value != "" {
					req.Header.Set(key, value)
				}
			}
		}
		if err := auth.apply(req); err != nil {
			http.Error(w, `{"error":"lifecycle service identity unavailable"}`, http.StatusServiceUnavailable)
			return
		}
		resp, err := client.Do(req)
		if err != nil {
			http.Error(w, `{"error":"upstream unavailable"}`, http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			health.healthy.Store(false)
		}

		for key, values := range resp.Header {
			for _, value := range values {
				w.Header().Add(key, value)
			}
		}
		w.WriteHeader(resp.StatusCode)
		io.Copy(w, resp.Body)
	})
}

func runRetentionLoop(
	ctx context.Context,
	client *http.Client,
	storeURL string,
	auth lifecycleServiceAuth,
	health *lifecycleHealth,
	logger *slog.Logger,
	interval time.Duration,
) {
	for {
		err := enforceTenant(ctx, client, storeURL, auth, logger)
		health.healthy.Store(err == nil)
		delay := interval
		if err != nil {
			logger.Error("retention enforcement failed", "error", err)
			if delay > 5*time.Second {
				delay = 5 * time.Second
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
	}
}

func enforceTenant(ctx context.Context, client *http.Client, storeURL string, auth lifecycleServiceAuth, logger *slog.Logger) error {
	if err := auth.validate(); err != nil {
		return err
	}
	if !auth.enabled {
		return nil
	}
	target, err := url.Parse(storeURL + "/v1/admin/retention-policies")
	if err != nil {
		return err
	}
	query := target.Query()
	query.Set("tenant_id", auth.tenantID)
	target.RawQuery = query.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return err
	}
	if err := auth.apply(req); err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("retention policy lookup returned %d", resp.StatusCode)
	}

	var policies []retentionPolicy
	if err := json.NewDecoder(resp.Body).Decode(&policies); err != nil {
		return err
	}

	hasPolicy := false
	for _, policy := range policies {
		if policy.TenantID != auth.tenantID {
			return fmt.Errorf("retention policy lookup crossed lifecycle tenant boundary")
		}
		hasPolicy = true
	}
	if !hasPolicy {
		return nil
	}

	payload, err := json.Marshal(map[string]string{"tenant_id": auth.tenantID})
	if err != nil {
		return err
	}
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
	if err := auth.apply(enforceReq); err != nil {
		return err
	}
	enforceResp, err := client.Do(enforceReq)
	if err != nil {
		return err
	}
	defer enforceResp.Body.Close()
	if enforceResp.StatusCode >= 400 {
		return fmt.Errorf("retention enforcement returned %d", enforceResp.StatusCode)
	}

	var result retentionResponse
	_ = json.NewDecoder(enforceResp.Body).Decode(&result)
	if len(result.ExpiredMemoryIDs) > 0 {
		logger.Info("retention enforcement", "tenant_id", auth.tenantID, "expired_count", len(result.ExpiredMemoryIDs))
		observability.MemoryOperationsTotal.Add(int64(len(result.ExpiredMemoryIDs)))
	}
	return nil
}
