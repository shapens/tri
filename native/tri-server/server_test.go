package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// The test binary doubles as a fake worker speaking the tri worker protocol:
// TRI_FAKE_WORKER selects its behavior.
func TestMain(m *testing.M) {
	if mode := os.Getenv("TRI_FAKE_WORKER"); mode != "" {
		fakeWorker(mode)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func fakeWorker(mode string) {
	in := bufio.NewReader(os.Stdin)
	out := os.Stdout
	if mode == "fatal" {
		body := []byte(`{"ok":false,"schema":"tri.error"}`)
		writeFrame(out, map[string]any{"type": "fatal", "length": len(body)}, body)
		return
	}
	if mode == "silent" {
		time.Sleep(time.Hour)
	}
	writeFrame(out, map[string]any{"type": "hello", "protocol": ProtocolVersion, "pid": os.Getpid(), "schema_version": "0.1.0", "home": "/fake/home", "auth": "none", "commands": []string{"files", "workspace"}, "startup": map[string]any{"ok": true}, "length": 0}, nil)
	for {
		var header map[string]any
		body, err := readFrame(in, &header)
		if err != nil {
			return
		}
		path, _ := header["path"].(string)
		switch {
		case strings.HasPrefix(path, "/slow"):
			time.Sleep(2 * time.Second)
		case strings.HasPrefix(path, "/crash"):
			os.Exit(3)
		}
		reply, _ := json.Marshal(map[string]any{"echo": header, "body": string(body), "pid": os.Getpid()})
		writeFrame(out, map[string]any{"type": "response", "id": header["id"], "status": 200, "content_type": "application/json", "length": len(reply), "wall_ms": 0.5}, reply)
	}
}

func newTestServer(t *testing.T, mode string, min, max int, authority *TokenAuthority, cfg ServerConfig) (*Server, *Pool, *httptest.Server) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	pool := NewPool(PoolConfig{Command: []string{exe}, Env: append(os.Environ(), "TRI_FAKE_WORKER="+mode), Min: min, Max: max, StartTimeout: 10 * time.Second, RestartBackoff: 20 * time.Millisecond})
	if err := pool.Start(); err != nil {
		t.Fatal(err)
	}
	if cfg.QueueTimeout == 0 {
		cfg.QueueTimeout = 5 * time.Second
	}
	if cfg.RequestTimeout == 0 {
		cfg.RequestTimeout = 10 * time.Second
	}
	if cfg.MaxBodyBytes == 0 {
		cfg.MaxBodyBytes = PythonMaxBody
	}
	server := NewServer(cfg, pool, authority, NewMetrics())
	ts := httptest.NewServer(server)
	t.Cleanup(func() {
		ts.Close()
		pool.Close(2 * time.Second)
	})
	return server, pool, ts
}

func get(t *testing.T, url string, header map[string]string) (int, map[string]any, string) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var body map[string]any
	_ = json.Unmarshal(raw, &body)
	return resp.StatusCode, body, string(raw)
}

func post(t *testing.T, url string, payload string, header map[string]string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url, strings.NewReader(payload))
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var body map[string]any
	_ = json.Unmarshal(raw, &body)
	return resp.StatusCode, body
}

func diagnosticCode(body map[string]any) string {
	list, _ := body["diagnostics"].([]any)
	if len(list) == 0 {
		return ""
	}
	first, _ := list[0].(map[string]any)
	code, _ := first["code"].(string)
	return code
}

func TestEnvelopeMatchesPythonBytes(t *testing.T) {
	// From tri/cli.py: response_json(envelope("tri.error", ["tri", "serve", "request"], ok=False,
	// actor="actor_local", diagnostics=[TriError("unauthenticated", ...).diagnostic])) with wall_ms 0.
	want := `{"actor":"actor_local","command":["tri","serve","request"],"cost":{"external_cost":"0 USD","input_bytes":0,"output_bytes":377,"wall_ms":0},"data":{},"diagnostics":[{"code":"unauthenticated","message":"A valid bearer token is required.","retryable":false,"severity":"error"}],"events":[],"links":{},"ok":false,"refs":{},"schema":"tri.error","schema_version":"0.1.0","warnings":[]}`
	got := string(ErrorEnvelope("0.1.0", "actor_local", NewDiagnostic("unauthenticated", "A valid bearer token is required.", false), time.Now()))
	if got != want {
		t.Fatalf("envelope bytes differ\n got %s\nwant %s", got, want)
	}
	if string(pythonJSON(map[string]any{"a": "\u00e9<&>\U0001F600"})) != `{"a":"\u00e9<&>\ud83d\ude00"}` {
		t.Fatalf("ascii escaping differs: %s", pythonJSON(map[string]any{"a": "\u00e9<&>\U0001F600"}))
	}
}

func TestTokenAuthorityFollowsFileEdits(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tokens.json")
	authority := NewTokenAuthority(path)
	if authority.ActorFor("tri_x") != "" {
		t.Fatal("missing file must hold no tokens")
	}
	write := func(entries string) {
		if err := os.WriteFile(path, []byte(`{"tokens": [`+entries+`]}`), 0o600); err != nil {
			t.Fatal(err)
		}
		// Make the stamp change even on coarse mtime filesystems.
		future := time.Now().Add(time.Duration(len(entries)) * time.Second)
		os.Chtimes(path, future, future)
	}
	write(fmt.Sprintf(`{"id": "tok_1", "actor": "agent:a", "sha256": %q}, {"actor": 5, "sha256": "x"}, "junk"`, TokenDigest("tri_a")))
	if got := authority.ActorFor("tri_a"); got != "agent:a" {
		t.Fatalf("ActorFor = %q", got)
	}
	if authority.ActorFor("tri_b") != "" || authority.ActorFor("") != "" {
		t.Fatal("unknown and empty tokens must not resolve")
	}
	if authority.Count() != 1 {
		t.Fatalf("invalid entries must be ignored, got %d tokens", authority.Count())
	}
	write("")
	if authority.ActorFor("tri_a") != "" {
		t.Fatal("revocation must apply on the next request")
	}
}

func TestAuthHealthReadyAndForwarding(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tokens.json")
	os.WriteFile(path, []byte(fmt.Sprintf(`{"tokens":[{"actor":"agent:a","sha256":%q}]}`, TokenDigest("tri_good"))), 0o600)
	_, _, ts := newTestServer(t, "echo", 1, 2, NewTokenAuthority(path), ServerConfig{})

	status, body, _ := get(t, ts.URL+"/v0/health", nil)
	if status != 200 || body["schema"] != "tri.api.health" {
		t.Fatalf("health: %d %v", status, body)
	}
	if data := body["data"].(map[string]any); data["home"] != nil || data["auth"] != "bearer" {
		t.Fatalf("health must hide home when tokens are required: %v", data)
	}
	status, body, _ = get(t, ts.URL+"/v0/ready", nil)
	if status != 200 || body["data"].(map[string]any)["ready"] != true {
		t.Fatalf("ready: %d %v", status, body)
	}
	status, body, _ = get(t, ts.URL+"/v0/projects", nil)
	if status != 401 || diagnosticCode(body) != "unauthenticated" {
		t.Fatalf("missing token: %d %v", status, body)
	}
	status, body, _ = get(t, ts.URL+"/v0/projects", map[string]string{"Authorization": "Bearer tri_bad"})
	if status != 401 {
		t.Fatalf("unknown token: %d", status)
	}
	status, _, _ = get(t, ts.URL+"/metrics", nil)
	if status != 401 {
		t.Fatalf("metrics must need a token when auth is on: %d", status)
	}
	status, body, _ = get(t, ts.URL+"/v0/projects?x=1", map[string]string{"Authorization": "bearer   tri_good ", "X-Tri-Actor": "agent:a"})
	if status != 200 {
		t.Fatalf("forwarded GET: %d %v", status, body)
	}
	echo := body["echo"].(map[string]any)
	if echo["actor"] != "agent:a" || echo["path"] != "/v0/projects?x=1" || echo["method"] != "GET" {
		t.Fatalf("frame: %v", echo)
	}
	headers := echo["headers"].(map[string]any)
	if headers["X-Tri-Actor"] != "agent:a" || headers["Authorization"] != nil {
		t.Fatalf("headers forwarded: %v (the token must never reach the worker)", headers)
	}
	status, body = post(t, ts.URL+"/v0/command", `{"argv":["files","list","ws"]}`, map[string]string{"Authorization": "Bearer tri_good"})
	if status != 200 || body["body"] != `{"argv":["files","list","ws"]}` {
		t.Fatalf("forwarded POST: %d %v", status, body)
	}
	if body["echo"].(map[string]any)["headers"].(map[string]any)["Content-Length"] != "30" {
		t.Fatalf("Content-Length must be forwarded: %v", body["echo"])
	}
	status, _, metricsText := get(t, ts.URL+"/metrics", map[string]string{"Authorization": "Bearer tri_good"})
	if status != 200 || !strings.Contains(metricsText, `tri_http_requests_total{route="command:files",method="POST",code="200"} 1`) || !strings.Contains(metricsText, "tri_auth_failures_total 3") {
		t.Fatalf("metrics: %d\n%s", status, metricsText)
	}
}

func TestOversizeBodyIsNotRead(t *testing.T) {
	_, _, ts := newTestServer(t, "echo", 1, 1, nil, ServerConfig{MaxBodyBytes: 64})
	status, body := post(t, ts.URL+"/v0/command", `{"argv":["`+strings.Repeat("x", 100)+`"]}`, nil)
	if status != 200 || diagnosticCode(body) != "invalid_command" {
		t.Fatalf("oversize body: %d %v", status, body)
	}
}

func TestUnsupportedMethod(t *testing.T) {
	_, _, ts := newTestServer(t, "echo", 1, 1, nil, ServerConfig{})
	req, _ := http.NewRequest(http.MethodDelete, ts.URL+"/v0/command", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 501 {
		t.Fatalf("DELETE: %d", resp.StatusCode)
	}
}

func TestCrashedWorkerIsReplaced(t *testing.T) {
	_, pool, ts := newTestServer(t, "echo", 1, 1, nil, ServerConfig{})
	_, before, _ := get(t, ts.URL+"/v0/x", nil)
	status, body, _ := get(t, ts.URL+"/crash", nil)
	if status != 502 || diagnosticCode(body) != "worker_failed" {
		t.Fatalf("crash: %d %v", status, body)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		status, after, _ := get(t, ts.URL+"/v0/x", nil)
		if status == 200 {
			if after["pid"] == before["pid"] {
				t.Fatal("expected a new worker process")
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no replacement worker: %d", status)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if stats := pool.Stats(); stats.Restarts < 1 || stats.Crashes < 1 {
		t.Fatalf("stats: %+v", stats)
	}
}

func TestRequestTimeoutKillsWorker(t *testing.T) {
	_, _, ts := newTestServer(t, "echo", 1, 1, nil, ServerConfig{RequestTimeout: 200 * time.Millisecond})
	status, body, _ := get(t, ts.URL+"/slow", nil)
	if status != 504 || diagnosticCode(body) != "request_timeout" {
		t.Fatalf("timeout: %d %v", status, body)
	}
	status, _, _ = get(t, ts.URL+"/v0/x", nil)
	if status != 200 {
		t.Fatalf("after timeout: %d", status)
	}
}

func TestQueueTimeoutAnswersBusy(t *testing.T) {
	_, _, ts := newTestServer(t, "echo", 1, 1, nil, ServerConfig{QueueTimeout: 100 * time.Millisecond})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		get(t, ts.URL+"/slow", nil)
	}()
	time.Sleep(200 * time.Millisecond)
	status, body, _ := get(t, ts.URL+"/v0/x", nil)
	if status != 503 || diagnosticCode(body) != "server_busy" {
		t.Fatalf("busy: %d %v", status, body)
	}
	list := body["diagnostics"].([]any)
	if list[0].(map[string]any)["retryable"] != true {
		t.Fatal("server_busy must be retryable")
	}
	wg.Wait()
}

func TestPoolGrowsUnderLoadAndRunsConcurrently(t *testing.T) {
	_, pool, ts := newTestServer(t, "echo", 1, 4, nil, ServerConfig{})
	var wg sync.WaitGroup
	began := time.Now()
	pids := sync.Map{}
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, body, _ := get(t, ts.URL+"/slow", nil)
			pids.Store(body["pid"], true)
		}()
	}
	wg.Wait()
	count := 0
	pids.Range(func(_, _ any) bool { count++; return true })
	if count < 2 {
		t.Fatalf("expected requests spread over several workers, got %d", count)
	}
	if elapsed := time.Since(began); elapsed > 7*time.Second {
		t.Fatalf("four 2 s requests took %s: not concurrent", elapsed)
	}
	if pool.Stats().Live < 2 {
		t.Fatalf("pool did not grow: %+v", pool.Stats())
	}
}

func TestFatalHelloIsReported(t *testing.T) {
	exe, _ := os.Executable()
	pool := NewPool(PoolConfig{Command: []string{exe}, Env: append(os.Environ(), "TRI_FAKE_WORKER=fatal"), Min: 1, Max: 1})
	err := pool.Start()
	fatal, ok := err.(*FatalError)
	if !ok || !strings.Contains(string(fatal.Envelope), "tri.error") {
		t.Fatalf("Start() = %v", err)
	}
}

func TestSilentWorkerTimesOutAtStart(t *testing.T) {
	exe, _ := os.Executable()
	pool := NewPool(PoolConfig{Command: []string{exe}, Env: append(os.Environ(), "TRI_FAKE_WORKER=silent"), Min: 1, Max: 1, StartTimeout: 300 * time.Millisecond})
	if err := pool.Start(); err == nil || !strings.Contains(err.Error(), "no hello") {
		t.Fatalf("Start() = %v", err)
	}
}

func TestDrainFinishesInFlightRequests(t *testing.T) {
	server, pool, _ := newTestServer(t, "echo", 1, 1, nil, ServerConfig{})
	httpServer := httptest.NewUnstartedServer(server)
	httpServer.Start()
	result := make(chan int, 1)
	go func() {
		status, _, _ := get(t, httpServer.URL+"/slow", nil)
		result <- status
	}()
	time.Sleep(300 * time.Millisecond)
	server.draining.Store(true)
	status, body, _ := get(t, httpServer.URL+"/v0/ready", nil)
	if status != 503 || body["data"].(map[string]any)["status"] != "draining" {
		t.Fatalf("ready while draining: %d %v", status, body)
	}
	httpServer.Config.SetKeepAlivesEnabled(false)
	httpServer.Close() // waits for the in-flight request
	if got := <-result; got != 200 {
		t.Fatalf("in-flight request during drain: %d", got)
	}
	pool.Close(time.Second)
	if pool.Stats().Live != 0 {
		t.Fatal("workers must stop after drain")
	}
}

func TestParseOptionsWorkerArgs(t *testing.T) {
	o, err := parseOptions([]string{"--host", "0.0.0.0", "--port", "0", "--auth-file", "t.json", "--run-sandbox", "required", "--run-executor", "remote", "--workers", "3", "--", "python", "-m", "tri", "worker", "--stdio"})
	if err != nil {
		t.Fatal(err)
	}
	got := strings.Join(o.workerArgs("4321"), " ")
	want := "python -m tri worker --stdio --host 0.0.0.0 --port 4321 --auth-file t.json --run-sandbox required --run-executor remote"
	if got != want || o.maxWorkers != 6 {
		t.Fatalf("worker args = %q (max %d)", got, o.maxWorkers)
	}
	if _, err := parseOptions([]string{"--run-sandbox", "maybe"}); err == nil {
		t.Fatal("bad --run-sandbox must be refused")
	}
}
