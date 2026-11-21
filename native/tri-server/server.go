package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// PythonMaxBody is the body limit TriAPIHandler.read_json enforces. The front
// server forwards at most this many bytes; above it, the worker sees only the
// Content-Length and answers exactly as `tri serve` does (invalid_command).
const PythonMaxBody = 10 * 1024 * 1024

type ServerConfig struct {
	QueueTimeout   time.Duration
	RequestTimeout time.Duration
	MaxBodyBytes   int64
	MetricsPublic  bool
}

type Server struct {
	cfg       ServerConfig
	pool      *Pool
	authority *TokenAuthority // nil: no token file, as `tri serve` without --auth-file
	metrics   *Metrics
	draining  atomic.Bool
	commands  map[string]bool
}

func NewServer(cfg ServerConfig, pool *Pool, authority *TokenAuthority, metrics *Metrics) *Server {
	s := &Server{cfg: cfg, pool: pool, authority: authority, metrics: metrics, commands: map[string]bool{}}
	for _, name := range pool.Hello().Commands {
		s.commands[name] = true
	}
	return s
}

func (s *Server) schemaVersion() string {
	if v := s.pool.Hello().SchemaVersion; v != "" {
		return v
	}
	return "0.1.0"
}

func (s *Server) writeJSON(w http.ResponseWriter, status int, body []byte) {
	h := w.Header()
	h.Set("Content-Type", "application/json")
	h.Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// bearerToken parses Authorization exactly as TriAPIHandler.authenticate does.
func bearerToken(r *http.Request) string {
	header := r.Header.Get("Authorization")
	if len(header) >= 7 && strings.ToLower(header[:7]) == "bearer " {
		return strings.TrimSpace(header[7:])
	}
	return ""
}

func claimedActor(r *http.Request) string {
	if actor := r.Header.Get("X-Tri-Actor"); actor != "" {
		return actor
	}
	return "actor_local"
}

func routeClass(method, path string) string {
	switch {
	case path == "/health" || path == "/v0/health":
		return "health"
	case path == "/ready" || path == "/v0/ready":
		return "ready"
	case path == "/metrics":
		return "metrics"
	case method == http.MethodPost && path == "/v0/command":
		return "command"
	case method == http.MethodPost && path == "/v0/batch":
		return "batch"
	case method == http.MethodGet && strings.HasPrefix(path, "/v0"):
		return "get"
	default:
		return "other"
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	s.metrics.inflight.Add(1)
	defer s.metrics.inflight.Add(-1)
	rec := &statusRecorder{ResponseWriter: w, status: 200}
	route := routeClass(r.Method, r.URL.Path)
	defer func() {
		s.metrics.Observe(route, r.Method, rec.status, time.Since(start))
	}()
	s.serve(rec, r, start, &route)
}

func (s *Server) serve(w http.ResponseWriter, r *http.Request, start time.Time, route *string) {
	path := r.URL.Path
	if r.Method == http.MethodGet {
		switch path {
		case "/health", "/v0/health":
			s.health(w, r, start)
			return
		case "/ready", "/v0/ready":
			s.ready(w, r, start)
			return
		case "/metrics":
			if !s.MetricsPublicOrAuthenticated(w, r, start) {
				return
			}
			w.Header().Set("Content-Type", "text/plain; version=0.0.4")
			s.metrics.Write(w, s.pool.Stats(), s.isReady())
			return
		}
	}
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		// BaseHTTPRequestHandler answers 501 for methods it has no do_<METHOD> for.
		s.writeJSON(w, http.StatusNotImplemented, ErrorEnvelope(s.schemaVersion(), "actor_local", NewDiagnostic("invalid_command", "Unsupported method.", false), start))
		return
	}
	var actor *string
	if s.authority != nil {
		resolved := s.authority.ActorFor(bearerToken(r))
		if resolved == "" {
			s.metrics.AuthFailure()
			s.writeJSON(w, http.StatusUnauthorized, ErrorEnvelope(s.schemaVersion(), "actor_local", NewDiagnostic("unauthenticated", "A valid bearer token is required.", false), start))
			return
		}
		actor = &resolved
	}
	headers := map[string]string{}
	for _, name := range []string{"Content-Type", "X-Tri-Actor"} {
		if v := r.Header.Get(name); v != "" {
			headers[name] = v
		}
	}
	var body []byte
	if r.Method == http.MethodPost && r.ContentLength >= 0 {
		// TriAPIHandler.read_json reads Content-Length bytes and refuses more than
		// 10 MiB. Forward the header; read the body only when it is within the
		// limit, never more than it claims.
		headers["Content-Length"] = strconv.FormatInt(r.ContentLength, 10)
		if r.ContentLength <= s.cfg.MaxBodyBytes && r.ContentLength <= PythonMaxBody {
			limited := io.LimitReader(r.Body, r.ContentLength)
			var err error
			body, err = io.ReadAll(limited)
			if err != nil || int64(len(body)) != r.ContentLength {
				s.writeJSON(w, http.StatusOK, ErrorEnvelope(s.schemaVersion(), s.errorActor(actor, r), NewDiagnostic("invalid_command", "Request body was shorter than its Content-Length.", false), start))
				return
			}
		} else if r.ContentLength > s.cfg.MaxBodyBytes {
			// A front-server limit below Python's: refuse before reading.
			w.Header().Set("Connection", "close")
			s.writeJSON(w, http.StatusOK, ErrorEnvelope(s.schemaVersion(), s.errorActor(actor, r), NewDiagnostic("invalid_command", "Request body too large.", false), start))
			return
		} else {
			// Over Python's limit: the worker refuses it from the header alone.
			w.Header().Set("Connection", "close")
		}
	}
	if r.Method == http.MethodPost && path == "/v0/command" && len(body) > 0 && len(body) < 1<<20 {
		*route = "command:" + s.commandLabel(body)
	}
	if s.draining.Load() {
		w.Header().Set("Connection", "close")
	}
	queued := time.Now()
	worker, err := s.pool.Acquire(s.cfg.QueueTimeout)
	s.metrics.ObserveQueue(time.Since(queued))
	if err != nil {
		code, message := "server_busy", "Every worker stayed busy for the queue timeout; retry later."
		if errors.Is(err, ErrPoolClosed) {
			message = "The server is shutting down."
		}
		s.metrics.ServerError(code)
		w.Header().Set("Retry-After", "1")
		s.writeJSON(w, http.StatusServiceUnavailable, ErrorEnvelope(s.schemaVersion(), s.errorActor(actor, r), NewDiagnostic(code, message, true), start))
		return
	}
	frame := FrameHeader{Type: "request", ID: s.pool.NextRequestID(), Method: r.Method, Path: r.URL.RequestURI(), Header: headers, Actor: actor}
	resp, respBody, err := worker.Do(frame, body, s.cfg.RequestTimeout)
	s.pool.Release(worker)
	if err != nil {
		if errors.Is(err, ErrTimeout) {
			s.metrics.ServerError("request_timeout")
			s.writeJSON(w, http.StatusGatewayTimeout, ErrorEnvelope(s.schemaVersion(), s.errorActor(actor, r), NewDiagnostic("request_timeout", "The request ran past the server's request timeout; its worker was stopped.", false), start))
			return
		}
		s.metrics.ServerError("worker_failed")
		// A GET has no effects, so it is safe to retry; a POST may have applied.
		s.writeJSON(w, http.StatusBadGateway, ErrorEnvelope(s.schemaVersion(), s.errorActor(actor, r), NewDiagnostic("worker_failed", "The worker serving this request exited; it has been replaced.", r.Method == http.MethodGet), start))
		return
	}
	s.metrics.ObserveWorker(*route, resp.WallMS)
	contentType := resp.ContentType
	if contentType == "" {
		contentType = "application/json"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(respBody)))
	w.WriteHeader(resp.Status)
	_, _ = w.Write(respBody)
}

// errorActor matches TriAPIHandler.actor_from_request() for server errors:
// the authenticated actor, else the X-Tri-Actor header, else actor_local.
func (s *Server) errorActor(actor *string, r *http.Request) string {
	if actor != nil {
		return *actor
	}
	return claimedActor(r)
}

func (s *Server) commandLabel(body []byte) string {
	var payload struct {
		Argv    []string `json:"argv"`
		Command []string `json:"command"`
	}
	if err := json.NewDecoder(bytes.NewReader(body)).Decode(&payload); err != nil {
		return "invalid"
	}
	argv := payload.Argv
	if argv == nil {
		argv = payload.Command
	}
	for len(argv) > 0 && (argv[0] == "tri" || strings.HasPrefix(argv[0], "--")) {
		if argv[0] == "--actor" || argv[0] == "--response" {
			if len(argv) < 2 {
				return "invalid"
			}
			argv = argv[2:]
			continue
		}
		argv = argv[1:]
	}
	if len(argv) == 0 {
		return "invalid"
	}
	if s.commands[argv[0]] {
		return argv[0]
	}
	return "other"
}

func (s *Server) MetricsPublicOrAuthenticated(w http.ResponseWriter, r *http.Request, start time.Time) bool {
	if s.authority == nil || s.cfg.MetricsPublic {
		return true
	}
	if s.authority.ActorFor(bearerToken(r)) == "" {
		s.metrics.AuthFailure()
		s.writeJSON(w, http.StatusUnauthorized, ErrorEnvelope(s.schemaVersion(), "actor_local", NewDiagnostic("unauthenticated", "A valid bearer token is required.", false), start))
		return false
	}
	return true
}

// health answers like TriAPIHandler: unauthenticated, home hidden when tokens
// are required. It reports this process; readiness reports the workers.
func (s *Server) health(w http.ResponseWriter, r *http.Request, start time.Time) {
	hello := s.pool.Hello()
	var home any = hello.Home
	auth := "none"
	if s.authority != nil {
		home = nil
		auth = "bearer"
	}
	data := map[string]any{"status": "ok", "home": home, "schema_version": s.schemaVersion(), "auth": auth}
	s.writeJSON(w, http.StatusOK, Envelope(s.schemaVersion(), "tri.api.health", []string{"tri", "serve", "health"}, claimedActor(r), true, data, nil, start))
}

func (s *Server) isReady() bool {
	if s.draining.Load() {
		return false
	}
	stats := s.pool.Stats()
	return stats.Live > 0
}

func (s *Server) ready(w http.ResponseWriter, r *http.Request, start time.Time) {
	stats := s.pool.Stats()
	status := "ready"
	switch {
	case s.draining.Load():
		status = "draining"
	case stats.Live == 0 && stats.Starting > 0:
		status = "starting"
	case stats.Live == 0:
		status = "unavailable"
	}
	ready := status == "ready"
	data := map[string]any{
		"status": status,
		"ready":  ready,
		"workers": map[string]any{
			"live": stats.Live, "idle": stats.Idle, "busy": stats.Busy, "starting": stats.Starting,
			"max": stats.Max, "restarts": stats.Restarts,
		},
	}
	code := http.StatusOK
	if !ready {
		code = http.StatusServiceUnavailable
	}
	s.writeJSON(w, code, Envelope(s.schemaVersion(), "tri.api.ready", []string{"tri", "serve", "ready"}, claimedActor(r), true, data, nil, start))
}

// Maintain asks one idle worker to reap leases and trim caches, what the
// reaper thread in `tri serve` does every five minutes.
func (s *Server) Maintain() {
	worker := s.pool.TryAcquire()
	if worker == nil {
		return
	}
	defer s.pool.Release(worker)
	frame := FrameHeader{Type: "maintain", ID: s.pool.NextRequestID()}
	_, _, _ = worker.Do(frame, nil, s.cfg.RequestTimeout)
}
