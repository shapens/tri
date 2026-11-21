"""Versioned contract files generated from the registries in `tri/cli.py`.

The JSON contracts are the compatibility boundary for rewriting the server
(tri.md, "Production runtime"): any implementation that produces these
envelopes for these commands and routes can replace the Python one. This module
turns `COMMAND_CONTRACTS`, `SCHEMA_CATALOG`, `API_BLOCKED_COMMANDS`, and the HTTP
endpoint catalog into JSON Schema (draft 2020-12) files under `contracts/v0/`.

    python -m tri.contracts --write    # regenerate contracts/v0
    python -m tri.contracts --check    # exit 1 if the files drifted from the registries

`tests/test_contracts.py` runs the check, so a registry edit without regenerating
the files fails the suite.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tri import cli

CONTRACT_VERSION = "v0"
JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "contracts" / CONTRACT_VERSION
SEMVER_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"

# Routes answered without a bearer token, in both `tri serve` and `tri-server`.
UNAUTHENTICATED_ROUTES = ["/health", "/v0/health", "/ready", "/v0/ready"]


def schema_file(name: str) -> str:
    return f"schemas/{name}.json"


def property_schema(informal: str) -> dict[str, Any]:
    """Map the catalog's informal property types onto JSON Schema."""
    mapping: dict[str, dict[str, Any]] = {
        "bool": {"type": "boolean"},
        "string": {"type": "string"},
        "semver": {"type": "string", "pattern": SEMVER_PATTERN},
        "actor id": {"type": "string"},
        "object": {"type": "object"},
        "schema-specific object": {"type": "object"},
        "Cost": {"type": "object", "required": ["wall_ms"], "properties": {"wall_ms": {"type": "integer"}, "input_bytes": {"type": "integer"}, "output_bytes": {"type": "integer"}, "external_cost": {"type": "string"}}},
        "list<string>": {"type": "array", "items": {"type": "string"}},
        "list<evt_ id>": {"type": "array", "items": {"type": "string"}},
        "list<Diagnostic>": {"type": "array", "items": {"$ref": "diagnostic.json"}},
    }
    return {**mapping.get(informal, {}), "x-tri-type": informal}


def data_schema(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {
        "$schema": JSON_SCHEMA,
        "$id": schema_file(name),
        "title": name,
        "x-tri-schema-version": cli.SCHEMA_VERSION,
        "type": "object",
    }
    if entry.get("required"):
        document["required"] = list(entry["required"])
    if entry.get("required_any"):
        document["anyOf"] = [{"required": list(variant)} for variant in entry["required_any"]]
    if entry.get("properties"):
        document["properties"] = {key: property_schema(value) for key, value in entry["properties"].items()}
    return document


def envelope_schema() -> dict[str, Any]:
    entry = cli.SCHEMA_CATALOG["tri.response"]
    document = data_schema("tri.response", entry)
    document["$id"] = "envelope.json"
    document["description"] = "Full response envelope. `data` is validated separately against schemas/<schema>.json; `tri.error` envelopes carry diagnostics instead."
    return document


def summary_envelope_schema() -> dict[str, Any]:
    entry = cli.SCHEMA_CATALOG["tri.response.summary"]
    document = data_schema("tri.response.summary", entry)
    document["$id"] = "envelope-summary.json"
    document["description"] = "Envelope for --response summary: data is a compacted view, so only the envelope is contractual."
    document["properties"] = {
        "ok": {"type": "boolean"},
        "schema": {"type": "string"},
        "schema_version": {"type": "string", "pattern": SEMVER_PATTERN},
        "data": {"type": "object"},
        "events": {"type": "array", "items": {"type": "string"}},
        "diagnostics": {"type": "array", "items": {"$ref": "diagnostic.json"}},
        "warnings": {"type": "array", "items": {"$ref": "diagnostic.json"}},
    }
    return document


def diagnostic_schema() -> dict[str, Any]:
    entry = cli.SCHEMA_CATALOG["tri.diagnostic"]
    document = data_schema("tri.diagnostic", entry)
    document["$id"] = "diagnostic.json"
    document["properties"] = {
        "code": {"type": "string", "enum": sorted(set(entry["codes"]))},
        "severity": {"type": "string"},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
    }
    return document


def commands_document() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA,
        "$id": "commands.json",
        "title": "Tri command contracts",
        "schema_version": cli.SCHEMA_VERSION,
        "description": "Every CLI command: argv shape (usage), the envelope schema it answers with, and the data schema for that envelope. Errors answer with tri.error.",
        "error_schema": "tri.error",
        "api_blocked_commands": sorted(cli.API_BLOCKED_COMMANDS),
        "commands": [
            {**contract, "data_schema": schema_file(contract["schema"])}
            for contract in cli.COMMAND_CONTRACTS
        ],
    }


def api_document() -> dict[str, Any]:
    base = "http://{host}:{port}"
    endpoints = cli.api_endpoint_catalog("{host}", "{port}")  # type: ignore[arg-type]
    routes = []
    for name, url in endpoints.items():
        path = url.removeprefix(base)
        method = "POST" if name in ("command", "batch") else "GET"
        route: dict[str, Any] = {"name": name, "method": method, "path": path, "authenticated": path.split("?")[0] not in UNAUTHENTICATED_ROUTES}
        if name == "command":
            route["request"] = {"type": "object", "anyOf": [{"required": ["argv"]}, {"required": ["command"]}], "properties": {"argv": {"type": "array", "items": {"type": "string"}}, "actor": {"type": "string"}}}
            route["response_schema"] = "per-command (commands.json) or tri.error"
        elif name == "batch":
            route["request"] = {"type": "object", "anyOf": [{"required": ["commands"]}, {"required": ["batch"]}], "properties": {"commands": {"type": "array", "items": {"type": "object"}}, "continue_on_error": {"type": "boolean"}, "response": {"enum": ["full", "summary"]}, "report": {"enum": ["results", "returns"]}, "actor": {"type": "string"}}}
            route["response_schema"] = "tri.api.batch"
        elif name == "health":
            route["response_schema"] = "tri.api.health"
        elif name == "root":
            route["response_schema"] = "tri.api.server"
        routes.append(route)
    routes.append({"name": "ready", "method": "GET", "path": "/v0/ready", "authenticated": False, "response_schema": "tri.api.ready", "description": "200 when the server can take requests, 503 while it is starting or draining."})
    return {
        "$schema": JSON_SCHEMA,
        "$id": "api.json",
        "title": "Tri HTTP API v0",
        "schema_version": cli.SCHEMA_VERSION,
        "description": "Routes served by `tri serve` and `tri-server`. Every response body is a JSON envelope (envelope.json). GET object routes map onto the command of the same name.",
        "auth": {
            "scheme": "bearer",
            "token_file": "{\"tokens\": [{\"id\", \"actor\", \"sha256\", \"label\", \"created_at\"}]}; sha256 is the hex SHA-256 of the token",
            "unauthenticated_routes": UNAUTHENTICATED_ROUTES,
            "errors": {
                "missing_or_unknown_token": {"status": 401, "code": "unauthenticated"},
                "x_tri_actor_or_payload_actor_differs": {"status": 403, "code": "actor_mismatch", "note": "403 for the X-Tri-Actor header; 200 with a tri.error envelope for a payload or argv actor"},
            },
        },
        "limits": {"max_body_bytes": 10 * 1024 * 1024, "oversize_body": {"status": 200, "code": "invalid_command"}},
        "unknown_route": {"status": 404, "code": "path_not_found"},
        "api_blocked_commands": sorted(cli.API_BLOCKED_COMMANDS),
        "server_paths": "Authenticated or non-loopback servers refuse arguments naming server files for non-maintainers: policy_denied with reason server_path.",
        "routes": routes,
    }


def contract_documents() -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {
        "envelope.json": envelope_schema(),
        "envelope-summary.json": summary_envelope_schema(),
        "diagnostic.json": diagnostic_schema(),
        "commands.json": commands_document(),
        "api.json": api_document(),
    }
    for name, entry in sorted(cli.SCHEMA_CATALOG.items()):
        documents[schema_file(name)] = data_schema(name, entry)
    documents["index.json"] = {
        "contract_version": CONTRACT_VERSION,
        "schema_version": cli.SCHEMA_VERSION,
        "generated_by": "python -m tri.contracts --write",
        "files": sorted(documents),
    }
    return documents


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def drift(directory: Path = DEFAULT_DIR) -> list[str]:
    """Files that are missing, stale, or no longer generated."""
    problems = []
    documents = contract_documents()
    for name, document in documents.items():
        path = directory / name
        if not path.exists():
            problems.append(f"missing: {name}")
            continue
        try:
            on_disk = json.loads(path.read_bytes())
        except json.JSONDecodeError:
            problems.append(f"unparseable: {name}")
            continue
        if on_disk != document:
            problems.append(f"stale: {name}")
    if directory.exists():
        for path in sorted(directory.rglob("*.json")):
            name = path.relative_to(directory).as_posix()
            if name not in documents:
                problems.append(f"no longer generated: {name}")
    return problems


def write(directory: Path = DEFAULT_DIR) -> list[str]:
    documents = contract_documents()
    written = []
    for name, document in documents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = render(document).encode()
        if not path.exists() or path.read_bytes() != text:
            path.write_bytes(text)
            written.append(name)
    for path in sorted(directory.rglob("*.json")):
        if path.relative_to(directory).as_posix() not in documents:
            path.unlink()
            written.append(f"removed {path.relative_to(directory).as_posix()}")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tri.contracts")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        for name in write(args.dir):
            print(name)
        return 0
    problems = drift(args.dir)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print("Contract files drifted from the registries; run `python -m tri.contracts --write`.", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
