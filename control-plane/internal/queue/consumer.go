// Package queue provides an in-process channel-based write queue with
// backpressure, configurable workers, and graceful drain on shutdown.
package queue

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"
)

// WriteItem represents a unit of work to be written to the pipeline.
type WriteItem struct {
	ID        string    `json:"id"`
	TenantID  string    `json:"tenant_id"`
	Payload   []byte    `json:"payload"`
	CreatedAt time.Time `json:"created_at"`
}

// QueueStats holds runtime statistics about the write queue.
type QueueStats struct {
	Depth     int   `json:"depth"`
	MaxDepth  int   `json:"max_depth"`
	Processed int64 `json:"processed"`
	Rejected  int64 `json:"rejected"`
	Errors    int64 `json:"errors"`
}

// ProcessFunc is the callback invoked for each batch of write items.
type ProcessFunc func(ctx context.Context, batch []WriteItem) error

// WriteQueue is a channel-based work queue with configurable concurrency.
type WriteQueue struct {
	ch        chan WriteItem
	maxDepth  int
	workers   int
	processFn ProcessFunc
	batchSize int

	processed atomic.Int64
	rejected  atomic.Int64
	errors    atomic.Int64

	wg     sync.WaitGroup
	cancel context.CancelFunc
	logger *slog.Logger
}

// NewWriteQueue creates a new write queue.
func NewWriteQueue(maxDepth, workers, batchSize int, fn ProcessFunc, logger *slog.Logger) *WriteQueue {
	if batchSize <= 0 {
		batchSize = 10
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &WriteQueue{
		ch:        make(chan WriteItem, maxDepth),
		maxDepth:  maxDepth,
		workers:   workers,
		processFn: fn,
		batchSize: batchSize,
		logger:    logger,
	}
}

// Enqueue adds an item to the queue. Returns an error if the queue is full.
func (q *WriteQueue) Enqueue(item WriteItem) error {
	select {
	case q.ch <- item:
		return nil
	default:
		q.rejected.Add(1)
		return fmt.Errorf("queue full (depth=%d)", q.maxDepth)
	}
}

// Start spawns worker goroutines that consume from the queue.
func (q *WriteQueue) Start(ctx context.Context) {
	ctx, q.cancel = context.WithCancel(ctx)
	for i := 0; i < q.workers; i++ {
		q.wg.Add(1)
		go q.worker(ctx, i)
	}
	q.logger.Info("queue started", "workers", q.workers, "max_depth", q.maxDepth)
}

func (q *WriteQueue) worker(ctx context.Context, id int) {
	defer q.wg.Done()
	batch := make([]WriteItem, 0, q.batchSize)
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()

	flush := func() {
		if len(batch) == 0 {
			return
		}
		if err := q.processFn(ctx, batch); err != nil {
			q.errors.Add(int64(len(batch)))
			q.logger.Error("batch processing failed", "worker", id, "batch_size", len(batch), "error", err)
		} else {
			q.processed.Add(int64(len(batch)))
		}
		batch = batch[:0]
	}

	for {
		select {
		case item, ok := <-q.ch:
			if !ok {
				flush()
				return
			}
			batch = append(batch, item)
			if len(batch) >= q.batchSize {
				flush()
			}
		case <-ticker.C:
			flush()
		case <-ctx.Done():
			// Drain remaining items in channel
			for {
				select {
				case item, ok := <-q.ch:
					if !ok {
						flush()
						return
					}
					batch = append(batch, item)
					if len(batch) >= q.batchSize {
						flush()
					}
				default:
					flush()
					return
				}
			}
		}
	}
}

// Drain performs a graceful shutdown: signals workers to stop, waits up to the timeout.
func (q *WriteQueue) Drain(timeout time.Duration) {
	q.logger.Info("draining queue", "remaining", len(q.ch))
	if q.cancel != nil {
		q.cancel()
	}

	done := make(chan struct{})
	go func() {
		q.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		q.logger.Info("queue drained successfully")
	case <-time.After(timeout):
		q.logger.Warn("queue drain timed out", "timeout", timeout)
	}
}

// Stats returns current queue statistics.
func (q *WriteQueue) Stats() QueueStats {
	return QueueStats{
		Depth:     len(q.ch),
		MaxDepth:  q.maxDepth,
		Processed: q.processed.Load(),
		Rejected:  q.rejected.Load(),
		Errors:    q.errors.Load(),
	}
}

// Depth returns the current number of items in the queue.
func (q *WriteQueue) Depth() int {
	return len(q.ch)
}
