// Command queue runs the Provena queue consumer on :8091.
// Channel-based queue with backpressure (429 when full), configurable workers,
// and graceful drain on shutdown.
package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/altrixy/provena/control-plane/internal/observability"
	"github.com/altrixy/provena/control-plane/internal/queue"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	listenAddr := env("PROVENA_QUEUE_LISTEN_ADDR", ":8091")
	maxDepth := envInt("PROVENA_QUEUE_MAX_DEPTH", 1000)
	workers := envInt("PROVENA_QUEUE_WORKERS", 4)
	pipelineURL := env("PROVENA_PIPELINE_URL", "http://localhost:8083")
	serviceAuth, err := loadQueueServiceAuth(os.Getenv)
	if err != nil {
		logger.Error("invalid queue service identity configuration", "error", err)
		os.Exit(1)
	}

	client := &http.Client{Timeout: 30 * time.Second}
	processFn := newPipelineProcessFn(client, pipelineURL, serviceAuth)

	wq := queue.NewWriteQueue(maxDepth, workers, 10, processFn, logger)

	ctx, cancel := context.WithCancel(context.Background())
	wq.Start(ctx)

	mux := http.NewServeMux()

	// Health check
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})

	// Stats endpoint
	mux.HandleFunc("GET /stats", func(w http.ResponseWriter, _ *http.Request) {
		stats := wq.Stats()
		observability.QueueDepth.Set(int64(stats.Depth))
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(stats)
	})

	// Enqueue endpoint
	mux.HandleFunc("POST /enqueue", newEnqueueHandler(wq, serviceAuth))

	srv := &http.Server{
		Addr:         listenAddr,
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	// Graceful shutdown
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		logger.Info("shutting down queue consumer")
		cancel()
		wq.Drain(10 * time.Second)
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer shutdownCancel()
		srv.Shutdown(shutdownCtx)
	}()

	logger.Info("queue consumer starting", "addr", listenAddr, "max_depth", maxDepth, "workers", workers, "service_auth_enabled", serviceAuth.enabled)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		logger.Error("queue consumer failed", "error", err)
		os.Exit(1)
	}
}
