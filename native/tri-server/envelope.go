package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"time"
	"unicode/utf8"
)

// Envelopes the front server writes itself (401, health, readiness, and its own
// failures). They have the same shape and byte format as tri/cli.py envelope()
// serialized by response_json(): sorted keys, compact separators, ASCII only.

type Diagnostic map[string]any

func NewDiagnostic(code, message string, retryable bool) Diagnostic {
	return Diagnostic{"code": code, "severity": "error", "message": message, "retryable": retryable}
}

// pythonJSON matches json.dumps(obj, sort_keys=True, separators=(",", ":")):
// map keys sorted, no HTML escaping, and non-ASCII escaped as \uXXXX.
func pythonJSON(value any) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(value); err != nil {
		panic(err)
	}
	out := bytes.TrimRight(buf.Bytes(), "\n")
	return asciiEscape(out)
}

func asciiEscape(in []byte) []byte {
	ascii := true
	for _, b := range in {
		if b >= 0x80 {
			ascii = false
			break
		}
	}
	if ascii {
		return in
	}
	var out bytes.Buffer
	for len(in) > 0 {
		r, size := utf8.DecodeRune(in)
		in = in[size:]
		if r < 0x80 {
			out.WriteByte(byte(r))
			continue
		}
		if r > 0xFFFF {
			r -= 0x10000
			fmt.Fprintf(&out, "\\u%04x\\u%04x", 0xD800+(r>>10), 0xDC00+(r&0x3FF))
			continue
		}
		fmt.Fprintf(&out, "\\u%04x", r)
	}
	return out.Bytes()
}

// Envelope builds a full response envelope. Like envelope() in tri/cli.py,
// cost.output_bytes is the length of the serialized envelope with output_bytes 0.
func Envelope(schemaVersion, schema string, command []string, actor string, ok bool, data map[string]any, diagnostics []Diagnostic, start time.Time) []byte {
	if data == nil {
		data = map[string]any{}
	}
	if diagnostics == nil {
		diagnostics = []Diagnostic{}
	}
	cost := map[string]any{
		"wall_ms":       int(time.Since(start).Milliseconds()),
		"input_bytes":   0,
		"output_bytes":  0,
		"external_cost": "0 USD",
	}
	out := map[string]any{
		"ok":             ok,
		"schema":         schema,
		"schema_version": schemaVersion,
		"command":        command,
		"actor":          actor,
		"data":           data,
		"events":         []string{},
		"diagnostics":    diagnostics,
		"warnings":       []any{},
		"cost":           cost,
		"refs":           map[string]any{},
		"links":          map[string]any{},
	}
	cost["output_bytes"] = len(pythonJSON(out))
	return pythonJSON(out)
}

func ErrorEnvelope(schemaVersion, actor string, diagnostic Diagnostic, start time.Time) []byte {
	return Envelope(schemaVersion, "tri.error", []string{"tri", "serve", "request"}, actor, false, nil, []Diagnostic{diagnostic}, start)
}

func itoa(n int) string { return strconv.Itoa(n) }
