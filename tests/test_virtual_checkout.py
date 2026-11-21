"""Virtual checkouts: a whole-workspace view whose content is faulted in on first read.

The layer logic (``tri.vfs.VirtualView``) is tested everywhere it can run.
The FUSE tests need a Linux host with /dev/fuse and fusermount3 that can
really mount (the ``linux-fuse`` CI job installs fuse3); elsewhere they skip.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib import request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli, fuse, vfs, virtual  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402

HIDDEN_CANARY = "vck_hiddenpath_z9q"
PACKAGE_CANARY = "vck_privatepkg_z9q"
CONTENT_CANARY = "vck_content_z9q"

_FUSE_PROBE: dict | None = None


def fuse_probe() -> dict:
    global _FUSE_PROBE
    if _FUSE_PROBE is None:
        _FUSE_PROBE = fuse.probe(deep=True)
    return _FUSE_PROBE


def fuse_skip_reason() -> str | None:
    if not sys.platform.startswith("linux"):
        return "the FUSE backend is Linux-only"
    info = fuse_probe()
    if not info["available"]:
        return f"FUSE unavailable here: {info.get('reason')} {info.get('error', '')}".strip()
    return None


class FakeSource:
    kind = "fake"

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.reads: list[str] = []

    def prepare(self) -> None:
        pass

    def read(self, path: str, entry: dict, stats: vfs.Stats) -> bytes:
        self.reads.append(path)
        data = self.blobs[entry["hash"]]
        stats.add("bytes_fetched", len(data))
        return data

    def close(self) -> None:
        pass


@unittest.skipIf(os.name == "nt", "the view layer serves the Linux FUSE backend; it uses POSIX file APIs")
class VirtualViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-vfs-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        files = {"README.md": b"hello\n", "src/app.py": b"print(1)\n", "src/lib/util.py": b"x = 1\n", "run.sh": b"#!/bin/sh\n"}
        self.blobs = {f"h-{path}": data for path, data in files.items()}
        entries = {path: {"blob": f"b-{path}", "hash": f"h-{path}", "size": len(data), "mode": "100755" if path == "run.sh" else "100644"} for path, data in files.items()}
        entries["link"] = {"blob": "b-link", "hash": "h-link", "size": 9, "mode": "120000", "symlink_target": "README.md"}
        self.manifest = {"entries": entries, "mounted_at": 1_700_000_000}
        self.source = FakeSource(self.blobs)
        self.view = vfs.VirtualView(self.state, self.manifest, self.source)

    def read_all(self, path: str) -> bytes:
        handle = self.view.open(path, os.O_RDONLY)
        try:
            return handle.read(0, 1 << 20)
        finally:
            handle.release()

    def test_tree_comes_from_the_manifest_and_content_is_faulted_once(self) -> None:
        self.assertEqual(self.view.listdir(""), ["README.md", "link", "run.sh", "src"])
        self.assertEqual(self.view.listdir("src"), ["app.py", "lib"])
        self.assertTrue(stat.S_ISDIR(self.view.getattr("src/lib").mode))
        self.assertEqual(self.view.getattr("run.sh").mode & 0o777, 0o755)
        self.assertTrue(stat.S_ISLNK(self.view.getattr("link").mode))
        self.assertEqual(self.view.readlink("link"), "README.md")
        self.assertEqual(self.source.reads, [], "listing and stat never fetch content")
        self.assertEqual(self.read_all("src/app.py"), b"print(1)\n")
        self.assertEqual(self.read_all("src/app.py"), b"print(1)\n")
        self.assertEqual(self.source.reads, ["src/app.py"])
        self.assertEqual(self.view.stats.values["files_faulted"], 1)
        self.assertEqual(self.view.stats.values["bytes_fetched"], 9)
        with self.assertRaises(OSError) as caught:
            self.view.getattr("missing.txt")
        self.assertEqual(caught.exception.errno, errno.ENOENT)

    def test_writes_copy_up_and_deletes_leave_whiteouts(self) -> None:
        handle = self.view.open("README.md", os.O_WRONLY | os.O_APPEND)
        handle.write(6, b"more\n")
        handle.release()
        self.assertEqual(self.read_all("README.md"), b"hello\nmore\n")
        with self.assertRaises(OSError) as caught:
            self.view.create("notes/todo.txt", 0o644, os.O_WRONLY)
        self.assertEqual(caught.exception.errno, errno.ENOENT)
        self.view.mkdir("notes", 0o755)
        handle = self.view.create("notes/todo.txt", 0o644, os.O_WRONLY)
        handle.write(0, b"todo\n")
        handle.release()
        self.view.unlink("run.sh")
        self.assertNotIn("run.sh", self.view.listdir(""))
        changed, deleted = vfs.upper_changes(self.state)
        self.assertEqual(changed, ["README.md", "notes/todo.txt"])
        self.assertEqual(deleted, ["run.sh"])
        reloaded = vfs.VirtualView(self.state, self.manifest, self.source)
        self.assertNotIn("run.sh", reloaded.listdir(""), "whiteouts persist across restarts")

    def test_rename_of_a_lower_directory_moves_its_subtree(self) -> None:
        self.view.rename("src", "lib")
        self.assertEqual(self.view.listdir("lib"), ["app.py", "lib"])
        self.assertEqual(self.read_all("lib/lib/util.py"), b"x = 1\n")
        with self.assertRaises(OSError):
            self.view.getattr("src/app.py")
        changed, deleted = vfs.upper_changes(self.state)
        self.assertEqual(changed, ["lib/app.py", "lib/lib/util.py"])
        self.assertEqual(deleted, ["src/app.py", "src/lib/util.py"])
        self.view.mkdir("src", 0o755)
        self.assertEqual(self.view.listdir("src"), [], "a recreated directory does not resurrect moved files")

    def test_rmdir_needs_an_empty_directory_and_mode_changes_record_only_the_exec_bit(self) -> None:
        with self.assertRaises(OSError) as caught:
            self.view.rmdir("src/lib")
        self.assertEqual(caught.exception.errno, errno.ENOTEMPTY)
        self.view.unlink("src/lib/util.py")
        self.view.rmdir("src/lib")
        self.assertEqual(self.view.listdir("src"), ["app.py"])
        self.view.chmod("src/app.py", 0o600)  # no exec-bit change: nothing to record, nothing fetched
        self.assertEqual(self.source.reads, [])
        self.view.chmod("src/app.py", 0o755)
        self.assertEqual(vfs.upper_changes(self.state)[0], ["src/app.py"])
        self.view.utimens("README.md", None, 0)  # touching a lower file never copies it up
        self.assertNotIn("README.md", vfs.upper_changes(self.state)[0])

    def test_read_only_views_refuse_writes(self) -> None:
        view = vfs.VirtualView(Path(self.tmp.name) / "ro", {**self.manifest, "read_only": True}, self.source)
        self.assertEqual(view.getattr("README.md").mode & 0o222, 0)
        for call in (lambda: view.open("README.md", os.O_WRONLY), lambda: view.mkdir("x", 0o755), lambda: view.unlink("README.md")):
            with self.assertRaises(OSError) as caught:
                call()
            self.assertEqual(caught.exception.errno, errno.EROFS)


class VirtualCheckoutCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-virtual-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_bytes(b"print(1)\n")
        patcher = mock.patch.dict(os.environ, {"TRI_HOME": str(self.root / "home"), "TRI_AUTO_REAP": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        os.environ.pop("TRI_REMOTE", None)
        self.run_tri("import", str(repo), "--name", "app", "--target", "main")
        self.ws = self.run_tri("workspace", "create", "app", "--from", "main")["data"]["id"]

    def run_tri(self, *argv: str, ok: bool = True) -> dict:
        output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        for diagnostic in output["diagnostics"]:
            self.assertIn(diagnostic["code"], SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        return output

    def test_doctor_reports_the_virtual_checkout_backend(self) -> None:
        report = self.run_tri("doctor")["data"]["virtual_checkouts"]
        self.assertIn("available", report)
        self.assertEqual(report["fallback"], "partial_checkout")
        if sys.platform == "darwin":
            self.assertFalse(report["available"])
            self.assertEqual(report["reason"], "macos_no_unprivileged_backend")
        elif os.name == "nt":
            self.assertFalse(report["available"])
            self.assertEqual(report["reason"], "projfs_not_implemented")
        else:
            self.assertEqual(report["backend"], "fuse")

    def test_unavailable_backend_refuses_with_a_partial_checkout_hint(self) -> None:
        unavailable = {"backend": None, "available": False, "reason": "macos_no_unprivileged_backend", "message": "no backend. " + virtual.FALLBACK_HINT, "fallback": "partial_checkout"}
        with mock.patch.object(virtual, "availability", return_value=unavailable):
            out = self.run_tri("workspace", "mount", self.ws, str(self.root / "view"), "--virtual", ok=False)
        diagnostic = out["diagnostics"][0]
        self.assertEqual(diagnostic["code"], "virtual_checkout_unavailable")
        self.assertEqual(diagnostic["fallback"], "--paths")
        self.assertIn("--paths", diagnostic["message"])
        self.assertFalse((self.root / "view").exists(), "nothing is created when the backend is missing")
        self.assertEqual(self.run_tri("workspace", "leases", self.ws)["data"]["leases"], [])

    def test_virtual_conflicts_with_partial_paths_and_foreground_needs_virtual(self) -> None:
        out = self.run_tri("workspace", "mount", self.ws, str(self.root / "v1"), "--virtual", "--paths", "src", ok=False)
        self.assertEqual(out["diagnostics"][0]["code"], "invalid_command")
        out = self.run_tri("workspace", "mount", self.ws, str(self.root / "v2"), "--foreground", ok=False)
        self.assertEqual(out["diagnostics"][0]["code"], "invalid_command")

    def test_unmount_without_a_virtual_checkout_is_a_clear_error(self) -> None:
        out = self.run_tri("workspace", "unmount", self.ws, ok=False)
        self.assertEqual(out["diagnostics"][0]["code"], "stale_materialization")


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FuseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        reason = fuse_skip_reason()
        if reason and os.environ.get("TRI_REQUIRE_FUSE") == "1":
            self.fail(f"TRI_REQUIRE_FUSE=1 but {reason}")  # the CI job that installs fuse3 must not skip
        if reason:
            self.skipTest(reason)
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-fuse-")
        self.addCleanup(self.tmp.cleanup)  # registered first: runs after every unmount
        self.root = Path(self.tmp.name)

    def track_mount(self, mountpoint: Path, state_dir: Path) -> None:
        def cleanup() -> None:
            virtual.stop_daemon(state_dir, mountpoint)
            if fuse.is_mounted(mountpoint):
                fuse.unmount(mountpoint)

        self.addCleanup(cleanup)

    def walk(self, top: Path) -> tuple[list[str], bytes]:
        names: list[str] = []
        content = b""
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames + filenames:
                names.append(os.path.relpath(os.path.join(dirpath, name), top))
            for name in filenames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    with open(full, "rb") as handle:
                        content += handle.read()
        return sorted(names), content


class FuseLocalCheckoutTests(FuseTestBase):
    def setUp(self) -> None:
        super().setUp()
        repo = self.root / "repo"
        for rel, data in {
            "README.md": b"hello\n",
            "src/app.py": b"print('v1')\n",
            "src/big.bin": bytes(range(256)) * 4096,
            f"docs/{HIDDEN_CANARY}.txt": f"{CONTENT_CANARY} hidden\n".encode(),
            f"vault/{PACKAGE_CANARY}/keys.txt": f"{CONTENT_CANARY} package\n".encode(),
            "tools/run.sh": b"#!/bin/sh\necho ran\n",
        }.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (repo / "tools" / "run.sh").chmod(0o755)
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"]):
            subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # git records the exec bit
        patcher = mock.patch.dict(os.environ, {"TRI_HOME": str(self.root / "home"), "TRI_AUTO_REAP": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        os.environ.pop("TRI_REMOTE", None)
        self.tri("import", str(repo), "--name", "app", "--target", "main")
        self.tri("packages", "declare", "app", "vault", "--path", "vault/**", "--visibility", "private")

    def tri(self, *argv: str, ok: bool = True, actor: str | None = None) -> dict:
        full = ["--actor", actor, *argv] if actor else list(argv)
        output, _ = cli.execute_argv(full)
        self.assertEqual(output["ok"], ok, output)
        for diagnostic in output["diagnostics"]:
            self.assertIn(diagnostic["code"], SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        schema = SCHEMA_CATALOG.get(output["schema"])
        if schema and output["ok"]:
            missing = [key for key in schema.get("required", []) if key not in output["data"]]
            self.assertEqual(missing, [], f"{output['schema']} missing {missing}")
        return output

    def mount(self, ws: str, *, actor: str | None = None, name: str = "view") -> tuple[Path, dict]:
        mountpoint = self.root / name
        out = self.tri("workspace", "mount", ws, str(mountpoint), "--virtual", actor=actor)["data"]
        state_dir = Path(os.environ["TRI_HOME"]) / "leases" / f"{out['lease']}.virtual"
        self.track_mount(mountpoint.resolve(), state_dir)
        self.assertTrue(fuse.is_mounted(mountpoint.resolve()))
        return mountpoint, out

    def state_dir(self, out: dict) -> Path:
        return Path(os.environ["TRI_HOME"]) / "leases" / f"{out['lease']}.virtual"

    def agent_workspace(self) -> str:
        return self.tri("workspace", "create", "app", "--from", "main", "--hide-path", f"docs/{HIDDEN_CANARY}.txt", actor="agent:a")["data"]["id"]

    def test_whole_tree_is_visible_and_content_is_fetched_on_first_read(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        view, out = self.mount(ws)
        counters = out["virtual"]["counters"]
        self.assertEqual(counters["files_faulted"], 0)
        self.assertEqual(counters["bytes_fetched"], 0)
        self.assertEqual(counters["view_files"], 6)
        self.assertEqual(out["materialization"]["backend"], "fuse")
        self.assertEqual(sorted(os.listdir(view)), ["README.md", "docs", "src", "tools", "vault"])
        self.assertEqual((view / "src" / "app.py").read_bytes(), b"print('v1')\n")
        self.assertEqual(os.stat(view / "src" / "big.bin").st_size, 256 * 4096)
        self.assertTrue(os.access(view / "tools" / "run.sh", os.X_OK))
        self.assertEqual(subprocess.run([str(view / "tools" / "run.sh")], stdout=subprocess.PIPE).stdout, b"ran\n")
        synced = self.tri("workspace", "sync", ws)["data"]
        self.assertEqual(synced["paths"], [])
        stats = synced["virtual"]["counters"]
        self.assertEqual(stats["files_faulted"], 2, "only the two files read were fetched; big.bin was only stat-ed")
        self.assertEqual(stats["bytes_fetched"], len(b"print('v1')\n") + len(b"#!/bin/sh\necho ran\n"))

    def test_hidden_paths_and_private_packages_never_appear(self) -> None:
        ws = self.agent_workspace()
        view, out = self.mount(ws, actor="agent:a")
        self.assertEqual(out["virtual"]["redacted_entries"], 2)
        names, content = self.walk(view)
        # docs/ held only the hidden file, so the directory itself is not shown.
        self.assertEqual(names, ["README.md", "src", "src/app.py", "src/big.bin", "tools", "tools/run.sh"])
        blob = json.dumps(names).encode() + content
        for canary in (HIDDEN_CANARY, PACKAGE_CANARY, CONTENT_CANARY):
            self.assertNotIn(canary.encode(), blob)
        for probe in (f"docs/{HIDDEN_CANARY}.txt", f"vault/{PACKAGE_CANARY}/keys.txt", "vault"):
            with self.assertRaises(FileNotFoundError):
                os.stat(view / probe)
        # Writing at a hidden path stays local: sync never records it.
        (view / "docs").mkdir()
        (view / "docs" / f"{HIDDEN_CANARY}.txt").write_bytes(b"agent overwrite\n")
        (view / "src" / "app.py").write_bytes(b"print('agent')\n")
        synced = self.tri("workspace", "sync", ws, actor="agent:a")["data"]
        self.assertEqual(synced["paths"], ["src/app.py"])
        self.assertEqual(synced["virtual"]["local_only_paths"], 1)
        store = cli.Store(pack_on_close=False)
        try:
            head = store.get_workspace(ws)["head_snapshot_id"]
            hidden = store.read_blob(store.load_snapshot_tree(head)[f"docs/{HIDDEN_CANARY}.txt"]["blob"])
        finally:
            store.close()
        self.assertIn(CONTENT_CANARY.encode(), hidden, "the hidden file kept its content")

    def test_edits_sync_as_workspace_edits_with_the_stale_head_refusal(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        view, out = self.mount(ws)
        with open(view / "README.md", "ab") as handle:
            handle.write(b"more\n")
        (view / "notes").mkdir()
        (view / "notes" / "todo.txt").write_bytes(b"todo\n")
        os.rename(view / "src", view / "lib")
        os.remove(view / "tools" / "run.sh")
        os.symlink("../README.md", view / "notes" / "readme")
        synced = self.tri("workspace", "sync", ws)["data"]
        self.assertEqual(synced["paths"], ["README.md", "lib/app.py", "lib/big.bin", "notes/readme", "notes/todo.txt", "src/app.py", "src/big.bin", "tools/run.sh"])
        self.assertEqual(self.tri("files", "read", ws, "README.md")["data"]["text"], "hello\nmore\n")
        self.assertEqual(self.tri("files", "stat", ws, "notes/readme")["data"]["symlink_target"], "../README.md")
        self.assertEqual(self.tri("workspace", "sync", ws)["data"]["paths"], [], "a second sync records nothing new")
        # The head moves behind the view's back: sync refuses instead of overwriting.
        self.tri("files", "write", ws, "README.md", "--text", "moved\n")
        (view / "README.md").write_bytes(b"from the view\n")
        stale = self.tri("workspace", "sync", ws, ok=False)
        self.assertEqual(stale["diagnostics"][0]["code"], "stale_materialization")
        self.assertEqual(self.tri("files", "read", ws, "README.md")["data"]["text"], "moved\n")

    def test_unmount_refuses_unsynced_edits_then_force_salvages_them(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        view, out = self.mount(ws)
        (view / "draft.txt").write_bytes(b"draft\n")
        refused = self.tri("workspace", "unmount", ws, ok=False)
        self.assertEqual(refused["diagnostics"][0]["code"], "unsynced_changes")
        self.assertEqual(refused["diagnostics"][0]["paths"], ["draft.txt"])
        self.assertTrue(fuse.is_mounted(view.resolve()))
        done = self.tri("workspace", "unmount", ws, "--force")["data"]
        self.assertTrue(done["unmounted"])
        self.assertEqual(done["status"], "reaped")
        self.assertIsNotNone(done["salvage"])
        self.assertFalse(fuse.is_mounted(view.resolve()))
        self.assertFalse(view.exists(), "the empty mountpoint is removed")
        salvage = self.tri("salvage", "list", ws)["data"]["salvages"][0]
        self.assertIn("draft.txt", salvage["paths"])
        self.assertFalse((Path(os.environ["TRI_HOME"]) / "leases" / f"{out['lease']}.virtual").exists())

    def test_sigterm_unmounts_cleanly_and_the_reaper_releases_the_lease(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        view, out = self.mount(ws)
        (view / "unsynced.txt").write_bytes(b"keep me\n")
        os.kill(out["virtual"]["pid"], signal.SIGTERM)
        self.assertTrue(wait_until(lambda: not fuse.is_mounted(view.resolve())), "SIGTERM must unmount")
        self.assertTrue(wait_until(lambda: not vfs.daemon_alive(self.state_dir(out))), "the daemon exits after unmounting")
        self.assertEqual(os.listdir(view), [], "the mountpoint is an ordinary empty directory again")
        report = self.tri("leases", "reap")["data"]
        reaped = [item for item in report["reaped"] if item["lease"] == out["lease"]]
        self.assertEqual(reaped[0]["reason"], "daemon_exited")
        self.assertIsNotNone(reaped[0]["salvage"])

    def test_killed_daemon_does_not_leave_a_wedged_mount(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        view, out = self.mount(ws)
        self.assertEqual((view / "README.md").read_bytes(), b"hello\n")
        os.kill(out["virtual"]["pid"], signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not vfs.daemon_alive(self.state_dir(out))))
        if out["virtual"]["auto_unmount"]:
            # Best effort: fusermount3's auto_unmount watcher usually clears the
            # mount, but not on every host (GitHub's ubuntu-24.04 runners keep it).
            # The guarantee is the reaper's lazy unmount, asserted below.
            wait_until(lambda: not fuse.is_mounted(view.resolve()), timeout=5.0)
        report = self.tri("gc", "--reap")["data"]["reaped"]
        self.assertEqual([item["reason"] for item in report["reaped"] if item["lease"] == out["lease"]], ["daemon_exited"])
        self.assertFalse(fuse.is_mounted(view.resolve()))
        self.assertFalse(view.exists() and os.listdir(view), "nothing wedged or left behind")
        # The directory can be mounted again.
        self.mount(ws, name="view")

    def test_remount_of_a_dead_view_reaps_it_first(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        view, out = self.mount(ws)
        os.kill(out["virtual"]["pid"], signal.SIGTERM)
        self.assertTrue(wait_until(lambda: not fuse.is_mounted(view.resolve())))
        self.assertTrue(wait_until(lambda: not vfs.daemon_alive(self.state_dir(out))))
        _, again = self.mount(ws)
        self.assertNotEqual(again["lease"], out["lease"])
        self.assertEqual(self.tri("workspace", "lease-inspect", out["lease"])["data"]["status"], "reaped")


class FuseRemoteCheckoutTests(FuseTestBase):
    def setUp(self) -> None:
        super().setUp()
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_bytes(b"print('v1')\n")
        (repo / "docs").mkdir()
        (repo / "docs" / "guide.md").write_bytes(b"guide\n")
        (repo / "docs" / "faq.md").write_bytes(b"faq\n")
        (repo / "docs" / "notes.md").write_bytes(b"notes\n")
        (repo / "vault").mkdir()
        (repo / "vault" / f"{PACKAGE_CANARY}.txt").write_bytes(f"{CONTENT_CANARY}\n".encode())
        self.server_env = {**os.environ, "TRI_HOME": str(self.root / "server-home"), "TRI_AUTO_REAP": "0"}
        self.server_env.pop("TRI_REMOTE", None)
        self.server_env.pop("TRI_ACTOR", None)
        self.server_tri("import", str(repo), "--name", "app", "--target", "main")
        self.server_tri("packages", "declare", "app", "vault", "--path", "vault/**", "--visibility", "private")
        tokens = self.root / "tokens.json"
        token = self.server_tri("auth", "issue", "agent:a", "--file", str(tokens))["data"]["token"]
        self.url = f"http://127.0.0.1:{self.start_server(tokens)}"
        patcher = mock.patch.dict(os.environ, {"TRI_REMOTE": self.url, "TRI_TOKEN": token, "TRI_HOME": str(self.root / "client-home"), "TRI_CACHE_DIR": str(self.root / "cache")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)

    def server_tri(self, *args: str) -> dict:
        result = subprocess.run([sys.executable, "-m", "tri", *args], cwd=ROOT, env=self.server_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"], output)
        return output

    def start_server(self, tokens: Path) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "tri", "serve", "--port", str(port), "--quiet", "--auth-file", str(tokens)], cwd=ROOT, env=self.server_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def stop() -> None:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)

        self.addCleanup(stop)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with request.urlopen(f"http://127.0.0.1:{port}/v0/health", timeout=2) as response:
                    if response.status == 200:
                        return port
            except (URLError, ConnectionError, TimeoutError):
                time.sleep(0.05)
        self.fail("server did not start")

    def tri(self, *argv: str, ok: bool = True) -> dict:
        output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        return output

    def test_remote_view_fetches_through_the_machine_cache_and_syncs(self) -> None:
        from tri.remote import mount_state_path

        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        view = self.root / "view"
        out = self.tri("workspace", "mount", ws, str(view), "--virtual")["data"]
        state_dir = Path(json.loads(mount_state_path(view.resolve()).read_text())["virtual"])
        self.track_mount(view.resolve(), state_dir)
        self.assertEqual(out["files"], 4, "the private package is not listed to agent:a")
        self.assertEqual(sorted(os.listdir(view)), ["docs", "src"])
        self.assertEqual((view / "src" / "app.py").read_bytes(), b"print('v1')\n")
        # One round trip fetches faq.md and prefetches its small neighbours.
        self.assertEqual((view / "docs" / "faq.md").read_bytes(), b"faq\n")
        self.assertEqual((view / "docs" / "notes.md").read_bytes(), b"notes\n")
        (view / "docs" / "guide.md").write_bytes(b"guide v2\n")  # truncating write: nothing to fetch
        (view / "new.txt").write_bytes(b"new\n")
        synced = self.tri("workspace", "sync", ws)["data"]
        self.assertEqual(synced["written"], ["docs/guide.md", "new.txt"])
        counters = synced["virtual"]["counters"]
        self.assertEqual(counters["files_faulted"], 3)
        self.assertEqual(counters["files_prefetched"], 2)
        self.assertEqual(counters["cache_hits"], 1, "notes.md came from the prefetch")
        self.assertEqual(counters["bytes_fetched"], len(b"print('v1')\n") + len(b"faq\n") + len(b"notes\n") + len(b"guide\n"))
        self.assertEqual(self.tri("files", "read", ws, "docs/guide.md")["data"]["text"], "guide v2\n")
        self.assertEqual(self.tri("workspace", "sync", ws)["data"]["written"], [])
        self.tri("workspace", "unmount", ws)
        self.assertFalse(fuse.is_mounted(view.resolve()))
        # A second view of the same content is served from the machine cache.
        again = self.tri("workspace", "mount", ws, str(view), "--virtual")["data"]
        state_dir = Path(json.loads(mount_state_path(view.resolve()).read_text())["virtual"])
        self.track_mount(view.resolve(), state_dir)
        self.assertEqual((view / "src" / "app.py").read_bytes(), b"print('v1')\n")
        (view / "scratch.txt").write_bytes(b"x\n")
        refused = self.tri("workspace", "unmount", ws, ok=False)
        self.assertEqual(refused["diagnostics"][0]["code"], "unsynced_changes")
        final = self.tri("workspace", "unmount", ws, "--force")["data"]["virtual"]["counters"]
        self.assertEqual(final["cache_hits"], 1)
        self.assertEqual(final["bytes_fetched"], 0)
        self.assertIsNotNone(again)


class VirtualBenchmarkTests(unittest.TestCase):
    def test_benchmark_runs_and_records_bytes_fetched(self) -> None:
        from benchmarks import virtual as bench

        modes = "full,paths" + ("" if fuse_skip_reason() else ",virtual")
        with tempfile.TemporaryDirectory(prefix="tri-bench-virtual-test-") as out:
            with mock.patch("sys.stdout"):
                self.assertEqual(bench.main(["--files", "40", "--packages", "4", "--file-size", "64", "--modes", modes, "--transports", "local", "--output-dir", out]), 0)
            report = json.loads((Path(out) / "results.json").read_text())
            rows = {row["mode"]: row for row in report["results"]}
            self.assertEqual(rows["full"]["files_on_disk"], 40)
            self.assertEqual(rows["full"]["bytes_fetched"], 40 * 64)
            self.assertEqual(rows["paths"]["files_on_disk"], 10)
            if "virtual" in rows:
                self.assertEqual(rows["virtual"]["files_fetched"], 1)
                self.assertEqual(rows["virtual"]["bytes_fetched"], 64)
                self.assertEqual(rows["virtual"]["package_walk_files_fetched"], 10)
            self.assertIn("| local | full |", (Path(out) / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
