// Command tri-server is the native front server for Tri's JSON HTTP API.
//
// It terminates HTTP, verifies bearer tokens against the token file written by
// `tri auth issue`, and dispatches every API request to a pool of persistent
// Python workers (`tri worker --stdio`). Routing, policy, actor pinning, the
// server-path refusal, and API-blocked commands stay in Python, in the same
// TriAPIHandler code `tri serve` runs. What moves here: connection handling,
// concurrency across processes, timeouts, request-size limits, graceful drain,
// health, readiness, and Prometheus metrics.
//
//	tri-server [flags] [-- worker command...]
//
// The worker command defaults to $TRI_WORKER_CMD (split on spaces), then
// `tri worker --stdio` if tri is on PATH, then `python3 -m tri worker --stdio`.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type options struct {
	host, port           string
	quiet                bool
	authFile             string
	allowUnauthenticated bool
	runSandbox           string
	allowUnsandboxedRuns bool
	runExecutor          string
	workers, maxWorkers  int
	queueTimeout         time.Duration
	requestTimeout       time.Duration
	drainTimeout         time.Duration
	workerStartTimeout   time.Duration
	workerIdleTimeout    time.Duration
	workerMaxRequests    int
	maintainInterval     time.Duration
	maxBodyBytes         int64
	maxHeaderBytes       int
	readHeaderTimeout    time.Duration
	idleTimeout          time.Duration
	metricsPublic        bool
	workerCommand        []string
}

func defaultWorkers() int {
	n := runtime.NumCPU()
	if n > 8 {
		n = 8
	}
	if n < 2 {
		n = 2
	}
	return n
}

func parseOptions(args []string) (*options, error) {
	o := &options{}
	fs := flag.NewFlagSet("tri-server", flag.ContinueOnError)
	fs.StringVar(&o.host, "host", "127.0.0.1", "address to bind (as tri serve)")
	port := fs.Int("port", 8765, "port to bind; 0 picks a free port")
	fs.BoolVar(&o.quiet, "quiet", false, "do not print the startup envelope")
	fs.StringVar(&o.authFile, "auth-file", os.Getenv("TRI_SERVE_AUTH_FILE"), "token file from `tri auth issue`; every request needs a bearer token")
	fs.BoolVar(&o.allowUnauthenticated, "allow-unauthenticated", false, "serve beyond loopback without tokens (as tri serve)")
	fs.StringVar(&o.runSandbox, "run-sandbox", "", "off|auto|required (as tri serve)")
	fs.BoolVar(&o.allowUnsandboxedRuns, "allow-unsandboxed-runs", false, "as tri serve")
	fs.StringVar(&o.runExecutor, "run-executor", "", "local|remote (as tri serve)")
	fs.IntVar(&o.workers, "workers", defaultWorkers(), "worker processes kept running")
	fs.IntVar(&o.maxWorkers, "max-workers", 0, "upper bound when every worker is busy (default 2x --workers)")
	fs.DurationVar(&o.queueTimeout, "queue-timeout", 30*time.Second, "how long a request waits for a free worker before 503 server_busy")
	fs.DurationVar(&o.requestTimeout, "request-timeout", 15*time.Minute, "how long a worker may take; past it the worker is killed and the request gets 504 request_timeout")
	fs.DurationVar(&o.drainTimeout, "drain-timeout", 30*time.Second, "on SIGTERM/SIGINT, how long in-flight requests may finish")
	fs.DurationVar(&o.workerStartTimeout, "worker-start-timeout", 60*time.Second, "how long a worker may take to start")
	fs.DurationVar(&o.workerIdleTimeout, "worker-idle-timeout", 60*time.Second, "retire workers above --workers after this long idle")
	fs.IntVar(&o.workerMaxRequests, "worker-max-requests", 0, "recycle a worker after this many requests (0: never)")
	fs.DurationVar(&o.maintainInterval, "maintain-interval", 5*time.Minute, "how often an idle worker reaps leases and trims caches (0: never)")
	fs.Int64Var(&o.maxBodyBytes, "max-body-bytes", PythonMaxBody, "largest request body read; Python refuses more than 10 MiB regardless")
	fs.IntVar(&o.maxHeaderBytes, "max-header-bytes", 1<<20, "largest request header block")
	fs.DurationVar(&o.readHeaderTimeout, "read-header-timeout", 10*time.Second, "time allowed to read request headers")
	fs.DurationVar(&o.idleTimeout, "idle-timeout", 120*time.Second, "keep-alive idle timeout")
	fs.BoolVar(&o.metricsPublic, "metrics-public", false, "serve /metrics without a bearer token when --auth-file is set")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	o.port = strconv.Itoa(*port)
	o.workerCommand = fs.Args()
	if o.runSandbox != "" && o.runSandbox != "off" && o.runSandbox != "auto" && o.runSandbox != "required" {
		return nil, fmt.Errorf("--run-sandbox must be off, auto, or required")
	}
	if o.runExecutor != "" && o.runExecutor != "local" && o.runExecutor != "remote" {
		return nil, fmt.Errorf("--run-executor must be local or remote")
	}
	if o.workers < 1 {
		return nil, fmt.Errorf("--workers must be at least 1")
	}
	if o.maxWorkers == 0 {
		o.maxWorkers = 2 * o.workers
	}
	if o.maxWorkers < o.workers {
		o.maxWorkers = o.workers
	}
	if len(o.workerCommand) == 0 {
		o.workerCommand = defaultWorkerCommand()
	}
	return o, nil
}

func defaultWorkerCommand() []string {
	if cmd := strings.Fields(os.Getenv("TRI_WORKER_CMD")); len(cmd) > 0 {
		return cmd
	}
	if _, err := exec.LookPath("tri"); err == nil {
		return []string{"tri", "worker", "--stdio"}
	}
	python := "python3"
	if runtime.GOOS == "windows" {
		python = "python"
	}
	return []string{python, "-m", "tri", "worker", "--stdio"}
}

// workerArgs passes the policy flags through, so `tri worker` applies the same
// refusals and run settings as `tri serve` would with these flags.
func (o *options) workerArgs(port string) []string {
	args := append([]string{}, o.workerCommand...)
	args = append(args, "--host", o.host, "--port", port)
	if o.authFile != "" {
		args = append(args, "--auth-file", o.authFile)
	}
	if o.allowUnauthenticated {
		args = append(args, "--allow-unauthenticated")
	}
	if o.runSandbox != "" {
		args = append(args, "--run-sandbox", o.runSandbox)
	}
	if o.allowUnsandboxedRuns {
		args = append(args, "--allow-unsandboxed-runs")
	}
	if o.runExecutor != "" {
		args = append(args, "--run-executor", o.runExecutor)
	}
	return args
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	o, err := parseOptions(args)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		fmt.Fprintln(os.Stderr, "tri-server:", err)
		return 2
	}
	if o.authFile != "" {
		if abs, err := filepath.Abs(o.authFile); err == nil {
			o.authFile = abs
		}
	}
	listener, err := net.Listen("tcp", net.JoinHostPort(o.host, o.port))
	if err != nil {
		fmt.Fprintln(os.Stderr, "tri-server:", err)
		return 1
	}
	port := strconv.Itoa(listener.Addr().(*net.TCPAddr).Port)

	pool := NewPool(PoolConfig{
		Command:      o.workerArgs(port),
		Env:          os.Environ(),
		Stderr:       os.Stderr,
		Min:          o.workers,
		Max:          o.maxWorkers,
		StartTimeout: o.workerStartTimeout,
		IdleTimeout:  o.workerIdleTimeout,
		MaxRequests:  o.workerMaxRequests,
	})
	if err := pool.Start(); err != nil {
		listener.Close()
		var fatal *FatalError
		if errors.As(err, &fatal) {
			// The policy refusal tri serve would print, from the same Python code.
			os.Stdout.Write(fatal.Envelope)
			return 1
		}
		fmt.Fprintln(os.Stderr, "tri-server:", err)
		return 1
	}
	var authority *TokenAuthority
	if o.authFile != "" {
		authority = NewTokenAuthority(o.authFile)
	}
	metrics := NewMetrics()
	server := NewServer(ServerConfig{QueueTimeout: o.queueTimeout, RequestTimeout: o.requestTimeout, MaxBodyBytes: o.maxBodyBytes, MetricsPublic: o.metricsPublic}, pool, authority, metrics)
	httpServer := &http.Server{
		Handler:           server,
		ReadHeaderTimeout: o.readHeaderTimeout,
		IdleTimeout:       o.idleTimeout,
		MaxHeaderBytes:    o.maxHeaderBytes,
	}
	if !o.quiet {
		if startup := pool.Hello().Startup; len(startup) > 0 {
			os.Stdout.Write(asciiEscape(startup))
			os.Stdout.Write([]byte("\n"))
		}
	}

	stopMaintain := make(chan struct{})
	if o.maintainInterval > 0 {
		go func() {
			tick := time.NewTicker(o.maintainInterval)
			defer tick.Stop()
			for {
				select {
				case <-stopMaintain:
					return
				case <-tick.C:
					server.Maintain()
				}
			}
		}()
	}

	serveErr := make(chan error, 1)
	go func() { serveErr <- httpServer.Serve(listener) }()
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	status := 0
	select {
	case sig := <-signals:
		fmt.Fprintf(os.Stderr, "tri-server: %s, draining (up to %s)\n", sig, o.drainTimeout)
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintln(os.Stderr, "tri-server:", err)
			status = 1
		}
	}
	// Drain: readiness goes false, the listener closes, in-flight requests finish.
	server.draining.Store(true)
	close(stopMaintain)
	ctx, cancel := context.WithTimeout(context.Background(), o.drainTimeout)
	if err := httpServer.Shutdown(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "tri-server: drain timed out; stopping workers")
	}
	cancel()
	pool.Close(10 * time.Second)
	return status
}
