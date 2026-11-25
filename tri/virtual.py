"""Tri integration for virtual checkouts (``tri workspace mount --virtual``).

The view itself lives in ``tri.vfs`` (layers, sources, daemon) and the Linux
kernel protocol in ``tri.fuse``. This module owns the Tri side:

- mounting creates an ordinary materialization lease with backend ``fuse``,
  writes the manifest of visible entries (the same filter ``files list``
  applies), and starts the daemon (detached, or ``--foreground``);
- ``tri workspace sync`` records the upper layer as workspace edits with the
  same stale-head refusal as a materialized mount;
- ``tri workspace unmount`` stops the daemon and releases the lease, refusing
  while there are unsynced edits unless ``--force`` (they become a salvage);
- the reaper treats a lease whose daemon died as orphaned: it unmounts the
  view (``fusermount3 -u -z``), salvages unsynced edits, and removes the state.

Remote mode (``TRI_REMOTE``) uses the same daemon with blobs fetched through
the per-machine cache; sync uploads the upper layer in one guarded batch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tri import fuse, vfs

BACKEND = "fuse"
READY_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 15.0
STARTUP_GRACE_SECONDS = 120.0
_DAEMONS: list[subprocess.Popen] = []  # kept so a detached daemon's Popen is not collected (and warned about) while it serves
FALLBACK_HINT = "Use a partial checkout instead: tri workspace mount <workspace> <dir> --paths <glob>."


# -- availability -------------------------------------------------------------------


def availability(*, deep: bool = False) -> dict[str, Any]:
    """Which virtual-checkout backend this host has, for doctor and for refusals."""
    if sys.platform.startswith("linux"):
        info = fuse.probe(deep=deep)
        if not info["available"]:
            info["message"] = {
                "no_dev_fuse": "/dev/fuse is missing (containers need --device /dev/fuse).",
                "dev_fuse_not_accessible": "/dev/fuse is not readable and writable by this user.",
                "no_fusermount3": "fusermount3 is not installed (apt-get install fuse3).",
                "mount_failed": "fusermount3 could not mount a test view.",
            }.get(info.get("reason") or "", "FUSE is unavailable.") + " " + FALLBACK_HINT
        info["fallback"] = "partial_checkout"
        return info
    if sys.platform == "darwin":
        macfuse = Path("/Library/Filesystems/macfuse.fs").exists()
        return {
            "backend": None,
            "available": False,
            "reason": "macos_no_unprivileged_backend",
            "macfuse_installed": macfuse,
            "message": (
                "macOS has no unprivileged user-space filesystem API Tri can drive from Python: macFUSE needs an "
                "administrator-approved kernel or FSKit extension, and FSKit modules ship inside signed apps. "
                + ("macFUSE is installed, but Tri's backend speaks only the Linux /dev/fuse protocol. " if macfuse else "")
                + FALLBACK_HINT
            ),
            "fallback": "partial_checkout",
        }
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        projfs = Path(system_root, "System32", "ProjectedFSLib.dll").exists()
        return {
            "backend": None,
            "available": False,
            "reason": "projfs_not_implemented",
            "projfs_installed": projfs,
            "message": "Windows ProjFS virtual checkouts are not implemented in this prototype. " + FALLBACK_HINT,
            "fallback": "partial_checkout",
        }
    return {"backend": None, "available": False, "reason": "unsupported_platform", "message": FALLBACK_HINT, "fallback": "partial_checkout"}


def require_available(error: Any) -> dict[str, Any]:
    info = availability()
    if not info["available"]:
        raise error("virtual_checkout_unavailable", info["message"], reason=info.get("reason"), fallback="--paths", retryable=False)
    return info


# -- daemon control -------------------------------------------------------------------


def _package_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def start_daemon(state_dir: Path, *, foreground: bool = False) -> dict[str, Any]:
    """Start serving ``state_dir``; returns ready.json. Foreground blocks until unmounted."""
    state_dir = Path(state_dir)
    (state_dir / "ready.json").unlink(missing_ok=True)
    if foreground:
        status = vfs.serve(state_dir)
        return {"ok": status == 0, "foreground": True, **_read_json(state_dir / "ready.json")}
    env = dict(os.environ)
    env["PYTHONPATH"] = _package_root() + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log = open(state_dir / "daemon.log", "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tri.vfs", "serve", str(state_dir)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            cwd="/",
            env=env,
        )
    finally:
        log.close()
    _DAEMONS[:] = [item for item in _DAEMONS if item.poll() is None]
    _DAEMONS.append(proc)
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        ready = _read_json(state_dir / "ready.json")
        if ready:
            return ready
        if proc.poll() is not None:
            ready = _read_json(state_dir / "ready.json")
            return ready or {"ok": False, "error": _log_tail(state_dir)}
        time.sleep(0.02)
    proc.kill()
    return {"ok": False, "error": "the view daemon did not report ready in time. " + _log_tail(state_dir)}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _log_tail(state_dir: Path) -> str:
    try:
        return (Path(state_dir) / "daemon.log").read_text(errors="replace")[-2000:]
    except OSError:
        return ""


def stop_daemon(state_dir: Path, mountpoint: Path) -> dict[str, Any]:
    """SIGTERM the daemon (it unmounts itself), escalate if needed, and make
    sure nothing stays mounted. Safe when the daemon is already gone."""
    state_dir = Path(state_dir)
    info = vfs.daemon_info(state_dir)
    was_alive = vfs.daemon_alive(state_dir)
    pid = info.get("pid")
    if was_alive and isinstance(pid, int):
        for sig, wait in ((signal.SIGTERM, STOP_TIMEOUT_SECONDS), (signal.SIGKILL, 5.0)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and vfs.daemon_alive(state_dir):
                time.sleep(0.05)
            if not vfs.daemon_alive(state_dir):
                break
    stale_mount = fuse.is_mounted(mountpoint)
    if stale_mount:
        fuse.unmount(mountpoint, lazy=True)
    return {"was_running": was_alive, "stale_mount_cleared": stale_mount and not fuse.is_mounted(mountpoint), "mounted": fuse.is_mounted(mountpoint)}


def state_dir_for(store: Any, lease_id: str) -> Path:
    return Path(store.leases_dir) / f"{lease_id}.virtual"


def is_virtual(lease: Any) -> bool:
    try:
        return lease["backend"] == BACKEND
    except (IndexError, KeyError):
        return False


def lease_live(store: Any, lease: Any) -> bool:
    """False only for a virtual lease whose daemon is gone (after startup)."""
    if not is_virtual(lease):
        return True
    state_dir = state_dir_for(store, lease["id"])
    if vfs.daemon_alive(state_dir):
        return True
    if not (state_dir / "ready.json").exists():
        from tri.cli import parse_utc

        try:
            age = time.time() - parse_utc(lease["created_at"]).timestamp()
        except (TypeError, ValueError):
            age = STARTUP_GRACE_SECONDS + 1
        if age < STARTUP_GRACE_SECONDS:
            return True  # still starting
    return False


def counters(state_dir: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    vfs.request_stats(state_dir)
    data: dict[str, Any] = vfs.read_stats(state_dir)
    if manifest is not None:
        data["view_files"] = len(manifest.get("entries", {}))
        data["view_bytes"] = sum(int(entry.get("size") or 0) for entry in manifest.get("entries", {}).values())
    return data


# -- local mode -----------------------------------------------------------------------


def _resolve_mountpoint(raw: str, error: Any) -> Path:
    requested = Path(raw)
    if requested.is_symlink():
        raise error("path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(requested))
    return requested.resolve()


def _prepare_mountpoint(root: Path, error: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise error("mount_path_in_use", "A virtual checkout needs an empty or new directory.", object=str(root))
    return root


def visible_root_manifest(store: Any, ws: Any, actor: str) -> tuple[str, dict[str, dict[str, Any]], dict[str, list[str]], list[str], int]:
    """Lazy root-only manifest built from ``list_directory_visible`` without flattening the repo."""
    from tri.cli import list_directory_visible, resolve_ref_context

    snapshot, hidden = resolve_ref_context(store, ws["id"], actor=actor, action="workspace.read")
    listing = list_directory_visible(store, snapshot, "", hidden)
    entries = {
        item["path"]: {key: item[key] for key in ("blob", "hash", "size", "mode", "symlink_target") if item.get(key) is not None}
        for item in listing["files"]
    }
    root_names = sorted([sub["name"] for sub in listing["directories"]] + [item["name"] for item in listing["files"]])
    known_dirs = ["", *[sub["path"] for sub in listing["directories"]]]
    return snapshot, entries, {"": root_names}, known_dirs, int(listing["redacted_skipped"])


def visible_manifest_entries(store: Any, ws: Any, actor: str) -> tuple[str, dict[str, dict[str, Any]], int]:
    """The view's lower layer: exactly what ``files list`` shows this actor."""
    from tri.cli import path_visibility, resolve_ref_context

    snapshot, hidden = resolve_ref_context(store, ws["id"], actor=actor, action="workspace.read")
    entries: dict[str, dict[str, Any]] = {}
    redacted = 0
    for path, entry in store.load_snapshot_tree(snapshot).items():
        if not path_visibility(path, hidden)[0]:
            redacted += 1
            continue
        kept = {key: entry[key] for key in ("blob", "hash", "size", "mode") if key in entry}
        if entry.get("symlink_target") is not None:
            kept["symlink_target"] = entry["symlink_target"]
        entries[path] = kept
    return snapshot, entries, redacted


def mount_local(store: Any, args: Any, command: list[str], start: float) -> dict[str, Any]:
    from tri import cli

    TriError = cli.TriError
    if args.paths:
        raise TriError("invalid_command", "--virtual faults in the whole tree on demand; drop --paths (or drop --virtual for a partial checkout).")
    if args.env_link:
        raise TriError("invalid_command", "--env-link is not supported with --virtual; virtual checkouts do not attach dependency environments.")
    info = require_available(TriError)
    ws = store.get_workspace(args.workspace)
    read_only = ws["status"] == "archived"
    if ws["status"] not in ("active", "archived"):
        raise TriError("policy_denied", "Only active or archived workspaces can be materialized.", object=ws["id"], status=ws["status"])
    cli.require_object_access(store, ws["id"], actor=args.actor, action="workspace.read")
    if args.ttl_seconds is not None and args.ttl_seconds <= 0:
        raise TriError("invalid_command", "Materialization lease TTL must be positive.", object=ws["id"])
    if args.no_ttl and args.ttl_seconds is not None:
        raise TriError("invalid_command", "Pass either --ttl-seconds or --no-ttl, not both.", object=ws["id"])
    root = _resolve_mountpoint(args.path, TriError)
    cli.claim_mount_path(store, root, actor=args.actor)  # may reap (and remove) a dead view first
    _prepare_mountpoint(root, TriError)
    snapshot, entries, dirs, known_dirs, redacted = visible_root_manifest(store, ws, args.actor)
    if snapshot != ws["head_snapshot_id"]:
        raise TriError("stale_materialization", "Workspace head moved while mounting; retry.", object=ws["id"], retryable=True)
    ttl_seconds = None if args.no_ttl else (args.ttl_seconds or cli.DEFAULT_MOUNT_TTL_SECONDS)
    lease_id = cli.new_id("lease")
    state_dir = state_dir_for(store, lease_id)
    vfs.write_manifest(
        state_dir,
        {
            "source": "local",
            "home": str(store.home),
            "workspace": ws["id"],
            "snapshot": snapshot,
            "lease": lease_id,
            "actor": args.actor,
            "mountpoint": str(root),
            "read_only": read_only,
            "mounted_at": time.time(),
            "lazy_trees": True,
            "entries": entries,
            "dirs": dirs,
            "known_dirs": known_dirs,
        },
    )
    ignored_paths = cli.normalize_hidden_patterns(args.ignore_path)
    with store.conn:
        store.conn.execute(
            """
            INSERT INTO materialization_leases (
              id, workspace_id, actor, path, mounted_snapshot_id, last_sync_snapshot_id,
              ignored_paths_json, generated_artifacts_json, status, expires_at, created_at, revoked_at,
              backend, degraded, baseline_path, ttl_seconds, env_keys_json, root_identity, sparse_paths_json, node_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease_id, ws["id"], args.actor, str(root), snapshot, snapshot,
                cli.dumps(ignored_paths), cli.dumps([]), "read_only" if read_only else "active",
                cli.lease_expiry(ttl_seconds) if ttl_seconds else None, cli.utcnow(), None,
                BACKEND, 0, None, ttl_seconds, cli.dumps([]), cli.directory_identity(root), None, store.node_id,
            ),
        )
        store.conn.execute("UPDATE workspaces SET materialized_path = ?, materialized_snapshot_id = ?, updated_at = ? WHERE id = ?", (str(root), snapshot, cli.utcnow(), ws["id"]))
        evt = store.create_event(
            kind="workspace.mounted",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"path": str(root), "snapshot": snapshot, "lease": lease_id, "read_only": read_only, "virtual": True},
        )
    if args.foreground:
        sys.stderr.write(f"tri: serving {ws['id']} at {root} (lease {lease_id}); stop with `tri workspace unmount {ws['id']}` or Ctrl-C\n")
        sys.stderr.flush()
    ready = start_daemon(state_dir, foreground=args.foreground)
    if not ready.get("ok"):
        cli._lease_update(store, "materialization_leases", lease_id, status="gone", reaped_at=cli.utcnow())
        cli.remove_tree(state_dir)
        raise TriError("virtual_mount_failed", "The virtual checkout could not be mounted.", object=str(root), error=str(ready.get("error", ""))[-1000:], retryable=False)
    data = {
        "workspace": ws["id"],
        "path": str(root),
        "snapshot": snapshot,
        "lease": lease_id,
        "read_only": read_only,
        "materialization": cli.materialization_lease_data(store.get_materialization_lease(lease_id)),
        "environments": [],
        "notes": [{"code": "virtual_checkout", "message": "Content is fetched on first read; dependency environments are not attached."}],
        "virtual": {
            "backend": info.get("backend"),
            "pid": ready.get("pid"),
            "foreground": bool(args.foreground),
            "auto_unmount": ready.get("auto_unmount"),
            "redacted_entries": redacted,
            "counters": counters(state_dir, {"entries": entries}),
        },
    }
    return cli.envelope("tri.workspace.mount", command, actor=args.actor, start=start, events=[evt], data=data)


def _accepted_changes(store: Any, ws: Any, lease: Any, before_entries: dict[str, dict[str, Any]], changed: list[str], deleted: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split the upper layer into what sync records and what it leaves local:
    paths hidden from the actor or secret-like, ignored (.gitignore, project
    ignores, --ignore-path), generated config, and .git directories."""
    from tri import cli

    _, hidden = cli.resolve_ref_context(store, ws["id"], actor=lease["actor"], action="workspace.read")
    rules = cli.import_rules(store, ws["project_id"], before_entries)
    lease_ignored = cli.loads(lease["ignored_paths_json"], [])
    tracked_dirs = {path.rsplit("/", 1)[0] for path in before_entries if "/" in path}
    skipped: list[str] = []

    def allowed(path: str, *, deleting: bool) -> bool:
        try:
            normalized = cli.normalize_path(path)
        except cli.TriError:
            return False
        if normalized != path or ".git" in path.split("/"):
            return False
        if not cli.path_visibility(path, hidden)[0] or cli.path_matches_hidden(path, lease_ignored):
            return False
        if path.startswith(cli.GENERATED_CONFIG_PREFIX + "/"):
            return False
        if deleting or path in before_entries:
            return True
        if rules and rules.matches(path):
            return False
        parts = path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            if ancestor not in tracked_dirs and rules and rules.dir_ignored(ancestor):
                return False
        return True

    accepted_changed = []
    for path in changed:
        (accepted_changed if allowed(path, deleting=False) else skipped).append(path)
    accepted_deleted = [path for path in deleted if path in before_entries and allowed(path, deleting=True)]
    return accepted_changed, accepted_deleted, skipped


def sync_entries(store: Any, ws: Any, lease: Any, before_entries: dict[str, dict[str, Any]], *, skip_escaping: bool = False) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """The workspace tree after applying a virtual lease's upper layer to ``before_entries``."""
    from tri import cli

    state_dir = state_dir_for(store, lease["id"])
    changed, deleted = vfs.upper_changes(state_dir)
    accepted_changed, accepted_deleted, skipped = _accepted_changes(store, ws, lease, before_entries, changed, deleted)
    after = cli.entries_after_scan(store, state_dir / "upper", before_entries, accepted_changed, accepted_deleted, skip_escaping=skip_escaping)
    report = {
        "backend": BACKEND,
        "running": vfs.daemon_alive(state_dir),
        "local_only_paths": len(skipped),
        "counters": counters(state_dir),
    }
    return after, report


def reap_claimed(store: Any, lease: Any, *, actor: str, force: bool, reason: str) -> dict[str, Any]:
    """Reaper path for a virtual lease: stop/unmount, salvage, remove the state."""
    from tri import cli

    state_dir = state_dir_for(store, lease["id"])
    root = Path(lease["path"])
    result: dict[str, Any] = {"lease": lease["id"], "kind": "mount", "path": lease["path"], "workspace": lease["workspace_id"], "reason": reason, "backend": BACKEND}
    result["daemon"] = stop_daemon(state_dir, root)
    if result["daemon"]["mounted"]:
        raise cli.TriError("mount_path_in_use", "The virtual checkout is still mounted and could not be unmounted; run fusermount3 -u -z on it.", object=str(root), lease=lease["id"])
    result["counters"] = counters(state_dir)
    ws = store.get_workspace(lease["workspace_id"])
    salvage = None
    if (state_dir / "upper").is_dir():
        before = lease["last_sync_snapshot_id"]
        before_entries = store.load_snapshot_tree(before)
        after_entries, _ = sync_entries(store, ws, lease, before_entries, skip_escaping=True)
        paths = cli.changed_paths(before_entries, after_entries)
        if paths:
            salvage = cli.create_salvage(store, ws, lease_id=lease["id"], kind="mount", base_snapshot=before, entries=after_entries, paths=paths, deleted_ignored={"count": 0, "paths": []}, actor=actor)
    cli.remove_tree(state_dir)
    identity = lease["root_identity"]
    if root.is_dir() and not root.is_symlink() and cli.same_directory(root, identity):
        try:
            root.rmdir()  # only if empty: the view never wrote into the directory itself
        except OSError:
            pass
    with store.conn:
        store.conn.execute(
            "UPDATE materialization_leases SET status = 'reaped', reaped_at = ?, salvage_id = ?, attention_json = NULL WHERE id = ?",
            (cli.utcnow(), salvage["id"] if salvage else None, lease["id"]),
        )
        store.create_event(
            kind="lease.reaped",
            actor=actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"lease": lease["id"], "path": str(root), "reason": reason, "salvage": salvage["id"] if salvage else None, "forced": force, "virtual": True},
        )
    result.update(status="reaped", salvage=salvage)
    return result


def _find_lease(store: Any, ws: Any, args: Any) -> Any:
    from tri import cli

    if args.lease:
        lease = store.get_materialization_lease(args.lease)
        if lease["workspace_id"] != ws["id"]:
            raise cli.TriError("stale_materialization", "Materialization lease is not scoped to this workspace.", object=args.lease, workspace=ws["id"])
        return lease
    sql = "SELECT * FROM materialization_leases WHERE workspace_id = ? AND backend = ? AND status NOT IN ('reaped', 'gone') AND reaped_at IS NULL"
    params: tuple[Any, ...] = (ws["id"], BACKEND)
    if args.path:
        sql += " AND path = ?"
        params = (*params, str(Path(args.path).resolve()))
    rows = store.rows(sql + " ORDER BY created_at DESC, rowid DESC", params)
    mine = [row for row in rows if row["actor"] == args.actor] or rows
    if not mine:
        raise cli.TriError("stale_materialization", "Workspace has no virtual checkout to unmount.", object=ws["id"])
    return mine[0]


def cmd_unmount(store: Any, args: Any, command: list[str], start: float) -> dict[str, Any]:
    from tri import cli

    ws = store.get_workspace(args.workspace)
    cli.require_object_access(store, ws["id"], actor=args.actor, action="workspace.read")
    lease = _find_lease(store, ws, args)
    if not is_virtual(lease):
        raise cli.TriError("invalid_command", "This lease is a materialized directory, not a virtual checkout; release it with tri leases reap <lease> --force.", object=lease["id"])
    if lease["actor"] != args.actor:
        cli.require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    if lease["status"] in ("reaped", "gone"):
        raise cli.TriError("lease_already_reaped", "Lease directory was already reaped.", object=lease["id"], status=lease["status"])
    if not args.force and lease["status"] in ("active", "read_only"):
        before_entries = store.load_snapshot_tree(lease["last_sync_snapshot_id"])
        after_entries, _ = sync_entries(store, ws, lease, before_entries, skip_escaping=True)
        pending = cli.changed_paths(before_entries, after_entries)
        if pending:
            raise cli.TriError(
                "unsynced_changes",
                "The view has edits that were not synced; run tri workspace sync first, or pass --force to keep them only as a salvage.",
                object=lease["id"],
                paths=pending[:50],
                count=len(pending),
                retryable=False,
            )
    result = cli.reap_mount_lease(store, lease, actor=args.actor, force=True, reason="unmounted")
    if result is None:
        raise cli.TriError("lease_already_reaped", "Another process is releasing this lease.", object=lease["id"])
    data = {
        "workspace": ws["id"],
        "lease": lease["id"],
        "path": lease["path"],
        "unmounted": not result.get("daemon", {}).get("mounted", False),
        "status": result.get("status"),
        "salvage": result.get("salvage"),
        "daemon": result.get("daemon"),
        "virtual": {"backend": BACKEND, "counters": result.get("counters", {})},
    }
    return cli.envelope("tri.workspace.unmount", command, actor=args.actor, start=start, data=data)


# -- remote mode ----------------------------------------------------------------------


def _remote_state_dir(dest: Path) -> Path:
    from tri.remote import mount_state_path

    state_file = mount_state_path(dest)
    return state_file.with_name(state_file.stem + ".virtual")


def remote_mount(client: Any, argv: list[str], workspace: str, dest: Path, *, foreground: bool) -> dict[str, Any]:
    from tri.cli import envelope
    from tri.remote import error_envelope, mount_state_path

    started = time.time()
    info = availability()
    if not info["available"]:
        return error_envelope(argv, "virtual_checkout_unavailable", info["message"], reason=info.get("reason"), fallback="--paths")
    if "--paths" in argv:
        return error_envelope(argv, "invalid_command", "--virtual faults in the whole tree on demand; drop --paths (or drop --virtual for a partial checkout).")
    dest = dest.expanduser()
    if dest.is_symlink():
        return error_envelope(argv, "path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if any(root.iterdir()):
        return error_envelope(argv, "mount_path_in_use", "A virtual checkout needs an empty or new directory.", object=str(root))
    listing = client.command(["files", "ls", workspace])
    if not listing.get("ok"):
        return listing
    snapshot = listing["data"]["snapshot"]
    entries = {}
    root_names: list[str] = []
    known_dirs: list[str] = [""]
    for sub in listing["data"]["directories"]:
        sub_path = sub["path"]
        if ".." in sub_path.split("/") or sub_path.startswith("/"):
            return error_envelope(argv, "path_escaped_mount", "Server listed a path outside the mount.", object=sub_path)
        known_dirs.append(sub_path)
        root_names.append(sub["name"])
    for item in listing["data"]["files"]:
        path = item["path"]
        if ".." in path.split("/") or path.startswith("/"):
            return error_envelope(argv, "path_escaped_mount", "Server listed a path outside the mount.", object=path)
        entries[path] = {key: item[key] for key in ("blob", "hash", "size", "mode", "symlink_target") if item.get(key) is not None}
        root_names.append(item["name"])
    state_dir = _remote_state_dir(root)
    if state_dir.exists():
        if vfs.daemon_alive(state_dir):
            return error_envelope(argv, "mount_path_in_use", "A virtual checkout already serves this directory.", object=str(root))
        shutil.rmtree(state_dir, ignore_errors=True)
    vfs.write_manifest(
        state_dir,
        {
            "source": "remote",
            "remote": client.url,
            "workspace": workspace,
            "snapshot": snapshot,
            "mountpoint": str(root),
            "read_only": False,
            "mounted_at": time.time(),
            "lazy_trees": True,
            "entries": entries,
            "dirs": {"": sorted(set(root_names))},
            "known_dirs": sorted(set(known_dirs)),
        },
    )
    state = {"remote": client.url, "workspace": workspace, "snapshot": snapshot, "path": str(root), "paths": [], "virtual": str(state_dir), "entries": {}}
    state_file = mount_state_path(root)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))
    ready = start_daemon(state_dir, foreground=foreground)
    if not ready.get("ok"):
        state_file.unlink(missing_ok=True)
        shutil.rmtree(state_dir, ignore_errors=True)
        return error_envelope(argv, "virtual_mount_failed", "The virtual checkout could not be mounted.", object=str(root), error=str(ready.get("error", ""))[-1000:])
    data = {
        "workspace": workspace,
        "path": str(root),
        "snapshot": snapshot,
        "remote": client.url,
        "files": len(entries),
        "fetched_bytes": 0,
        "cache_hits": 0,
        "sparse_paths": [],
        "virtual": {"backend": info.get("backend"), "pid": ready.get("pid"), "foreground": foreground, "auto_unmount": ready.get("auto_unmount"), "counters": counters(state_dir, {"entries": entries})},
    }
    return envelope("tri.remote.mount", ["tri", *argv], start=started, data=data)


def _synced_hash(state_dir: Path, manifest: dict[str, Any], synced: dict[str, Any], path: str, *, lower_hashes: dict[str, str] | None = None) -> str | None:
    if path in synced:
        return synced[path]
    entry = manifest["entries"].get(path)
    if entry:
        return entry.get("hash")
    if lower_hashes and path in lower_hashes:
        return lower_hashes[path]
    return None


def _remote_pending(state_dir: Path) -> tuple[dict[str, tuple[bytes, str]], list[str], list[str], dict[str, Any], dict[str, Any]]:
    from tri.cli import is_secret_like_path

    manifest = vfs.load_manifest(state_dir)
    synced = _read_json(state_dir / "synced.json")
    lower_hashes = vfs.load_layer_state(state_dir).get("lower_hashes", {})
    changed, deleted = vfs.upper_changes(state_dir)
    upper = state_dir / "upper"
    writes: dict[str, tuple[bytes, str]] = {}
    skipped: list[str] = []
    for path in changed:
        if is_secret_like_path(path) or ".git" in path.split("/"):
            skipped.append(path)
            continue
        full = upper / path
        data = os.readlink(full).encode() if full.is_symlink() else full.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if _synced_hash(state_dir, manifest, synced, path, lower_hashes=lower_hashes) != digest:
            writes[path] = (data, digest)
    deletes = [path for path in deleted if _synced_hash(state_dir, manifest, synced, path, lower_hashes=lower_hashes) is not None and not is_secret_like_path(path)]
    return writes, deletes, skipped, manifest, synced


def remote_sync(client: Any, argv: list[str], state_file: Path, state: dict[str, Any]) -> dict[str, Any]:
    import base64

    from tri.cli import envelope

    started = time.time()
    state_dir = Path(state["virtual"])
    workspace = state["workspace"]
    writes, deletes, skipped, _, synced = _remote_pending(state_dir)
    commands: list[dict[str, Any]] = []
    for index, (rel, (data, _)) in enumerate(sorted(writes.items())):
        argv_write = ["files", "write", workspace, rel, "--base64", base64.b64encode(data).decode()]
        if index == 0:
            argv_write += ["--expect-head", state["snapshot"]]
        commands.append({"argv": argv_write})
    for index, rel in enumerate(sorted(deletes)):
        argv_delete = ["files", "delete", workspace, rel]
        if not commands and index == 0:
            argv_delete += ["--expect-head", state["snapshot"]]
        commands.append({"argv": argv_delete})
    snapshot = state["snapshot"]
    if commands:
        result = client.batch(commands)
        if not result.get("ok"):
            diagnostics = result.get("diagnostics") or [diag for item in result.get("data", {}).get("results", []) for diag in item.get("diagnostics", [])]
            stale = any(diag.get("code") == "stale_workspace_head" for diag in diagnostics)
            return envelope(
                "tri.error",
                ["tri", *argv],
                ok=False,
                diagnostics=[
                    {
                        "code": "stale_materialization" if stale else (diagnostics[0]["code"] if diagnostics else "remote_unreachable"),
                        "severity": "error",
                        "message": "The workspace moved on the server since this mount; remount to pick up its changes." if stale else "Remote sync failed.",
                        "retryable": False,
                        "details": diagnostics[:5],
                    }
                ],
                data={"workspace": workspace, "executed": result.get("data", {}).get("executed")},
            )
        for item in result["data"]["results"]:
            snapshot = item["data"].get("snapshot", snapshot)
    for rel, (_, digest) in writes.items():
        synced[rel] = digest
    for rel in deletes:
        synced[rel] = None
    vfs._atomic_write(state_dir / "synced.json", synced)
    state["snapshot"] = snapshot
    state_file.write_text(json.dumps(state))
    data = {
        "workspace": workspace,
        "path": state["path"],
        "snapshot": snapshot,
        "remote": client.url,
        "written": sorted(writes),
        "deleted": sorted(deletes),
        "virtual": {"backend": BACKEND, "running": vfs.daemon_alive(state_dir), "local_only_paths": len(skipped), "counters": counters(state_dir)},
    }
    return envelope("tri.remote.sync", ["tri", *argv], start=started, data=data)


def remote_unmount(client: Any, argv: list[str], workspace: str, dest: Path | None, *, force: bool) -> dict[str, Any]:
    from tri.cli import envelope
    from tri.remote import cache_root, error_envelope, mount_state_path

    started = time.time()
    state_file: Path | None = None
    state: dict[str, Any] | None = None
    if dest is not None:
        state_file = mount_state_path(dest.expanduser())
        state = _read_json(state_file) or None
    else:
        mounts = cache_root() / "mounts"
        for candidate in sorted(mounts.glob("*.json")) if mounts.is_dir() else []:
            loaded = _read_json(candidate)
            if loaded.get("workspace") == workspace and loaded.get("remote") == client.url and loaded.get("virtual"):
                state_file, state = candidate, loaded
                break
    if state_file is None or not state or not state.get("virtual"):
        return error_envelope(argv, "stale_materialization", "No virtual checkout of this workspace on this machine.", object=workspace)
    state_dir = Path(state["virtual"])
    if not force and state_dir.is_dir():
        writes, deletes, _, _, _ = _remote_pending(state_dir)
        pending = sorted(set(writes) | set(deletes))
        if pending:
            return error_envelope(argv, "unsynced_changes", "The view has edits that were not synced; run tri workspace sync first, or pass --force to drop them.", object=workspace, paths=pending[:50], count=len(pending))
    root = Path(state["path"])
    daemon = stop_daemon(state_dir, root)
    if daemon["mounted"]:
        return error_envelope(argv, "mount_path_in_use", "The virtual checkout is still mounted and could not be unmounted; run fusermount3 -u -z on it.", object=str(root))
    final = counters(state_dir)
    shutil.rmtree(state_dir, ignore_errors=True)
    state_file.unlink(missing_ok=True)
    try:
        root.rmdir()
    except OSError:
        pass
    data = {"workspace": workspace, "lease": None, "path": str(root), "unmounted": True, "status": "released", "salvage": None, "daemon": daemon, "remote": client.url, "virtual": {"backend": BACKEND, "counters": final}}
    return envelope("tri.workspace.unmount", ["tri", *argv], start=started, data=data)
