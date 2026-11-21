"""Contention benchmark: N agents edit shared files and merge to main at once —
Tri line merges versus Tri path-level merges versus Git pull-rebase-push.

It measures the two contention rows of the Agent-Scale Scorecard in ``tri.md``:
merges per hour under contention, and conflicts per 1,000 merges.

Each agent starts from the current main, reads a few shared files, rewrites a
few lines in each, runs a trivial check, and merges to main through a merge
rule (no human review). Most edits land in disjoint line regions of the shared
files; an ``--overlap`` fraction of agents deliberately rewrites the same lines
as another agent running at about the same time, which is a real conflict for
every target.

Targets:
- ``tri``: ``tri changeset merge --retry N`` with line-level three-way merges.
- ``tri-path``: the same, with line merges switched off (``TRI_MERGE_TEXT=0``),
  so any two changesets touching one file conflict (path-level merging).
- ``git``: a bare repository; each agent clones it, commits, and loops
  ``git pull --rebase`` + ``git push`` until the push lands or the same retry
  budget runs out.

After each target, final main is checked: every successful agent's edit must be
present (unless a later agent read it and knowingly rewrote it), conflicted
agents' edits must be absent, and every other line must be the base line.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "tri.benchmark.contention.v1"
GAP = 2  # unchanged lines between two agents' regions, so disjoint edits never touch

CHECK_SCRIPT = '''\
from pathlib import Path
files = sorted(Path("src").glob("file*.txt"))
assert files, "no shared files"
print("ok", len(files))
'''


# ---------------------------------------------------------------------------
# Fixture and edit plan


def file_path(index: int) -> str:
    return f"src/file{index:03d}.txt"


def base_line(file: int, line: int) -> str:
    return f"file{file:03d} line{line:04d} base"


def agent_line(file: int, line: int, agent: int) -> str:
    return f"file{file:03d} line{line:04d} agent{agent:04d}"


def make_fixture(repo: Path, *, files: int, lines: int) -> None:
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "tools").mkdir(exist_ok=True)
    for f in range(files):
        (repo / file_path(f)).write_text("".join(base_line(f, n) + "\n" for n in range(lines)))
    (repo / "tools" / "check.py").write_text(CHECK_SCRIPT)
    run(["git", "init", "-q", "-b", "main"], repo)
    git_identity(repo)
    run(["git", "add", "."], repo)
    run(["git", "commit", "-q", "-m", "fixture"], repo)


def git_identity(repo: Path) -> None:
    run(["git", "config", "user.email", "bench@example.com"], repo)
    run(["git", "config", "user.name", "Bench"], repo)


@dataclass
class Region:
    file: int
    start: int
    length: int

    def lines(self) -> range:
        return range(self.start, self.start + self.length)


@dataclass
class Agent:
    index: int
    regions: list[Region]
    overlaps: int | None = None  # agent whose first region this one deliberately reuses


def plan_agents(args: argparse.Namespace) -> list[Agent]:
    """Deterministic edit plan: disjoint regions for everyone, except overlap
    agents whose first region copies a recent agent's region exactly."""
    stride = args.edit_lines + GAP
    slots = [Region(f, s * stride, args.edit_lines) for f in range(args.files) for s in range(args.lines // stride)]
    rng = random.Random(args.seed)
    rng.shuffle(slots)
    overlap_count = min(round(args.agents * args.overlap), max(args.agents - 1, 0))
    overlappers = set(rng.sample(range(1, args.agents), overlap_count)) if overlap_count else set()
    agents: list[Agent] = []
    for index in range(args.agents):
        regions: list[Region] = []
        overlaps = None
        if index in overlappers:
            # Pick a victim that starts close by (inside one parallel window) so the
            # two edits race instead of running one after the other.
            window = [a for a in agents[max(0, index - args.parallel) : index] if a.overlaps is None] or [a for a in agents if a.overlaps is None]
            victim = rng.choice(window)
            regions.append(victim.regions[0])
            overlaps = victim.index
        used_files = {r.file for r in regions}
        while len(regions) < args.edits_per_agent:
            if not slots:
                raise SystemExit("not enough disjoint regions: raise --files/--lines or lower --agents/--edits-per-agent")
            pick = next((i for i, r in enumerate(slots) if r.file not in used_files), 0)
            region = slots.pop(pick)
            used_files.add(region.file)
            regions.append(region)
        agents.append(Agent(index, regions, overlaps))
    return agents


def apply_edit(text: str, agent: Agent, path_file: int) -> tuple[str, dict[int, str]]:
    """Rewrite this agent's lines in one file; returns the new text and what each
    rewritten line held before (to tell a knowing rewrite from a lost update)."""
    lines = text.split("\n")
    seen: dict[int, str] = {}
    for region in agent.regions:
        if region.file != path_file:
            continue
        for n in region.lines():
            seen[n] = lines[n]
            lines[n] = agent_line(region.file, n, agent.index)
    return "\n".join(lines), seen


# ---------------------------------------------------------------------------
# Running one agent


@dataclass
class Outcome:
    agent: int
    status: str = "error"  # merged | conflicted | exhausted | error
    retries: int = 0
    merge_s: float | None = None
    agent_s: float = 0.0
    text_merged: bool = False
    mode: str | None = None
    seen: dict[tuple[int, int], str] = field(default_factory=dict)
    error: str | None = None


def run(argv: list[str], cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:6])} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:2000]}")
    return proc


def tri_call(env: dict[str, str], *argv: str) -> dict[str, Any]:
    proc = subprocess.run([sys.executable, "-m", "tri", *argv], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tri {' '.join(argv[:4])} returned non-JSON (exit {proc.returncode}): {proc.stderr.strip()[-2000:]}") from exc


def tri_ok(env: dict[str, str], *argv: str) -> dict[str, Any]:
    payload = tri_call(env, *argv)
    if not payload.get("ok"):
        raise RuntimeError(f"tri {' '.join(argv[:4])} failed: {json.dumps(payload.get('diagnostics'))[:2000]}")
    return payload["data"]


class TriTarget:
    def __init__(self, workdir: Path, args: argparse.Namespace, *, line_merges: bool) -> None:
        self.args = args
        self.repo = workdir / "repo"
        make_fixture(self.repo, files=args.files, lines=args.lines)
        self.env = os.environ.copy()
        self.env["TRI_HOME"] = str(workdir / "tri-home")
        self.env.pop("TRI_ACTOR", None)
        self.env["TRI_AUTO_REAP"] = "0"
        self.env["TRI_MERGE_TEXT"] = "1" if line_merges else "0"
        tri_ok(self.env, "import", str(self.repo), "--name", "app")
        tri_ok(self.env, "merge-rules", "add", "app", "agents", "--path", "src/*", "--max-files", str(max(args.edits_per_agent, 1)))

    def agent(self, agent: Agent, outcome: Outcome) -> None:
        actor = ["--actor", f"agent:{agent.index}"]
        ws = tri_ok(self.env, *actor, "workspace", "create", "app", "--from", "main")["id"]
        for f in sorted({r.file for r in agent.regions}):
            text = tri_ok(self.env, *actor, "files", "read", ws, file_path(f))["text"]
            new_text, seen = apply_edit(text, agent, f)
            outcome.seen.update({(f, n): line for n, line in seen.items()})
            tri_ok(self.env, *actor, "files", "write", ws, file_path(f), "--text", new_text)
        check = tri_ok(self.env, *actor, "run", ws, "--", sys.executable, "tools/check.py")
        if check["status"] != "passed":
            raise RuntimeError(f"check {check['status']}")
        cs = tri_ok(self.env, *actor, "changeset", "create", ws, "--title", f"agent {agent.index}")["id"]
        started = time.perf_counter()
        payload = tri_call(self.env, *actor, "changeset", "merge", cs, "--target", "main", "--retry", str(self.args.retry))
        outcome.merge_s = time.perf_counter() - started
        data = payload.get("data") or {}
        codes = [d.get("code") for d in payload.get("diagnostics", [])]
        if payload.get("ok"):
            outcome.status = "merged"
            outcome.retries = data.get("merge_attempts", 1) - 1
            outcome.text_merged = bool(data.get("text_merged_paths"))
            outcome.mode = data.get("mode")
        elif "conflict_required" in codes:
            outcome.status = "conflicted"
            outcome.retries = data.get("merge_attempts", 1) - 1
        elif "stale_target_snapshot" in codes:
            outcome.status = "exhausted"
            outcome.retries = self.args.retry
        else:
            raise RuntimeError(f"merge failed: {json.dumps(payload.get('diagnostics'))[:2000]}")

    def final_files(self) -> dict[int, str]:
        snapshot = tri_ok(self.env, "targets", "list", "app")["targets"][0]["snapshot"]
        return {f: tri_ok(self.env, "files", "read", snapshot, file_path(f))["text"] for f in range(self.args.files)}


class GitTarget:
    def __init__(self, workdir: Path, args: argparse.Namespace) -> None:
        self.args = args
        self.workdir = workdir
        seed = workdir / "seed"
        make_fixture(seed, files=args.files, lines=args.lines)
        self.bare = workdir / "main.git"
        run(["git", "clone", "-q", "--bare", str(seed), str(self.bare)], workdir)

    def agent(self, agent: Agent, outcome: Outcome) -> None:
        clone = self.workdir / "agents" / f"agent{agent.index}"
        run(["git", "clone", "-q", str(self.bare), str(clone)], self.workdir)
        git_identity(clone)
        for f in sorted({r.file for r in agent.regions}):
            path = clone / file_path(f)
            new_text, seen = apply_edit(path.read_text(), agent, f)
            outcome.seen.update({(f, n): line for n, line in seen.items()})
            path.write_text(new_text)
        run([sys.executable, "tools/check.py"], clone)
        run(["git", "commit", "-qam", f"agent {agent.index}"], clone)
        started = time.perf_counter()
        attempts = 0
        while True:
            push = run(["git", "push", "-q", "origin", "HEAD:main"], clone, check=False)
            if push.returncode == 0:
                outcome.status = "merged"
                break
            attempts += 1
            if attempts > self.args.retry:
                outcome.status = "exhausted"
                attempts = self.args.retry
                break
            time.sleep(min(0.05 * attempts, 0.5))  # the same backoff `tri changeset merge` uses
            pull = run(["git", "pull", "-q", "--rebase", "origin", "main"], clone, check=False)
            if pull.returncode != 0:
                run(["git", "rebase", "--abort"], clone, check=False)
                fetch_failed = "CONFLICT" not in (pull.stdout + pull.stderr)
                if fetch_failed:
                    raise RuntimeError(f"git pull --rebase failed: {pull.stderr.strip()[:2000]}")
                outcome.status = "conflicted"
                break
        outcome.merge_s = time.perf_counter() - started
        outcome.retries = attempts

    def final_files(self) -> dict[int, str]:
        return {f: run(["git", "--git-dir", str(self.bare), "show", f"main:{file_path(f)}"], self.workdir).stdout for f in range(self.args.files)}


TARGETS: dict[str, Callable[[Path, argparse.Namespace], Any]] = {
    "tri": lambda workdir, args: TriTarget(workdir, args, line_merges=True),
    "tri-path": lambda workdir, args: TriTarget(workdir, args, line_merges=False),
    "git": GitTarget,
}


# ---------------------------------------------------------------------------
# Verification and metrics


def verify(final: dict[int, str], agents: list[Agent], outcomes: list[Outcome], args: argparse.Namespace) -> dict[str, Any]:
    """Every line of final main must be either the base line, or the edit of a
    merged agent that no later merged agent knowingly rewrote."""
    merged = {o.agent for o in outcomes if o.status == "merged"}
    by_agent = {o.agent: o for o in outcomes}
    claims: dict[tuple[int, int], list[int]] = {}
    for agent in agents:
        if agent.index in merged:
            for region in agent.regions:
                for n in region.lines():
                    claims.setdefault((region.file, n), []).append(agent.index)
    lost_agents: set[int] = set()
    mismatched_lines = 0
    line_count_errors = 0
    for f in range(args.files):
        lines = final.get(f, "").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if len(lines) != args.lines:
            line_count_errors += 1
        for n in range(args.lines):
            actual = lines[n] if n < len(lines) else None
            owners = claims.get((f, n), [])
            # An owner is superseded when another merged owner read its line before rewriting it.
            live = [a for a in owners if not any(by_agent[b].seen.get((f, n)) == agent_line(f, n, a) for b in owners if b != a)]
            expected = agent_line(f, n, live[0]) if len(live) == 1 else (base_line(f, n) if not owners else None)
            if len(live) > 1:
                lost_agents.update(a for a in live if actual != agent_line(f, n, a))
            if actual != expected:
                mismatched_lines += 1
                lost_agents.update(a for a in live if actual != agent_line(f, n, a))
    present = len(merged - lost_agents)
    return {
        "merged_edits_present": present,
        "merged_edits_missing": sorted(lost_agents),
        "mismatched_lines": mismatched_lines,
        "files_with_wrong_line_count": line_count_errors,
        "ok": not lost_agents and mismatched_lines == 0 and line_count_errors == 0,
    }


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[rank]


def run_target(name: str, workdir: Path, agents: list[Agent], args: argparse.Namespace) -> dict[str, Any]:
    target_dir = workdir / name
    target_dir.mkdir(parents=True)
    try:
        target = TARGETS[name](target_dir, args)
    except Exception as exc:
        return {"target": name, "status": "failed", "error": f"setup: {exc}"}
    outcomes: list[Outcome] = []
    lock = threading.Lock()

    def one(agent: Agent) -> None:
        outcome = Outcome(agent.index)
        started = time.perf_counter()
        try:
            target.agent(agent, outcome)
        except Exception as exc:  # recorded per agent; the run continues
            outcome.status = "error"
            outcome.error = str(exc)[:2000]
        outcome.agent_s = time.perf_counter() - started
        with lock:
            outcomes.append(outcome)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        list(pool.map(one, agents))
    wall = time.perf_counter() - started
    outcomes.sort(key=lambda o: o.agent)
    correctness = verify(target.final_files(), agents, outcomes, args)
    counts = {status: sum(1 for o in outcomes if o.status == status) for status in ("merged", "conflicted", "exhausted", "error")}
    attempted = counts["merged"] + counts["conflicted"] + counts["exhausted"]
    latencies = [o.merge_s for o in outcomes if o.merge_s is not None]
    overlapping = {a.index for a in agents if a.overlaps is not None} | {a.overlaps for a in agents if a.overlaps is not None}
    return {
        "target": name,
        "status": "ok" if counts["error"] == 0 and correctness["ok"] else "failed",
        "agents": len(agents),
        "merges_attempted": attempted,
        "merges_succeeded": counts["merged"],
        "merges_conflicted": counts["conflicted"],
        "merges_retry_exhausted": counts["exhausted"],
        "agent_errors": counts["error"],
        "retries": sum(o.retries for o in outcomes),
        "text_merges": sum(1 for o in outcomes if o.text_merged),
        "conflicts_outside_overlap": sum(1 for o in outcomes if o.status == "conflicted" and o.agent not in overlapping),
        "wall_s": round(wall, 3),
        "merges_per_hour": round(counts["merged"] / wall * 3600, 1) if wall > 0 else 0.0,
        "conflicts_per_1000_merges": round(counts["conflicted"] / attempted * 1000, 1) if attempted else 0.0,
        "merge_latency_p50_s": round(percentile(latencies, 50) or 0, 3),
        "merge_latency_p95_s": round(percentile(latencies, 95) or 0, 3),
        "agent_seconds_p50": round(percentile([o.agent_s for o in outcomes], 50) or 0, 3),
        "correctness": correctness,
        "errors": [{"agent": o.agent, "error": o.error} for o in outcomes if o.error][:10],
        "outcomes": [{"agent": o.agent, "status": o.status, "retries": o.retries, "merge_s": round(o.merge_s, 3) if o.merge_s is not None else None, "mode": o.mode, "text_merged": o.text_merged} for o in outcomes],
    }


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    params = payload["parameters"]
    lines = [
        "# Contention benchmark",
        "",
        f"- Created: `{payload['created_at']}`",
        f"- Platform: `{payload['platform']}`",
        f"- Agents: `{params['agents']}` (`{params['parallel']}` at a time); retry budget `{params['retry']}`",
        f"- Fixture: `{params['files']}` shared files x `{params['lines']}` lines; each agent rewrites `{params['edit_lines']}` lines in each of `{params['edits_per_agent']}` files",
        f"- Overlap: `{params['overlap']}` of agents rewrite the same lines as another agent (`{payload['overlap_agents']}` agents); seed `{params['seed']}`",
        "",
        "| Target | Status | Attempted | Merged | Conflicted | Retry-exhausted | Retries | Wall s | Merges/hour | Conflicts per 1,000 | p50 merge s | p95 merge s | Correct |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in payload["results"]:
        if "merges_attempted" not in item:
            lines.append(f"| {item['target']} | {item['status']} | error: {item.get('error', '')[:200]} |  |  |  |  |  |  |  |  |  |  |")
            continue
        correct = "yes" if item["correctness"]["ok"] else f"no ({item['correctness']['mismatched_lines']} lines, missing {item['correctness']['merged_edits_missing'][:5]})"
        lines.append(
            f"| {item['target']} | {item['status']} | {item['merges_attempted']} | {item['merges_succeeded']} | {item['merges_conflicted']} | {item['merges_retry_exhausted']} | "
            f"{item['retries']} | {item['wall_s']} | {item['merges_per_hour']} | {item['conflicts_per_1000_merges']} | {item['merge_latency_p50_s']} | {item['merge_latency_p95_s']} | {correct} |"
        )
        if item["agent_errors"]:
            lines.append(f"|  | {item['agent_errors']} agent errors: {item['errors'][0]['error'][:200]} |  |  |  |  |  |  |  |  |  |  |  |")
    lines += [
        "",
        "Merges/hour is successful merges over the wall time of the whole agent phase (workspace, edits, check, merge), not only the merge call. "
        "Conflicts per 1,000 is conflicted merges over attempted merges. Merge latency is the merge call alone (Tri: `tri changeset merge` including its retries; "
        "Git: the `git push` / `git pull --rebase` loop). `Correct` means final main holds every merged agent's edit and every other line is untouched.",
        "",
    ]
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tri-bench-contention", description=__doc__.split("\n\n")[0])
    parser.add_argument("--agents", type=int, default=50)
    parser.add_argument("--parallel", type=int, default=8, help="agents running at once")
    parser.add_argument("--files", type=int, default=20, help="shared files in the fixture")
    parser.add_argument("--lines", type=int, default=200, help="lines per shared file")
    parser.add_argument("--edits-per-agent", type=int, default=2, help="files each agent edits")
    parser.add_argument("--edit-lines", type=int, default=3, help="consecutive lines rewritten per edited file")
    parser.add_argument("--overlap", type=float, default=0.1, help="fraction of agents that rewrite the same lines as another agent")
    parser.add_argument("--retry", type=int, default=5, help="retry budget on a moved target (tri --retry; git push attempts)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--targets", default="tri,tri-path,git")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.overlap <= 1:
        parser.error("--overlap must be between 0 and 1")
    if args.agents < 1 or args.edits_per_agent < 1 or args.edit_lines < 1:
        parser.error("--agents, --edits-per-agent and --edit-lines must be positive")
    if args.edits_per_agent > args.files:
        parser.error("--edits-per-agent cannot exceed --files")
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        parser.error(f"unknown targets: {unknown}; available: {sorted(TARGETS)}")
    agents = plan_agents(args)
    output = args.output or ROOT / "benchmark-results" / f"contention-{time.strftime('%Y%m%d-%H%M%S')}"
    output.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="tri-contention-"))
    try:
        results = [run_target(target, workdir, agents, args) for target in targets]
    finally:
        if args.keep_workdir:
            print(f"workdir kept at {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    payload = {
        "schema": SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "parameters": {k: getattr(args, k) for k in ("agents", "parallel", "files", "lines", "edits_per_agent", "edit_lines", "overlap", "retry", "seed")},
        "overlap_agents": sum(1 for a in agents if a.overlaps is not None),
        "results": results,
    }
    (output / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    write_summary(output / "summary.md", payload)
    print((output / "summary.md").read_text())


if __name__ == "__main__":
    main()
