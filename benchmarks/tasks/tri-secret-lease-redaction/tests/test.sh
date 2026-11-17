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
(repo / "README.md").write_text("secret workflow\n")

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
workspace = tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"], "--task", "secret lease")["data"]["id"]
secret_value = "s3cr3t-token"
secret_file = workdir / "token.txt"
secret_file.write_text(secret_value)
secret = tri("secrets", "put", "app", "API_TOKEN", "--value-file", str(secret_file))["data"]["id"]
lease = tri("secrets", "request-lease", workspace, secret, "--env", "API_TOKEN")["data"]["id"]

check = tri(
    "run",
    workspace,
    "--with-lease",
    lease,
    "--",
    sys.executable,
    "-c",
    "import os; assert os.environ['API_TOKEN'] == 's3cr3t-token'; print(os.environ['API_TOKEN'])",
)
assert check["data"]["status"] == "passed"
stdout = tri("artifacts", "read", check["data"]["logs"]["stdout"])["data"]["text"]
assert secret_value not in stdout
assert f"[tri-secret-redacted:{lease}]" in stdout

audit = tri("secrets", "audit", "--workspace", workspace)["data"]["events"]
assert audit
assert audit[-1]["kind"] == "secret.lease_granted"

revoked = tri("secrets", "revoke", lease)
assert revoked["data"]["status"] == "revoked"
blocked = tri("run", workspace, "--with-lease", lease, "--", sys.executable, "-c", "print('blocked')", ok=False)
assert blocked["diagnostics"][0]["code"] == "policy_denied"

verifier.mkdir(parents=True, exist_ok=True)
(verifier / "reward.txt").write_text("1\n")
PY
