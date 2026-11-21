"""Runner autoscaling: queue metrics on the service, a worker supervisor on runner hosts.

The service side (`tri runner metrics`, `GET /v0/runners/metrics`) reports
queue depth, claims per runner, claim-lease expiry, secret grants, and recent
throughput, as JSON or Prometheus text exposition. An external autoscaler can
scale a runner Deployment on it: KEDA's `metrics-api` scaler reads
`data.queued` from the JSON, and a Prometheus adapter reads
`tri_run_jobs_queued`.

The runner side (`tri runner autoscale --min N --max M`) supervises local
`tri runner work` processes against `TRI_REMOTE`. Each pass it reads the
metrics, wants `ceil((queued + busy local workers) / --jobs-per-runner)`
workers clamped to `[--min, --max]`, starts workers up to that number, and
drains workers that have been idle for `--idle-seconds` when it wants fewer.
Draining is cooperative: the supervisor writes a `drain` file in the worker's
control directory and the worker exits before its next claim, so a scale-down
never abandons a claimed job.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATUS_FILE = "status.json"
DRAIN_FILE = "drain"
THROUGHPUT_WINDOW_SECONDS = 300


# -- service-side metrics -------------------------------------------------------------


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def queue_metrics(store: Any) -> dict[str, Any]:
    """Counts only: no job ids, commands, workspaces, or secret names."""
    from tri import cli, runner_secrets

    now = time.time()
    counts = {row["status"]: row["n"] for row in store.rows("SELECT status, COUNT(*) AS n FROM run_jobs GROUP BY status", ())}
    oldest = store.row("SELECT MIN(created_at) AS t FROM run_jobs WHERE status = 'queued'", ())
    oldest_at = _parse_time(oldest["t"] if oldest else None)
    claims = [{"runner": row["runner_actor"], "active": row["n"]} for row in store.rows("SELECT runner_actor, COUNT(*) AS n FROM run_jobs WHERE status = 'claimed' GROUP BY runner_actor ORDER BY runner_actor", ())]
    soonest = store.row("SELECT MIN(lease_expires_at) AS t FROM run_jobs WHERE status = 'claimed'", ())
    soonest_at = _parse_time(soonest["t"] if soonest else None)
    cutoff = datetime.fromtimestamp(now - THROUGHPUT_WINDOW_SECONDS, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    recent = {row["status"]: row["n"] for row in store.rows("SELECT status, COUNT(*) AS n FROM run_jobs WHERE completed_at >= ? GROUP BY status", (cutoff,))}
    waits = [
        wait
        for row in store.rows("SELECT created_at, claimed_at FROM run_jobs WHERE claimed_at IS NOT NULL ORDER BY rowid DESC LIMIT 100", ())
        if (wait := _wait_seconds(row["created_at"], row["claimed_at"])) is not None
    ]
    runner_secrets.ensure_table(store)
    grants = {row["status"]: row["n"] for row in store.rows("SELECT status, COUNT(*) AS n FROM run_job_secrets WHERE status IN ('granted', 'delivered') GROUP BY status", ())}
    queued = int(counts.get("queued", 0))
    claimed = int(counts.get("claimed", 0))
    return {
        "queued": queued,
        "claimed": claimed,
        "completed": int(counts.get("completed", 0)),
        "failed": int(counts.get("failed", 0)),
        "demand": queued + claimed,
        "oldest_queued_age_seconds": round(max(now - oldest_at, 0.0), 3) if oldest_at else 0.0,
        "claims": claims,
        "leases": {
            "active": claimed,
            "lease_seconds": cli.RUNNER_LEASE_SECONDS,
            "soonest_expiry_seconds": round(soonest_at - now, 3) if soonest_at else None,
        },
        "secret_grants": {"pending": int(grants.get("granted", 0)), "delivered": int(grants.get("delivered", 0))},
        "throughput": {
            "window_seconds": THROUGHPUT_WINDOW_SECONDS,
            "completed": int(recent.get("completed", 0)),
            "failed": int(recent.get("failed", 0)),
            "mean_claim_wait_seconds": round(sum(waits) / len(waits), 3) if waits else None,
        },
        "generated_at": cli.utcnow(),
    }


def _wait_seconds(created: str | None, claimed: str | None) -> float | None:
    start, end = _parse_time(created), _parse_time(claimed)
    return max(end - start, 0.0) if start is not None and end is not None else None


def prometheus_text(metrics: dict[str, Any]) -> str:
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, Any]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value}")

    def label(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    metric("tri_run_jobs_queued", "gauge", "Run jobs waiting for a runner.", [("", metrics["queued"])])
    metric("tri_run_jobs_claimed", "gauge", "Run jobs claimed by a runner and not yet completed.", [("", metrics["claimed"])])
    metric("tri_run_jobs_demand", "gauge", "Queued plus claimed run jobs: runners wanted at one job each.", [("", metrics["demand"])])
    metric("tri_run_jobs_completed_total", "counter", "Run jobs that completed with a passing check.", [("", metrics["completed"])])
    metric("tri_run_jobs_failed_total", "counter", "Run jobs that completed with a failing or stale check.", [("", metrics["failed"])])
    metric("tri_run_jobs_oldest_queued_age_seconds", "gauge", "Age of the oldest queued run job.", [("", metrics["oldest_queued_age_seconds"])])
    metric("tri_runner_claims", "gauge", "Active claims per runner identity.", [(f'{{runner="{label(item["runner"] or "")}"}}', item["active"]) for item in metrics["claims"]])
    soonest = metrics["leases"]["soonest_expiry_seconds"]
    metric("tri_run_job_claim_lease_seconds", "gauge", "Claim lease length granted on claim (plus the job timeout).", [("", metrics["leases"]["lease_seconds"])])
    if soonest is not None:
        metric("tri_run_job_claim_lease_soonest_expiry_seconds", "gauge", "Seconds until the next claim lease expires and its job is re-queued.", [("", soonest)])
    metric("tri_run_job_secret_grants", "gauge", "Open secret grants on run jobs, by state.", [('{state="pending"}', metrics["secret_grants"]["pending"]), ('{state="delivered"}', metrics["secret_grants"]["delivered"])])
    window = metrics["throughput"]["window_seconds"]
    metric("tri_run_jobs_recent", "gauge", f"Run jobs finished in the last {window} s, by outcome.", [('{outcome="completed"}', metrics["throughput"]["completed"]), ('{outcome="failed"}', metrics["throughput"]["failed"])])
    return "\n".join(lines) + "\n"


# -- worker control files -------------------------------------------------------------


def write_status(control_dir: Path | None, state: str, **fields: Any) -> None:
    if control_dir is None:
        return
    control_dir.mkdir(parents=True, exist_ok=True)
    tmp = control_dir / f".{STATUS_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"state": state, "since": time.time(), "pid": os.getpid(), **fields}))
    os.replace(tmp, control_dir / STATUS_FILE)


def drain_requested(control_dir: Path | None) -> bool:
    return control_dir is not None and (control_dir / DRAIN_FILE).exists()


# -- supervisor --------------------------------------------------------------------------


def desired_workers(queued: int, busy: int, *, minimum: int, maximum: int, jobs_per_runner: int = 1) -> int:
    demand = max(queued, 0) + max(busy, 0)
    wanted = math.ceil(demand / max(jobs_per_runner, 1))
    return max(minimum, min(maximum, wanted))


@dataclass
class Worker:
    proc: subprocess.Popen
    control_dir: Path
    started_at: float = field(default_factory=time.time)
    draining: bool = False

    def status(self) -> dict[str, Any]:
        try:
            return json.loads((self.control_dir / STATUS_FILE).read_text())
        except (OSError, ValueError):
            return {"state": "starting", "since": self.started_at}

    @property
    def busy(self) -> bool:
        return self.status().get("state") == "busy"

    def idle_for(self, now: float) -> float:
        status = self.status()
        if status.get("state") == "busy":
            return 0.0
        return now - float(status.get("since") or self.started_at)

    def drain(self) -> None:
        self.draining = True
        (self.control_dir / DRAIN_FILE).write_text("drain\n")


def worker_command(control_dir: Path, *, poll: float, sandbox_mode: str | None) -> list[str]:
    argv = [sys.executable, "-m", "tri", "runner", "work", "--interval", f"{poll:g}", "--control-dir", str(control_dir)]
    if sandbox_mode:
        argv += ["--sandbox", sandbox_mode]
    return argv


def worker_env() -> dict[str, str]:
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([package_root, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
    return env


def supervise(
    metrics: Callable[[], dict[str, Any]],
    *,
    minimum: int,
    maximum: int,
    jobs_per_runner: int = 1,
    idle_seconds: float = 30.0,
    interval: float = 2.0,
    poll: float = 1.0,
    sandbox_mode: str | None = None,
    duration: float | None = None,
    exit_when_idle: bool = False,
    spawn: Callable[[Path], subprocess.Popen] | None = None,
) -> dict[str, Any]:
    """Run the scaling loop until --duration, --exit-when-idle, or SIGTERM/SIGINT; then drain every worker."""
    if minimum < 0 or maximum < max(minimum, 1):
        from tri.cli import TriError

        raise TriError("invalid_command", "--max must be at least 1 and at least --min.", min=minimum, max=maximum)
    root = Path(tempfile.mkdtemp(prefix="tri-autoscale-"))
    workers: list[Worker] = []
    decisions: list[dict[str, Any]] = []
    stop = threading.Event()
    reason = "duration"
    counter = 0
    started = drained = peak = 0
    errors = 0
    begin = time.time()

    def default_spawn(control_dir: Path) -> subprocess.Popen:
        log = open(control_dir / "worker.log", "wb")
        try:
            return subprocess.Popen(worker_command(control_dir, poll=poll, sandbox_mode=sandbox_mode), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=worker_env())
        finally:
            log.close()

    launch = spawn or default_spawn
    previous_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
            except (ValueError, OSError):
                pass
    try:
        while True:
            now = time.time()
            workers = [worker for worker in workers if worker.proc.poll() is None]
            if stop.is_set():
                reason = "signal"
                break
            if duration is not None and now - begin >= duration:
                reason = "duration"
                break
            snapshot = metrics()
            if not snapshot.get("ok", True) or "queued" not in snapshot:
                errors += 1
                decisions.append({"at": round(now - begin, 3), "action": "error", "detail": snapshot.get("error")})
                if errors >= 5:
                    reason = "metrics_unavailable"
                    break
                stop.wait(interval)
                continue
            errors = 0
            active = [worker for worker in workers if not worker.draining]
            busy = sum(1 for worker in active if worker.busy)
            queued = int(snapshot["queued"])
            want = desired_workers(queued, busy, minimum=minimum, maximum=maximum, jobs_per_runner=jobs_per_runner)
            action, count = "hold", 0
            if len(active) < want:
                for _ in range(want - len(active)):
                    counter += 1
                    control_dir = root / f"worker-{counter}"
                    control_dir.mkdir()
                    workers.append(Worker(proc=launch(control_dir), control_dir=control_dir))
                    started += 1
                action, count = "start", want - len(active)
            elif len(active) > want:
                surplus = len(active) - want
                for worker in sorted(active, key=lambda item: -item.idle_for(now)):
                    if surplus == 0:
                        break
                    if not worker.busy and worker.idle_for(now) >= idle_seconds:
                        worker.drain()
                        drained += 1
                        surplus -= 1
                        count += 1
                if count:
                    action = "drain"
            live = [worker for worker in workers if not worker.draining]
            peak = max(peak, len(live))
            if action != "hold" or not decisions or decisions[-1].get("desired") != want:
                decisions.append({"at": round(now - begin, 3), "queued": queued, "busy": busy, "workers": len(live), "desired": want, "action": action, "count": count})
            draining_alive = [worker for worker in workers if worker.draining and worker.proc.poll() is None]
            if exit_when_idle and queued == 0 and busy == 0 and len(live) <= minimum and not draining_alive:
                reason = "idle"
                break
            stop.wait(interval)
    finally:
        for worker in workers:
            if worker.proc.poll() is None and not worker.draining:
                worker.drain()
                drained += 1
        deadline = time.time() + max(poll * 3, 5) + 30
        for worker in workers:
            remaining = max(deadline - time.time(), 0.1)
            try:
                worker.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                worker.proc.terminate()
                try:
                    worker.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.proc.kill()
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        shutil.rmtree(root, ignore_errors=True)
    return {
        "min": minimum,
        "max": maximum,
        "jobs_per_runner": jobs_per_runner,
        "idle_seconds": idle_seconds,
        "decisions": decisions[-200:],
        "peak_workers": peak,
        "started": started,
        "drained": drained,
        "workers": sum(1 for worker in workers if worker.proc.poll() is None),
        "stopped_because": reason,
        "duration_seconds": round(time.time() - begin, 3),
    }
