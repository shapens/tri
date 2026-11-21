package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"sync/atomic"
	"time"
)

// The worker protocol (tri/worker.py): one JSON header line, then exactly
// header.length bytes of body.

type FrameHeader struct {
	Type   string            `json:"type"`
	ID     uint64            `json:"id,omitempty"`
	Method string            `json:"method,omitempty"`
	Path   string            `json:"path,omitempty"`
	Header map[string]string `json:"headers,omitempty"`
	// Actor is the authenticated actor; it is always sent on requests (null when
	// the server has no token file), so it is not omitempty.
	Actor  *string `json:"actor"`
	Length int     `json:"length"`
}

type ResponseHeader struct {
	Type        string  `json:"type"`
	ID          uint64  `json:"id"`
	Status      int     `json:"status"`
	ContentType string  `json:"content_type"`
	Length      int     `json:"length"`
	WallMS      float64 `json:"wall_ms"`
	Retire      bool    `json:"retire"`
	OK          bool    `json:"ok"`
}

type Hello struct {
	Type          string          `json:"type"`
	Protocol      int             `json:"protocol"`
	PID           int             `json:"pid"`
	SchemaVersion string          `json:"schema_version"`
	Home          string          `json:"home"`
	Auth          string          `json:"auth"`
	RunSandbox    string          `json:"run_sandbox"`
	Commands      []string        `json:"commands"`
	Startup       json.RawMessage `json:"startup"`
	Length        int             `json:"length"`
}

const ProtocolVersion = 1

var (
	ErrBusy        = errors.New("no worker became free in time")
	ErrTimeout     = errors.New("worker did not answer in time")
	ErrWorkerDied  = errors.New("worker exited during the request")
	ErrPoolClosed  = errors.New("worker pool is closed")
	errFatalWorker = errors.New("worker refused to start")
)

// FatalError carries the tri.error envelope a worker wrote before refusing to
// start (for example, serving beyond loopback without a token file).
type FatalError struct {
	Envelope []byte
}

func (e *FatalError) Error() string { return "worker refused to start: " + string(e.Envelope) }

type Worker struct {
	id       int64
	cmd      *exec.Cmd
	in       *os.File
	out      *bufio.Reader
	outFile  *os.File
	hello    Hello
	served   int
	lastUsed time.Time
	exited   chan struct{}
	broken   atomic.Bool
}

func (w *Worker) alive() bool {
	select {
	case <-w.exited:
		return false
	default:
		return !w.broken.Load()
	}
}

func (w *Worker) kill() {
	w.broken.Store(true)
	killTree(w.cmd)
	w.in.Close()
}

func (w *Worker) close(grace time.Duration) {
	w.broken.Store(true)
	w.in.Close() // end of input: the worker closes its store and exits
	select {
	case <-w.exited:
	case <-time.After(grace):
		killTree(w.cmd)
		<-w.exited
	}
}

func writeFrame(dst io.Writer, header any, body []byte) error {
	line, err := json.Marshal(header)
	if err != nil {
		return err
	}
	buf := make([]byte, 0, len(line)+1+len(body))
	buf = append(buf, line...)
	buf = append(buf, '\n')
	buf = append(buf, body...)
	_, err = dst.Write(buf)
	return err
}

func readFrame(src *bufio.Reader, header any) ([]byte, error) {
	line, err := src.ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	var probe struct {
		Length int `json:"length"`
	}
	if err := json.Unmarshal(line, &probe); err != nil {
		return nil, fmt.Errorf("bad frame header: %w", err)
	}
	if err := json.Unmarshal(line, header); err != nil {
		return nil, fmt.Errorf("bad frame header: %w", err)
	}
	if probe.Length < 0 {
		return nil, fmt.Errorf("bad frame length %d", probe.Length)
	}
	body := make([]byte, probe.Length)
	if _, err := io.ReadFull(src, body); err != nil {
		return nil, err
	}
	return body, nil
}

// Do sends one request and waits for its response. On timeout the worker is
// killed (a stuck worker cannot be reused) and ErrTimeout is returned.
func (w *Worker) Do(header FrameHeader, body []byte, timeout time.Duration) (ResponseHeader, []byte, error) {
	header.Length = len(body)
	type result struct {
		header ResponseHeader
		body   []byte
		err    error
	}
	done := make(chan result, 1)
	go func() {
		if err := writeFrame(w.in, header, body); err != nil {
			done <- result{err: err}
			return
		}
		var resp ResponseHeader
		respBody, err := readFrame(w.out, &resp)
		done <- result{resp, respBody, err}
	}()
	var timer <-chan time.Time
	if timeout > 0 {
		t := time.NewTimer(timeout)
		defer t.Stop()
		timer = t.C
	}
	select {
	case r := <-done:
		if r.err != nil {
			w.kill()
			return ResponseHeader{}, nil, fmt.Errorf("%w: %v", ErrWorkerDied, r.err)
		}
		if r.header.ID != header.ID {
			w.kill()
			return ResponseHeader{}, nil, fmt.Errorf("%w: response id %d for request %d", ErrWorkerDied, r.header.ID, header.ID)
		}
		w.served++
		w.lastUsed = time.Now()
		if r.header.Retire {
			w.broken.Store(true)
		}
		return r.header, r.body, nil
	case <-timer:
		w.kill()
		return ResponseHeader{}, nil, ErrTimeout
	}
}

type PoolConfig struct {
	Command        []string // worker argv, e.g. python3 -m tri worker --stdio --host ... --port ...
	Env            []string
	Stderr         io.Writer
	Min, Max       int
	StartTimeout   time.Duration
	IdleTimeout    time.Duration // retire workers above Min after this long idle
	MaxRequests    int           // recycle a worker after this many requests (0: never)
	RestartBackoff time.Duration
}

type PoolStats struct {
	Live, Idle, Busy, Starting, Max int
	Restarts, Spawned, Crashes      int64
}

// Pool runs persistent worker processes. Each worker serves one request at a
// time; the pool grows from Min to Max when every worker is busy and shrinks
// back after IdleTimeout. Workers that exit or time out are replaced.
type Pool struct {
	cfg  PoolConfig
	idle chan *Worker

	mu       sync.Mutex
	live     map[*Worker]struct{}
	starting int
	waiting  int
	closed   bool
	lastErr  error
	fatal    *FatalError

	nextID   atomic.Int64
	reqID    atomic.Uint64
	restarts atomic.Int64
	spawned  atomic.Int64
	crashes  atomic.Int64

	helloMu sync.RWMutex
	hello   Hello

	stop chan struct{}
}

func NewPool(cfg PoolConfig) *Pool {
	if cfg.Min < 1 {
		cfg.Min = 1
	}
	if cfg.Max < cfg.Min {
		cfg.Max = cfg.Min
	}
	if cfg.StartTimeout <= 0 {
		cfg.StartTimeout = 60 * time.Second
	}
	if cfg.RestartBackoff <= 0 {
		cfg.RestartBackoff = 200 * time.Millisecond
	}
	if cfg.Stderr == nil {
		cfg.Stderr = os.Stderr
	}
	return &Pool{cfg: cfg, idle: make(chan *Worker, cfg.Max), live: map[*Worker]struct{}{}, stop: make(chan struct{})}
}

// Start spawns the first worker synchronously, so a refusal to start (policy)
// or a broken worker command is reported before the server accepts requests,
// then brings the pool up to Min in the background.
func (p *Pool) Start() error {
	p.mu.Lock()
	p.starting++
	p.mu.Unlock()
	w, err := p.spawn()
	p.mu.Lock()
	p.starting--
	if err != nil {
		p.mu.Unlock()
		return err
	}
	p.live[w] = struct{}{}
	p.mu.Unlock()
	p.setHello(w.hello)
	p.idle <- w
	for i := 1; i < p.cfg.Min; i++ {
		p.grow(false)
	}
	go p.janitor()
	return nil
}

func (p *Pool) setHello(h Hello) {
	p.helloMu.Lock()
	p.hello = h
	p.helloMu.Unlock()
}

func (p *Pool) Hello() Hello {
	p.helloMu.RLock()
	defer p.helloMu.RUnlock()
	return p.hello
}

func (p *Pool) spawn() (*Worker, error) {
	if len(p.cfg.Command) == 0 {
		return nil, errors.New("no worker command")
	}
	stdinR, stdinW, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	stdoutR, stdoutW, err := os.Pipe()
	if err != nil {
		stdinR.Close()
		stdinW.Close()
		return nil, err
	}
	cmd := exec.Command(p.cfg.Command[0], p.cfg.Command[1:]...)
	cmd.Env = p.cfg.Env
	// Pass the pipe files directly: exec.Cmd then starts no copying goroutines,
	// and Wait never closes our read end before a final response is read.
	cmd.Stdin = stdinR
	cmd.Stdout = stdoutW
	cmd.Stderr = p.cfg.Stderr
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		stdinR.Close()
		stdinW.Close()
		stdoutR.Close()
		stdoutW.Close()
		return nil, fmt.Errorf("start worker %q: %w", p.cfg.Command[0], err)
	}
	stdinR.Close()
	stdoutW.Close()
	p.spawned.Add(1)
	w := &Worker{id: p.nextID.Add(1), cmd: cmd, in: stdinW, outFile: stdoutR, out: bufio.NewReaderSize(stdoutR, 1<<16), exited: make(chan struct{}), lastUsed: time.Now()}
	go func() {
		cmd.Wait()
		stdoutR.Close()
		close(w.exited)
	}()

	type helloResult struct {
		hello Hello
		body  []byte
		err   error
	}
	got := make(chan helloResult, 1)
	go func() {
		var h Hello
		body, err := readFrame(w.out, &h)
		got <- helloResult{h, body, err}
	}()
	select {
	case r := <-got:
		if r.err != nil {
			w.kill()
			return nil, fmt.Errorf("worker exited before its hello: %v", r.err)
		}
		if r.hello.Type == "fatal" {
			w.close(5 * time.Second)
			return nil, &FatalError{Envelope: r.body}
		}
		if r.hello.Type != "hello" || r.hello.Protocol != ProtocolVersion {
			w.kill()
			return nil, fmt.Errorf("worker speaks protocol %q/%d, want hello/%d", r.hello.Type, r.hello.Protocol, ProtocolVersion)
		}
		w.hello = r.hello
		return w, nil
	case <-time.After(p.cfg.StartTimeout):
		w.kill()
		return nil, fmt.Errorf("worker sent no hello within %s", p.cfg.StartTimeout)
	}
}

// grow starts one more worker in the background if the pool has room. On
// demand (onDemand), it starts one only while waiters outnumber workers
// already starting, so a burst does not spawn more workers than it needs.
func (p *Pool) grow(isRestart bool) bool { return p.growIf(isRestart, false) }

func (p *Pool) growIf(isRestart, onDemand bool) bool {
	p.mu.Lock()
	if p.closed || len(p.live)+p.starting >= p.cfg.Max || (onDemand && p.starting >= p.waiting) {
		p.mu.Unlock()
		return false
	}
	p.starting++
	p.mu.Unlock()
	go func() {
		if isRestart {
			p.restarts.Add(1)
		}
		for attempt := 0; ; attempt++ {
			w, err := p.spawn()
			p.mu.Lock()
			if err == nil {
				p.starting--
				if p.closed {
					p.mu.Unlock()
					w.close(5 * time.Second)
					return
				}
				p.live[w] = struct{}{}
				p.mu.Unlock()
				p.setHello(w.hello)
				p.idle <- w
				return
			}
			p.lastErr = err
			var fatal *FatalError
			if errors.As(err, &fatal) {
				p.fatal = fatal
			}
			stop := p.closed || attempt >= 4 || (len(p.live) > 0 && p.waiting == 0)
			if stop {
				p.starting--
				p.mu.Unlock()
				fmt.Fprintf(p.cfg.Stderr, "tri-server: worker start failed: %v\n", err)
				return
			}
			p.mu.Unlock()
			time.Sleep(p.cfg.RestartBackoff << attempt)
		}
	}()
	return true
}

// Acquire returns an idle worker, growing the pool when every worker is busy,
// or ErrBusy once queueTimeout passes.
func (p *Pool) Acquire(queueTimeout time.Duration) (*Worker, error) {
	var deadline <-chan time.Time
	for {
		select {
		case w := <-p.idle:
			if w.alive() {
				return w, nil
			}
			p.discard(w)
			continue
		default:
		}
		p.mu.Lock()
		if p.closed {
			p.mu.Unlock()
			return nil, ErrPoolClosed
		}
		p.waiting++
		p.mu.Unlock()
		p.growIf(false, true)
		if deadline == nil {
			t := time.NewTimer(queueTimeout)
			defer t.Stop()
			deadline = t.C
		}
		select {
		case w := <-p.idle:
			p.mu.Lock()
			p.waiting--
			p.mu.Unlock()
			if w.alive() {
				return w, nil
			}
			p.discard(w)
		case <-deadline:
			p.mu.Lock()
			p.waiting--
			p.mu.Unlock()
			return nil, ErrBusy
		case <-p.stop:
			p.mu.Lock()
			p.waiting--
			p.mu.Unlock()
			return nil, ErrPoolClosed
		}
	}
}

// TryAcquire returns an idle worker without waiting, or nil.
func (p *Pool) TryAcquire() *Worker {
	select {
	case w := <-p.idle:
		if w.alive() {
			return w
		}
		p.discard(w)
	default:
	}
	return nil
}

// Release returns a worker after a request. A broken or exhausted worker is
// replaced instead.
func (p *Pool) Release(w *Worker) {
	if p.cfg.MaxRequests > 0 && w.served >= p.cfg.MaxRequests && w.alive() {
		p.remove(w)
		go w.close(10 * time.Second)
		p.grow(false)
		return
	}
	if !w.alive() {
		p.crashes.Add(1)
		p.discard(w)
		return
	}
	p.mu.Lock()
	closed := p.closed
	p.mu.Unlock()
	if closed {
		p.remove(w)
		go w.close(10 * time.Second)
		return
	}
	p.idle <- w
}

func (p *Pool) remove(w *Worker) {
	p.mu.Lock()
	delete(p.live, w)
	p.mu.Unlock()
}

// discard drops a dead worker and starts a replacement when the pool is below
// its minimum or someone is waiting.
func (p *Pool) discard(w *Worker) {
	w.kill()
	p.mu.Lock()
	delete(p.live, w)
	need := !p.closed && (len(p.live)+p.starting < p.cfg.Min || p.waiting > 0)
	p.mu.Unlock()
	if need {
		p.grow(true)
	}
}

// janitor keeps the pool at Min after crashes and retires idle extra workers.
func (p *Pool) janitor() {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-p.stop:
			return
		case <-tick.C:
		}
		p.mu.Lock()
		short := !p.closed && len(p.live)+p.starting < p.cfg.Min
		extra := len(p.live) - p.cfg.Min
		p.mu.Unlock()
		if short {
			p.grow(true)
		}
		if p.cfg.IdleTimeout <= 0 || extra <= 0 {
			continue
		}
		for i := 0; i < extra; i++ {
			w := p.TryAcquire()
			if w == nil {
				break
			}
			if time.Since(w.lastUsed) >= p.cfg.IdleTimeout {
				p.remove(w)
				go w.close(10 * time.Second)
			} else {
				p.idle <- w
			}
		}
	}
}

func (p *Pool) Stats() PoolStats {
	p.mu.Lock()
	defer p.mu.Unlock()
	idle := len(p.idle)
	return PoolStats{
		Live:     len(p.live),
		Idle:     idle,
		Busy:     len(p.live) - idle,
		Starting: p.starting,
		Max:      p.cfg.Max,
		Restarts: p.restarts.Load(),
		Spawned:  p.spawned.Load(),
		Crashes:  p.crashes.Load(),
	}
}

func (p *Pool) NextRequestID() uint64 { return p.reqID.Add(1) }

// Close stops the pool: idle workers get end of input and exit; busy ones are
// closed when released. Workers still running after grace are killed.
func (p *Pool) Close(grace time.Duration) {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	p.closed = true
	close(p.stop)
	workers := make([]*Worker, 0, len(p.live))
	for w := range p.live {
		workers = append(workers, w)
	}
	p.live = map[*Worker]struct{}{}
	p.mu.Unlock()
	var wg sync.WaitGroup
	for _, w := range workers {
		wg.Add(1)
		go func(w *Worker) {
			defer wg.Done()
			w.close(grace)
		}(w)
	}
	wg.Wait()
}
