"""Secret leases for remote run jobs.

`tri run --runner remote --with-lease <lease>` lets a queued job carry a secret
only when the requester could use it on a local run: the lease is active, not
expired, bound to the requester (or the requester is a maintainer), and policy
still grants the requester `secret.use` on the secret. The grant is recorded
per job (`run_job.secret_granted`), never with the value.

Values leave the service only through `tri runner secrets <job>`, which answers
just the runner that holds the job's claim, while that claim is live. Policy
and lease expiry are checked again at delivery, and each delivery is an event
(`run_job.secret_delivered`). Delivery ends when the job completes
(`run_job.secrets_released`) or its claim lease expires and the job is
re-queued (`run_job.secret_expired`); a new claim needs a new delivery.

The runner keeps values in memory and in the job's environment only. On
`tri runner complete` the service redacts every leased value from stdout and
stderr before they are stored, and refuses to import a written file that
contains one, so a value cannot reach logs, artifacts, or source history.
"""

from __future__ import annotations

import base64
import sqlite3
from typing import Any

TABLE = """
CREATE TABLE IF NOT EXISTS run_job_secrets (
  job_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  secret_id TEXT NOT NULL,
  env_name TEXT NOT NULL,
  status TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  granted_at TEXT NOT NULL,
  delivered_to TEXT,
  delivered_claim TEXT,
  delivered_at TEXT,
  deliveries INTEGER NOT NULL DEFAULT 0,
  closed_at TEXT,
  PRIMARY KEY (job_id, lease_id)
)
"""


def ensure_table(store: Any) -> None:
    store.conn.execute(TABLE)


def validate_grants(store: Any, ws: sqlite3.Row, lease_ids: list[str], *, actor: str) -> list[dict[str, Any]]:
    """The leases a queued job may carry, checked as strictly as a local run checks them."""
    from tri import cli

    if not lease_ids:
        return []
    leases, _, _ = cli.active_secret_leases(store, ws["id"], list(dict.fromkeys(lease_ids)), actor=actor)
    for lease in leases:
        cli.require_object_access(store, lease["secret"], actor=actor, action="secret.use", requested=lease["id"])
    return leases


def record_grants(store: Any, job_id: str, ws: sqlite3.Row, leases: list[dict[str, Any]], *, actor: str) -> list[str]:
    """Insert grant rows and their audit events; the caller holds the transaction."""
    from tri import cli

    ensure_table(store)
    events = []
    for lease in leases:
        store.conn.execute(
            "INSERT INTO run_job_secrets (job_id, lease_id, secret_id, env_name, status, granted_by, granted_at) VALUES (?, ?, ?, ?, 'granted', ?, ?)",
            (job_id, lease["id"], lease["secret"], lease["env"], actor, cli.utcnow()),
        )
        events.append(
            store.create_event(
                kind="run_job.secret_granted",
                actor=actor,
                project_id=ws["project_id"],
                workspace_id=ws["id"],
                object_refs={"job": job_id, "lease": lease["id"], "secret": lease["secret"], "env": lease["env"]},
                policy_result={"allowed": True, "scope": "run_job", "lease_expires_at": lease["expires_at"]},
            )
        )
    return events


def job_grants(store: Any, job_id: str) -> list[sqlite3.Row]:
    ensure_table(store)
    return store.rows("SELECT * FROM run_job_secrets WHERE job_id = ? ORDER BY rowid", (job_id,))


def grant_summaries(store: Any, job_id: str) -> list[dict[str, Any]]:
    """What a job's record shows about its secrets: lease ids, env names, status. Never values."""
    return [{"lease": row["lease_id"], "env": row["env_name"], "status": row["status"], "deliveries": row["deliveries"]} for row in job_grants(store, job_id)]


def deliver(store: Any, job: sqlite3.Row, *, actor: str) -> tuple[dict[str, Any], list[str]]:
    """Values for the runner holding this job's live claim."""
    from tri import cli

    if job["status"] != "claimed" or job["runner_actor"] != actor:
        raise cli.TriError("policy_denied", "Secrets go only to the runner that holds this job's claim.", object=job["id"], status=job["status"], reason="not_claim_holder")
    now = cli.utcnow()
    if not job["lease_expires_at"] or job["lease_expires_at"] <= now:
        raise cli.TriError("policy_denied", "This job's claim lease has expired; its secrets are no longer delivered.", object=job["id"], expired_at=job["lease_expires_at"], reason="claim_expired")
    grants = job_grants(store, job["id"])
    env: dict[str, str] = {}
    leases: list[str] = []
    for grant in grants:
        if grant["status"] not in ("granted", "delivered"):
            raise cli.TriError("policy_denied", "This job's secret grant is closed.", object=job["id"], lease=grant["lease_id"], status=grant["status"], reason="grant_closed")
        # Re-check at delivery: the lease may have expired or been revoked, and
        # the requester may have lost secret.use since the job was queued.
        _, lease_env, _ = cli.active_secret_leases(store, job["workspace_id"], [grant["lease_id"]], actor=job["actor"])
        cli.require_object_access(store, grant["secret_id"], actor=job["actor"], action="secret.use", requested=grant["lease_id"])
        env.update(lease_env)
        leases.append(grant["lease_id"])
    events: list[str] = []
    if grants:
        with store.conn:
            store.conn.execute(
                "UPDATE run_job_secrets SET status = 'delivered', delivered_to = ?, delivered_claim = ?, delivered_at = ?, deliveries = deliveries + 1 WHERE job_id = ?",
                (actor, job["claimed_at"], now, job["id"]),
            )
            events.append(
                store.create_event(
                    kind="run_job.secret_delivered",
                    actor=actor,
                    project_id=job["project_id"],
                    workspace_id=job["workspace_id"],
                    object_refs={"job": job["id"], "leases": leases, "env": sorted(env), "runner": actor, "claim_expires_at": job["lease_expires_at"]},
                    policy_result={"allowed": True, "scope": "run_job_claim", "requested_by": job["actor"]},
                )
            )
    data = {"job": job["id"], "env": env, "leases": leases, "expires_at": job["lease_expires_at"], "redaction": "values are redacted from stdout and stderr on runner complete"}
    return data, events


def redactions(store: Any, job_id: str) -> list[tuple[str, bytes]]:
    from tri import cli

    lease_ids = [row["lease_id"] for row in job_grants(store, job_id)]
    return cli.secret_redactions_for_leases(store, lease_ids) if lease_ids else []


def redact(data: bytes, values: list[tuple[str, bytes]]) -> bytes:
    from tri import cli

    return cli.redact_secret_bytes(data, values)


def written_secret_paths(written: list[dict[str, Any]], values: list[tuple[str, bytes]]) -> set[str]:
    """Paths of reported file writes that carry a leased value."""
    if not values:
        return set()
    hits = set()
    for item in written:
        content = base64.b64decode(item.get("content") or "")
        if any(value and value in content for _, value in values):
            hits.add(item["path"])
    return hits


def release(store: Any, job: sqlite3.Row, *, actor: str, status: str = "completed") -> list[str]:
    """Close a job's grants; the caller holds the transaction."""
    from tri import cli

    grants = job_grants(store, job["id"])
    if not grants:
        return []
    store.conn.execute("UPDATE run_job_secrets SET status = ?, closed_at = ? WHERE job_id = ? AND status IN ('granted', 'delivered')", (status, cli.utcnow(), job["id"]))
    return [
        store.create_event(
            kind="run_job.secrets_released",
            actor=actor,
            project_id=job["project_id"],
            workspace_id=job["workspace_id"],
            object_refs={"job": job["id"], "leases": [row["lease_id"] for row in grants], "status": status},
        )
    ]


def expire_claims(store: Any, jobs: list[sqlite3.Row]) -> None:
    """A claim lease ran out: what was delivered under it is no longer valid; the next claim fetches again."""
    ensure_table(store)
    for job in jobs:
        delivered = store.rows("SELECT lease_id FROM run_job_secrets WHERE job_id = ? AND status = 'delivered'", (job["id"],))
        if not delivered:
            continue
        store.conn.execute("UPDATE run_job_secrets SET status = 'granted', delivered_to = NULL, delivered_claim = NULL WHERE job_id = ? AND status = 'delivered'", (job["id"],))
        store.create_event(
            kind="run_job.secret_expired",
            actor=job["runner_actor"] or "actor_local",
            project_id=job["project_id"],
            workspace_id=job["workspace_id"],
            object_refs={"job": job["id"], "leases": [row["lease_id"] for row in delivered], "runner": job["runner_actor"], "claim_expired_at": job["lease_expires_at"]},
        )
