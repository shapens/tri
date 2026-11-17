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
(repo / "src").mkdir(parents=True, exist_ok=True)
(repo / "src" / "app.txt").write_text("base\n")

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
    data = json.loads(result.stdout)
    if ok and result.returncode != 0:
        raise AssertionError(f"tri {' '.join(args)} failed: {data.get('diagnostics')}\nSTDERR={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"tri {' '.join(args)} unexpectedly passed")
    return data


imported = tri("import", str(repo), "--name", "app")
target = imported["data"]["target"]["name"]
workspace = tri("workspace", "create", "app", "--from", target, "--task", "conflict resolution")["data"]["id"]
tri("files", "write", workspace, "src/app.txt", "--text", "workspace edit\n")

(repo / "src" / "app.txt").write_text("target edit\n")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "target edit"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
tri("targets", "refresh", "app", target)

conflict = tri("workspace", "update", workspace, "--from", target, ok=False)
assert conflict["diagnostics"][0]["code"] == "conflict_required"
conflict_id = conflict["data"]["conflict"]
entry = conflict["data"]["conflicts"][0]
assert entry["path"] == "src/app.txt"
assert entry["kind"] == "file_text_conflict"

listed = tri("conflicts", "list", "--workspace", workspace)["data"]
assert listed["conflicts"][0]["id"] == conflict_id
inspected = tri("conflicts", "inspect", conflict_id)["data"]
assert inspected["status"] == "open"

tri("conflicts", "resolve", conflict_id, "--strategy", "theirs")
assert tri("conflicts", "inspect", conflict_id)["data"]["status"] == "resolved"
assert tri("files", "read", workspace, "src/app.txt")["data"]["text"] == "target edit\n"

verifier.mkdir(parents=True, exist_ok=True)
(verifier / "reward.txt").write_text("1\n")
PY
