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
(repo / "private").mkdir(parents=True, exist_ok=True)
(repo / "src").mkdir(parents=True, exist_ok=True)
(repo / "src" / "app.txt").write_text("public\n")
(repo / "private" / "token.txt").write_text("shh-token\n")

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
workspace = tri(
    "--actor",
    "agent:policy",
    "workspace",
    "create",
    "app",
    "--from",
    target,
    "--task",
    "hidden path policy",
    "--hide-path",
    "private/**",
)["data"]["id"]

files = tri("--actor", "agent:policy", "files", "list", workspace)["data"]
paths = {item["path"] for item in files["files"]}
assert "src/app.txt" in paths
assert "private/token.txt" not in paths
assert files["redacted_skipped"] >= 1

search = tri("--actor", "agent:policy", "files", "search", workspace, "shh-token")["data"]
assert search["matches"] == []
assert search["redacted_skipped"] >= 1

tri("--actor", "agent:policy", "files", "write", workspace, "src/app.txt", "--text", "public edit\n")
changeset = tri("--actor", "agent:policy", "changeset", "create", workspace, "--title", "Public edit")["data"]["id"]
diff = tri("--actor", "agent:policy", "diff", changeset, "--mode", "machine")
serialized = json.dumps(diff, sort_keys=True)
assert "private/token.txt" not in serialized
assert "shh-token" not in serialized

verifier.mkdir(parents=True, exist_ok=True)
(verifier / "reward.txt").write_text("1\n")
PY
