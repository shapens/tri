package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"sync"
)

// TokenAuthority resolves bearer tokens to actors using the token file written
// by `tri auth issue` (tri/auth.py): {"tokens": [{"sha256": hex, "actor": ...}]}.
// Only token digests are stored. The file is re-read whenever its mtime or size
// changes, so a revocation applies on the next request without a restart.
type TokenAuthority struct {
	path string

	mu       sync.Mutex
	stamp    [2]int64
	loaded   bool
	byDigest map[string]string
	loadErr  error
}

func NewTokenAuthority(path string) *TokenAuthority {
	return &TokenAuthority{path: path, byDigest: map[string]string{}}
}

func TokenDigest(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

type tokenFile struct {
	Tokens []map[string]any `json:"tokens"`
}

// loadTokens mirrors tri.auth.load_token_file: a missing file holds no tokens,
// and entries without string sha256 and actor fields are ignored.
func loadTokens(path string) (map[string]string, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return map[string]string{}, nil
	}
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 {
		return map[string]string{}, nil
	}
	var parsed any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, err
	}
	out := map[string]string{}
	object, ok := parsed.(map[string]any)
	if !ok {
		return out, nil
	}
	list, _ := object["tokens"].([]any)
	for _, item := range list {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		digest, ok1 := entry["sha256"].(string)
		actor, ok2 := entry["actor"].(string)
		if ok1 && ok2 {
			out[digest] = actor
		}
	}
	return out, nil
}

func (a *TokenAuthority) refresh() {
	var stamp [2]int64
	if info, err := os.Stat(a.path); err == nil {
		stamp = [2]int64{info.ModTime().UnixNano(), info.Size()}
	}
	if a.loaded && stamp == a.stamp {
		return
	}
	tokens, err := loadTokens(a.path)
	if err != nil {
		// A half-written or corrupt file must not open the server: keep no tokens
		// until it parses again (tri serve raises and fails the request instead).
		a.byDigest = map[string]string{}
		a.loadErr = err
	} else {
		a.byDigest = tokens
		a.loadErr = nil
	}
	a.stamp = stamp
	a.loaded = true
}

// ActorFor returns the actor a token is bound to, or "" when it is unknown.
func (a *TokenAuthority) ActorFor(token string) string {
	if token == "" {
		return ""
	}
	digest := TokenDigest(token)
	a.mu.Lock()
	defer a.mu.Unlock()
	a.refresh()
	return a.byDigest[digest]
}

// Count reports how many tokens are currently loaded.
func (a *TokenAuthority) Count() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.refresh()
	return len(a.byDigest)
}
