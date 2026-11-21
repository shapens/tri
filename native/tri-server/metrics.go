package main

import (
	"fmt"
	"io"
	"math"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// A small Prometheus text-format registry (standard library only).

var latencyBuckets = []float64{0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300}

type histogram struct {
	counts []uint64
	sum    float64
	count  uint64
}

func (h *histogram) observe(v float64) {
	if h.counts == nil {
		h.counts = make([]uint64, len(latencyBuckets))
	}
	for i, b := range latencyBuckets {
		if v <= b {
			h.counts[i]++
		}
	}
	h.sum += v
	h.count++
}

type Metrics struct {
	mu        sync.Mutex
	requests  map[string]uint64     // route|method|code
	durations map[string]*histogram // route
	queueWait histogram
	worker    map[string]*histogram // route: time inside the Python worker
	authFail  uint64
	errors    map[string]uint64 // diagnostic code of server-side failures

	inflight atomic.Int64
	started  time.Time
}

func NewMetrics() *Metrics {
	return &Metrics{requests: map[string]uint64{}, durations: map[string]*histogram{}, worker: map[string]*histogram{}, errors: map[string]uint64{}, started: time.Now()}
}

func (m *Metrics) Observe(route, method string, code int, elapsed time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requests[route+"|"+method+"|"+itoa(code)]++
	h := m.durations[route]
	if h == nil {
		h = &histogram{}
		m.durations[route] = h
	}
	h.observe(elapsed.Seconds())
}

func (m *Metrics) ObserveQueue(wait time.Duration) {
	m.mu.Lock()
	m.queueWait.observe(wait.Seconds())
	m.mu.Unlock()
}

func (m *Metrics) ObserveWorker(route string, ms float64) {
	m.mu.Lock()
	h := m.worker[route]
	if h == nil {
		h = &histogram{}
		m.worker[route] = h
	}
	h.observe(ms / 1000)
	m.mu.Unlock()
}

func (m *Metrics) AuthFailure() {
	m.mu.Lock()
	m.authFail++
	m.mu.Unlock()
}

func (m *Metrics) ServerError(code string) {
	m.mu.Lock()
	m.errors[code]++
	m.mu.Unlock()
}

func label(v string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`).Replace(v)
}

func fmtFloat(v float64) string {
	if math.IsInf(v, 1) {
		return "+Inf"
	}
	return fmt.Sprintf("%g", v)
}

func writeHistogram(w io.Writer, name, labels string, h *histogram) {
	sep := ""
	if labels != "" {
		sep = ","
	}
	counts := h.counts
	if counts == nil {
		counts = make([]uint64, len(latencyBuckets))
	}
	for i, b := range latencyBuckets {
		fmt.Fprintf(w, "%s_bucket{%s%sle=\"%s\"} %d\n", name, labels, sep, fmtFloat(b), counts[i])
	}
	fmt.Fprintf(w, "%s_bucket{%s%sle=\"+Inf\"} %d\n", name, labels, sep, h.count)
	if labels != "" {
		fmt.Fprintf(w, "%s_sum{%s} %g\n%s_count{%s} %d\n", name, labels, h.sum, name, labels, h.count)
	} else {
		fmt.Fprintf(w, "%s_sum %g\n%s_count %d\n", name, h.sum, name, h.count)
	}
}

func (m *Metrics) Write(w io.Writer, stats PoolStats, ready bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	fmt.Fprintln(w, "# HELP tri_http_requests_total HTTP requests by route class, method, and status.")
	fmt.Fprintln(w, "# TYPE tri_http_requests_total counter")
	keys := make([]string, 0, len(m.requests))
	for k := range m.requests {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		parts := strings.SplitN(k, "|", 3)
		fmt.Fprintf(w, "tri_http_requests_total{route=\"%s\",method=\"%s\",code=\"%s\"} %d\n", label(parts[0]), label(parts[1]), parts[2], m.requests[k])
	}
	fmt.Fprintln(w, "# HELP tri_http_request_duration_seconds End-to-end request latency in the front server.")
	fmt.Fprintln(w, "# TYPE tri_http_request_duration_seconds histogram")
	routes := make([]string, 0, len(m.durations))
	for r := range m.durations {
		routes = append(routes, r)
	}
	sort.Strings(routes)
	for _, r := range routes {
		writeHistogram(w, "tri_http_request_duration_seconds", fmt.Sprintf("route=\"%s\"", label(r)), m.durations[r])
	}
	fmt.Fprintln(w, "# HELP tri_worker_duration_seconds Time spent inside the Python worker, as the worker measured it.")
	fmt.Fprintln(w, "# TYPE tri_worker_duration_seconds histogram")
	routes = routes[:0]
	for r := range m.worker {
		routes = append(routes, r)
	}
	sort.Strings(routes)
	for _, r := range routes {
		writeHistogram(w, "tri_worker_duration_seconds", fmt.Sprintf("route=\"%s\"", label(r)), m.worker[r])
	}
	fmt.Fprintln(w, "# HELP tri_queue_wait_seconds Time a request waited for a free worker.")
	fmt.Fprintln(w, "# TYPE tri_queue_wait_seconds histogram")
	writeHistogram(w, "tri_queue_wait_seconds", "", &m.queueWait)
	fmt.Fprintln(w, "# HELP tri_http_inflight Requests being served now.")
	fmt.Fprintln(w, "# TYPE tri_http_inflight gauge")
	fmt.Fprintf(w, "tri_http_inflight %d\n", m.inflight.Load())
	fmt.Fprintln(w, "# HELP tri_auth_failures_total Requests refused for a missing or unknown bearer token.")
	fmt.Fprintln(w, "# TYPE tri_auth_failures_total counter")
	fmt.Fprintf(w, "tri_auth_failures_total %d\n", m.authFail)
	fmt.Fprintln(w, "# HELP tri_server_errors_total Requests the front server failed itself, by diagnostic code.")
	fmt.Fprintln(w, "# TYPE tri_server_errors_total counter")
	codes := make([]string, 0, len(m.errors))
	for c := range m.errors {
		codes = append(codes, c)
	}
	sort.Strings(codes)
	for _, c := range codes {
		fmt.Fprintf(w, "tri_server_errors_total{code=\"%s\"} %d\n", label(c), m.errors[c])
	}
	fmt.Fprintln(w, "# HELP tri_workers Worker processes by state.")
	fmt.Fprintln(w, "# TYPE tri_workers gauge")
	fmt.Fprintf(w, "tri_workers{state=\"idle\"} %d\ntri_workers{state=\"busy\"} %d\ntri_workers{state=\"starting\"} %d\n", stats.Idle, stats.Busy, stats.Starting)
	fmt.Fprintln(w, "# HELP tri_workers_max Upper bound of the worker pool.")
	fmt.Fprintln(w, "# TYPE tri_workers_max gauge")
	fmt.Fprintf(w, "tri_workers_max %d\n", stats.Max)
	fmt.Fprintln(w, "# HELP tri_worker_spawns_total Worker processes started.")
	fmt.Fprintln(w, "# TYPE tri_worker_spawns_total counter")
	fmt.Fprintf(w, "tri_worker_spawns_total %d\n", stats.Spawned)
	fmt.Fprintln(w, "# HELP tri_worker_restarts_total Workers started to replace one that exited or timed out.")
	fmt.Fprintln(w, "# TYPE tri_worker_restarts_total counter")
	fmt.Fprintf(w, "tri_worker_restarts_total %d\n", stats.Restarts)
	fmt.Fprintln(w, "# HELP tri_worker_crashes_total Workers that exited or were killed during a request.")
	fmt.Fprintln(w, "# TYPE tri_worker_crashes_total counter")
	fmt.Fprintf(w, "tri_worker_crashes_total %d\n", stats.Crashes)
	fmt.Fprintln(w, "# HELP tri_ready Whether the server accepts requests (1) or is starting or draining (0).")
	fmt.Fprintln(w, "# TYPE tri_ready gauge")
	r := 0
	if ready {
		r = 1
	}
	fmt.Fprintf(w, "tri_ready %d\n", r)
	fmt.Fprintln(w, "# HELP tri_uptime_seconds Seconds since the server started.")
	fmt.Fprintln(w, "# TYPE tri_uptime_seconds gauge")
	fmt.Fprintf(w, "tri_uptime_seconds %g\n", time.Since(m.started).Seconds())
}
