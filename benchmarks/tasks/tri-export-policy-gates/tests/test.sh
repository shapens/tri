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
workspace = tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"], "--task", "policy gates")["data"]["id"]
tri("files", "write", workspace, "src/app.txt", "--text", "policy edit\n")
unchecked_changeset = tri("changeset", "create", workspace, "--title", "Unchecked policy edit")["data"]["id"]
unchecked_review = tri("review", "create", unchecked_changeset)["data"]["id"]

approval = tri("review", "approve", unchecked_review, ok=False)
assert approval["diagnostics"][0]["code"] == "missing_required_check"
decision = tri("policy", "evaluate", "export", unchecked_changeset, ok=False)
assert decision["data"]["denials"] == ["missing_required_check", "missing_review_approval"]
blocked_export = tri("export", "git-patch", unchecked_changeset, ok=False)
assert blocked_export["diagnostics"][0]["code"] == "missing_required_check"

tri(
    "run",
    workspace,
    "--",
    sys.executable,
    "-c",
    "from pathlib import Path; assert Path('src/app.txt').read_text() == 'policy edit\\n'",
)
checked_changeset = tri("changeset", "create", workspace, "--title", "Checked policy edit")["data"]["id"]
review = tri("review", "create", checked_changeset)["data"]["id"]
tri("review", "approve", review)
allowed = tri("policy", "evaluate", "export", checked_changeset)
assert allowed["data"]["allowed"]
patch_path = workdir / "export.patch"
exported = tri("export", "git-patch", checked_changeset, "--output", str(patch_path), "--summary")
assert exported["ok"]
assert patch_path.exists()
assert "policy edit" in patch_path.read_text()

verifier.mkdir(parents=True, exist_ok=True)
(verifier / "reward.txt").write_text("1\n")
PY
