"""Model Context Protocol server for Tri over stdio.

Agent harnesses (Claude Code, Codex, Cursor) reach tools through MCP. This
server exposes Tri's machine-readable command surface as two tools, `tri` and
`tri_batch`, and pins every call to the actor the server started as, so a model
cannot escalate by passing `--actor`.

Messages are newline-delimited JSON-RPC 2.0 on stdin/stdout. Anything a Tri
command prints while running is redirected to stderr so stdout stays a clean
protocol stream.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import IO, Any, Callable

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "tri", "version": "0.1.0"}

Execute = Callable[[list[str]], dict[str, Any]]
Batch = Callable[[dict[str, Any]], dict[str, Any]]


def tool_definitions(actor: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "tri",
            "title": "Run a Tri command",
            "description": (
                "Run one Tri source-control command and get its typed JSON response. "
                f"Every call runs as {actor}; passing --actor is refused. "
                'Start with argv ["commands"] to list every command with usage. Core loop: '
                '["workspace","create",<project>,"--from","main"] -> ["files","read"|"write"|"search",...] -> '
                '["run",<workspace>,"--",<cmd>...] -> ["changeset","create",<workspace>,"--title",<t>] -> '
                '["review","create",<changeset>] or ["changeset","merge",<changeset>,"--target","main"]. '
                'History without a checkout: ["files","log"|"blame",<ref>,<path>], ["snapshots","merge-base",<a>,<b>].'
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "description": 'Arguments after "tri", for example ["workspace","list"].'},
                    "response": {"type": "string", "enum": ["full", "summary"], "description": "summary trims large payloads."},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        },
        {
            "name": "tri_batch",
            "title": "Run dependent Tri commands",
            "description": (
                "Run several Tri commands in one round trip. Later commands can reference earlier results "
                'with placeholders such as "${ws.data.id}" when the earlier entry has "as": "ws". '
                f"All commands run as {actor}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "argv": {"type": "array", "items": {"type": "string"}},
                                "as": {"type": "string"},
                                "response": {"type": "string", "enum": ["full", "summary"]},
                            },
                            "required": ["argv"],
                        },
                    },
                    "continue_on_error": {"type": "boolean"},
                    "response": {"type": "string", "enum": ["full", "summary"]},
                    "report": {"type": "string", "enum": ["results", "returns"]},
                },
                "required": ["commands"],
                "additionalProperties": False,
            },
        },
    ]


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True, separators=(",", ":"))}],
        "structuredContent": payload,
        "isError": not payload.get("ok", False),
    }


class TriMCPServer:
    def __init__(self, *, actor: str, execute: Execute, batch: Batch) -> None:
        self.actor = actor
        self.execute = execute
        self.batch = batch

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid JSON-RPC message.")
        method = message.get("method")
        message_id = message.get("id")
        is_notification = "id" not in message
        if method is None:
            return None  # a response to a request we never send
        params = message.get("params") or {}
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        f"Tri is agent-native source control. You act as {self.actor}. Do source-control work through the "
                        "tri tool: isolated workspaces instead of branches, automatic snapshots instead of commits, "
                        "changesets instead of pull requests. Responses are typed JSON with diagnostics; a diagnostic "
                        "with retryable=true is safe to retry."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_definitions(self.actor)}
            elif method == "tools/call":
                result = self.call_tool(params.get("name"), params.get("arguments") or {})
            elif is_notification:
                return None
            else:
                return _error(message_id, -32601, f"Method not found: {method}")
        except ValueError as exc:
            return None if is_notification else _error(message_id, -32602, str(exc))
        return None if is_notification else _result(message_id, result)

    def call_tool(self, name: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        if name == "tri":
            argv = arguments.get("argv")
            if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
                raise ValueError("tri requires argv as a list of strings.")
            if argv and argv[0] == "tri":
                argv = argv[1:]
            response = arguments.get("response")
            if response in ("full", "summary"):
                argv = ["--response", response, *argv]
            return _tool_result(self.execute(argv))
        if name == "tri_batch":
            if not isinstance(arguments.get("commands"), list):
                raise ValueError("tri_batch requires commands as a list.")
            return _tool_result(self.batch(arguments))
        raise ValueError(f"Unknown tool: {name}")


def serve_stdio(server: TriMCPServer, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "Parse error.")
        else:
            with contextlib.redirect_stdout(sys.stderr):
                reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            stdout.flush()
