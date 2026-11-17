#!/usr/bin/env bash
set -euo pipefail

cd "$TRI_BENCH_REPO_ROOT"

python3 - <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path


root = Path(os.environ["TRI_BENCH_REPO_ROOT"])
workdir = Path(os.environ["TRI_BENCH_WORKDIR"])
verifier = Path(os.environ["TRI_BENCH_VERIFIER_DIR"])
repo = workdir / "repo"
repo.mkdir(parents=True, exist_ok=True)
(repo / "app.txt").write_text("base\n")

subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "Tri Bench"], cwd=repo, check=True)
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def tri(*args: str, ok: bool = True) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "tri", *args],
        cwd=root,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"tri {' '.join(args)} did not return JSON\nSTDOUT={result.stdout}\nSTDERR={result.stderr}") from exc
    if ok and result.returncode != 0:
        raise AssertionError(f"tri {' '.join(args)} failed: {data.get('diagnostics')}\nSTDERR={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"tri {' '.join(args)} unexpectedly passed")
    return data


imported = tri("import", str(repo), "--name", "app")
target = imported["data"]["target"]["name"]
base_snapshot = imported["data"]["snapshot"]["id"]

workspace_a = tri("--actor", "agent:alpha", "workspace", "create", "app", "--from", target, "--task", "attempt alpha")["data"]["id"]
workspace_b = tri("--actor", "agent:beta", "workspace", "create", "app", "--from", target, "--task", "attempt beta")["data"]["id"]

tri("--actor", "agent:alpha", "files", "write", workspace_a, "app.txt", "--text", "alpha\n")
tri("--actor", "agent:beta", "files", "write", workspace_b, "app.txt", "--text", "beta\n")

assert tri("--actor", "agent:alpha", "files", "read", workspace_a, "app.txt")["data"]["text"] == "alpha\n"
assert tri("--actor", "agent:beta", "files", "read", workspace_b, "app.txt")["data"]["text"] == "beta\n"
assert tri("files", "read", base_snapshot, "app.txt")["data"]["text"] == "base\n"

changeset_a = tri("--actor", "agent:alpha", "changeset", "create", workspace_a, "--title", "Alpha attempt")["data"]["id"]
changeset_b = tri("--actor", "agent:beta", "changeset", "create", workspace_b, "--title", "Beta attempt")["data"]["id"]
comparison = tri("changeset", "compare", changeset_a, changeset_b)
assert [item["changeset"] for item in comparison["data"]["changesets"]] == [changeset_a, changeset_b]

verifier.mkdir(parents=True, exist_ok=True)
(verifier / "reward.txt").write_text("1\n")
PY
