// Package observability provides metrics collection, trace context propagation,
// and Prometheus-compatible metrics exposition using only the Go standard library.
package observability

import (
	"context"
	"crypto/rand"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ---------- Counter ----------

// Counter is a monotonically increasing atomic counter.
type Counter struct {
	name   string
	labels string
	val    atomic.Int64
}

// Inc increments the counter by 1.
func (c *Counter) Inc() { c.val.Add(1) }

// Add increments the counter by n.
func (c *Counter) Add(n int64) { c.val.Add(n) }

// Value returns the current counter value.
func (c *Counter) Value() int64 { return c.val.Load() }

// ---------- Gauge ----------

// Gauge is a value that can go up and down, stored as int64 (use fixed-point if needed).
type Gauge struct {
	name   string
	labels string
	val    atomic.Int64
}

// Set sets the gauge to v.
func (g *Gauge) Set(v int64) { g.val.Store(v) }

// Inc increments the gauge by 1.
func (g *Gauge) Inc() { g.val.Add(1) }

// Dec decrements the gauge by 1.
func (g *Gauge) Dec() { g.val.Add(-1) }

// Value returns the current gauge value.
func (g *Gauge) Value() int64 { return g.val.Load() }

// ---------- Histogram ----------

// Histogram collects observations into predefined buckets.
type Histogram struct {
	name    string
	labels  string
	mu      sync.Mutex
	buckets []float64
	counts  []uint64 // one per bucket + Inf
	sum     float64
	count   uint64
}

// NewHistogram creates a histogram with the given bucket boundaries.
func NewHistogram(name, labels string, buckets []float64) *Histogram {
	sorted := make([]float64, len(buckets))
	copy(sorted, buckets)
	sort.Float64s(sorted)
	return &Histogram{
		name:    name,
		labels:  labels,
		buckets: sorted,
		counts:  make([]uint64, len(sorted)+1), // last = +Inf
	}
}

// Observe records a value.
func (h *Histogram) Observe(v float64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.sum += v
	h.count++
	for i, b := range h.buckets {
		if v <= b {
			h.counts[i]++
		}
	}
	h.counts[len(h.buckets)]++ // +Inf always
}

// ---------- Registry ----------

// Registry holds all metrics.
type Registry struct {
	mu         sync.RWMutex
	counters   []*Counter
	gauges     []*Gauge
	histograms []*Histogram
}

var globalRegistry = &Registry{}

// RegisterCounter creates and registers a counter.
func RegisterCounter(name, labels string) *Counter {
	c := &Counter{name: name, labels: labels}
	globalRegistry.mu.Lock()
	globalRegistry.counters = append(globalRegistry.counters, c)
	globalRegistry.mu.Unlock()
	return c
}

// RegisterGauge creates and registers a gauge.
func RegisterGauge(name, labels string) *Gauge {
	g := &Gauge{name: name, labels: labels}
	globalRegistry.mu.Lock()
	globalRegistry.gauges = append(globalRegistry.gauges, g)
	globalRegistry.mu.Unlock()
	return g
}

// RegisterHistogram creates and registers a histogram.
func RegisterHistogram(name, labels string, buckets []float64) *Histogram {
	h := NewHistogram(name, labels, buckets)
	globalRegistry.mu.Lock()
	globalRegistry.histograms = append(globalRegistry.histograms, h)
	globalRegistry.mu.Unlock()
	return h
}

// ---------- Pre-defined metrics ----------

var (
	RequestsTotal         = RegisterCounter("provena_requests_total", "")
	RequestDuration       = RegisterHistogram("provena_request_duration_seconds", "", []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10})
	QueueDepth            = RegisterGauge("provena_queue_depth", "")
	MemoryOperationsTotal = RegisterCounter("provena_memory_operations_total", "")
	AuthFailuresTotal     = RegisterCounter("provena_auth_failures_total", "")
	ErrorsTotal           = RegisterCounter("provena_errors_total", "")
)

// ---------- Metrics handler ----------

func formatMetricName(name, labels string) string {
	if labels == "" {
		return name
	}
	return name + "{" + labels + "}"
}

// MetricsHandler returns an HTTP handler that renders metrics in Prometheus text format.
func MetricsHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		var b strings.Builder

		globalRegistry.mu.RLock()
		defer globalRegistry.mu.RUnlock()

		for _, c := range globalRegistry.counters {
			fmt.Fprintf(&b, "# TYPE %s counter\n", c.name)
			fmt.Fprintf(&b, "%s %d\n", formatMetricName(c.name, c.labels), c.Value())
		}
		for _, g := range globalRegistry.gauges {
			fmt.Fprintf(&b, "# TYPE %s gauge\n", g.name)
			fmt.Fprintf(&b, "%s %d\n", formatMetricName(g.name, g.labels), g.Value())
		}
		for _, h := range globalRegistry.histograms {
			h.mu.Lock()
			fmt.Fprintf(&b, "# TYPE %s histogram\n", h.name)
			cumulative := uint64(0)
			for i, bound := range h.buckets {
				cumulative += h.counts[i]
				le := fmt.Sprintf("%g", bound)
				fmt.Fprintf(&b, "%s_bucket{le=\"%s\"} %d\n", h.name, le, cumulative)
			}
			cumulative += h.counts[len(h.buckets)]
			fmt.Fprintf(&b, "%s_bucket{le=\"+Inf\"} %d\n", h.name, cumulative)
			fmt.Fprintf(&b, "%s_sum %s\n", h.name, formatFloat(h.sum))
			fmt.Fprintf(&b, "%s_count %d\n", h.name, h.count)
			h.mu.Unlock()
		}

		w.Write([]byte(b.String()))
	}
}

func formatFloat(f float64) string {
	if f == math.Trunc(f) && !math.IsInf(f, 0) && !math.IsNaN(f) {
		return fmt.Sprintf("%.1f", f)
	}
	return fmt.Sprintf("%g", f)
}

// ---------- Metrics middleware ----------

// MetricsMiddleware records request count and duration.
func MetricsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		RequestsTotal.Inc()
		start := time.Now()
		next.ServeHTTP(w, r)
		elapsed := time.Since(start).Seconds()
		RequestDuration.Observe(elapsed)
	})
}

// ---------- Trace context ----------

type traceKey string

const traceContextKey traceKey = "trace_id"

// TraceContext holds a distributed trace identifier.
type TraceContext struct {
	TraceID string
}

// NewTraceID generates a random 16-byte hex trace ID.
func NewTraceID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x", b)
}

// TraceMiddleware propagates or generates X-Trace-Id headers.
func TraceMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		traceID := r.Header.Get("X-Trace-Id")
		if traceID == "" {
			traceID = NewTraceID()
		}
		w.Header().Set("X-Trace-Id", traceID)
		ctx := context.WithValue(r.Context(), traceContextKey, &TraceContext{TraceID: traceID})
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// TraceFromContext extracts the TraceContext from context.
func TraceFromContext(ctx context.Context) string {
	tc, ok := ctx.Value(traceContextKey).(*TraceContext)
	if !ok || tc == nil {
		return ""
	}
	return tc.TraceID
}
