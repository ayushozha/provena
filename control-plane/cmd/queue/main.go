// Command queue runs the Provena queue consumer on :8091.
// Channel-based queue with backpressure (429 when full), configurable workers,
// and graceful drain on shutdown.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
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

	// Process function: batch-writes to pipeline
	client := &http.Client{Timeout: 30 * time.Second}
	processFn := func(ctx context.Context, batch []queue.WriteItem) error {
		payload, err := json.Marshal(batch)
		if err != nil {
			return fmt.Errorf("marshal batch: %w", err)
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, pipelineURL+"/v1/batch-write", nil)
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Body = readCloser(payload)
		req.ContentLength = int64(len(payload))

		resp, err := client.Do(req)
		if err != nil {
			return fmt.Errorf("pipeline request: %w", err)
		}
		defer resp.Body.Close()
		if resp.StatusCode >= 400 {
			return fmt.Errorf("pipeline returned %d", resp.StatusCode)
		}
		return nil
	}

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
	mux.HandleFunc("POST /enqueue", func(w http.ResponseWriter, r *http.Request) {
		var item queue.WriteItem
		if err := json.NewDecoder(r.Body).Decode(&item); err != nil {
			http.Error(w, `{"error":"invalid request body"}`, http.StatusBadRequest)
			return
		}
		if item.CreatedAt.IsZero() {
			item.CreatedAt = time.Now()
		}
		if err := wq.Enqueue(item); err != nil {
			observability.QueueDepth.Set(int64(wq.Depth()))
			http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusTooManyRequests)
			return
		}
		observability.QueueDepth.Set(int64(wq.Depth()))
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		w.Write([]byte(`{"status":"accepted"}`))
	})

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

	logger.Info("queue consumer starting", "addr", listenAddr, "max_depth", maxDepth, "workers", workers)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		logger.Error("queue consumer failed", "error", err)
		os.Exit(1)
	}
}

// readCloser wraps a byte slice as an io.ReadCloser.
type readCloserImpl struct {
	data   []byte
	offset int
}

func readCloser(data []byte) *readCloserImpl {
	return &readCloserImpl{data: data}
}

func (r *readCloserImpl) Read(p []byte) (int, error) {
	if r.offset >= len(r.data) {
		return 0, io.EOF
	}
	n := copy(p, r.data[r.offset:])
	r.offset += n
	if r.offset >= len(r.data) {
		return n, io.EOF
	}
	return n, nil
}

func (r *readCloserImpl) Close() error {
	return nil
}
