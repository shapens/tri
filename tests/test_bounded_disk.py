from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri.locking import FileLock

POSIX = os.name != "nt"


class FileLockTests(unittest.TestCase):
    def test_file_lock_shared_then_exclusive_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            a, b, c = FileLock(path), FileLock(path), FileLock(path)
            try:
                self.assertTrue(a.acquire(exclusive=False))
                self.assertTrue(b.acquire(exclusive=False))
                self.assertFalse(c.acquire(exclusive=True, blocking=False))
                a.release()
                b.release()
                self.assertTrue(c.acquire(exclusive=True, blocking=False))
                self.assertFalse(a.acquire(exclusive=False, blocking=False))
            finally:
                for lock in (a, b, c):
                    lock.close()


def rules_for(files: dict[str, str], extra: list[str] | None = None):
    from tri.ignore import IgnoreRules

    entries = {path: {"blob": path} for path in files}
    return IgnoreRules.from_entries(entries, lambda path: files.get(path), extra or [])


class IgnoreRulesTests(unittest.TestCase):
    def test_ignore_basic_globs(self) -> None:
        rules = rules_for({".gitignore": "# comment\n\n*.log\n__pycache__\n"})
        self.assertTrue(rules.matches("app.log"))
        self.assertTrue(rules.matches("deep/dir/app.log"))
        self.assertTrue(rules.matches("pkg/__pycache__", is_dir=True))
        self.assertTrue(rules.matches("pkg/__pycache__/mod.pyc"))
        self.assertFalse(rules.matches("app.txt"))
        self.assertFalse(rules.matches("# comment"))

    def test_ignore_negation_and_dir_only(self) -> None:
        rules = rules_for({".gitignore": "*.log\n!keep.log\nbuild/\n"})
        self.assertTrue(rules.matches("a.log"))
        self.assertFalse(rules.matches("keep.log"))
        self.assertTrue(rules.matches("build", is_dir=True))
        self.assertFalse(rules.matches("build", is_dir=False))
        self.assertTrue(rules.matches("build/out.js"))

    def test_ignore_anchored_and_nested(self) -> None:
        rules = rules_for({
            ".gitignore": "/dist\ndocs/*.html\n",
            "web/.gitignore": "/cache\n*.tmp\n",
        })
        self.assertTrue(rules.matches("dist/a.js"))
        self.assertFalse(rules.matches("src/dist/a.js"))
        self.assertTrue(rules.matches("docs/index.html"))
        self.assertFalse(rules.matches("docs/sub/index.html"))
        self.assertTrue(rules.matches("web/cache/x"))
        self.assertFalse(rules.matches("cache/x"))
        self.assertTrue(rules.matches("web/deep/a.tmp"))
        self.assertFalse(rules.matches("a.tmp"))

    def test_ignore_double_star_and_parent_dirs(self) -> None:
        rules = rules_for({".gitignore": "node_modules/\nlogs/**/debug.txt\na/**/b\n"}, extra=["target/**", "*.orig"])
        self.assertTrue(rules.matches("node_modules/a/b.js"))
        self.assertTrue(rules.matches("pkg/node_modules/x/index.js"))
        self.assertTrue(rules.matches("logs/debug.txt"))
        self.assertTrue(rules.matches("logs/x/y/debug.txt"))
        self.assertTrue(rules.matches("a/b"))
        self.assertTrue(rules.matches("a/x/y/b"))
        self.assertTrue(rules.matches("target/release/app"))
        self.assertTrue(rules.matches("src/merge.orig"))
        self.assertFalse(rules.matches("src/node_modules.txt"))


class FakeBlobs:
    """Minimal content store for materializer tests: blob_id -> bytes."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def entry(self, content: bytes, mode: str = "100644", target: str | None = None) -> dict:
        import hashlib

        digest = hashlib.sha256(content).hexdigest()
        blob_id = f"blob_{digest[:16]}"
        self.data[blob_id] = content
        entry = {"blob": blob_id, "hash": digest, "size": len(content), "mode": mode}
        if target is not None:
            entry["symlink_target"] = target
        return entry

    def read(self, blob_id: str) -> bytes:
        return self.data[blob_id]


def sample_entries(blobs: FakeBlobs) -> dict[str, dict]:
    return {
        "README.md": blobs.entry(b"hello\n"),
        "bin/run.sh": blobs.entry(b"#!/bin/sh\necho hi\n", mode="100755"),
        "src/app/main.py": blobs.entry(b"print('main')\n"),
        "src/app/util.py": blobs.entry(b"x = 1\n"),
        "link.md": blobs.entry(b"README.md", mode="120000", target="README.md"),
    }


class MaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        from tri.materialize import CheckoutCache, Materializer

        self.tmp = tempfile.TemporaryDirectory(prefix="tri-mat-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.blobs = FakeBlobs()
        self.cache = CheckoutCache(self.home / "checkout", self.blobs.read)
        self.materializer = Materializer(self.home, self.cache, read_blob=self.blobs.read)
        self.entries = sample_entries(self.blobs)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def assert_tree_matches(self, root: Path, entries: dict[str, dict]) -> None:
        from tri.materialize import FIXED_MTIME

        for path, entry in entries.items():
            target = root / path
            if entry["mode"] == "120000" and not POSIX and not target.is_symlink():
                continue  # Windows without symlink privilege leaves symlinks unmaterialized
            if entry["mode"] == "120000":
                self.assertTrue(target.is_symlink(), path)
                self.assertEqual(os.readlink(target), entry["symlink_target"])
                continue
            self.assertEqual(target.read_bytes(), self.blobs.read(entry["blob"]), path)
            st = target.stat()
            if POSIX:
                self.assertEqual(bool(st.st_mode & 0o100), entry["mode"] == "100755", path)
            self.assertTrue(st.st_mode & 0o200, f"{path} must be writable")
            self.assertEqual(int(st.st_mtime), FIXED_MTIME, path)

    def test_materialize_clone_and_copy_produce_identical_trees(self) -> None:
        from tri.materialize import probe_clone

        clone_dest = self.root / "clone"
        copy_dest = self.root / "copy"
        clone = self.materializer.materialize(self.entries, clone_dest)
        copied = self.materializer.materialize(self.entries, copy_dest, backend="copy")
        self.assert_tree_matches(clone_dest, self.entries)
        self.assert_tree_matches(copy_dest, self.entries)
        expected = "clone" if probe_clone(self.root) else "copy"
        self.assertEqual(clone.backend, expected)
        self.assertEqual(clone.degraded, expected == "copy")
        self.assertEqual(copied.backend, "copy")
        self.assertTrue(copied.degraded)

    def test_scan_detects_same_size_edit(self) -> None:
        dest = self.root / "w"
        mat = self.materializer.materialize(self.entries, dest)
        (dest / "src/app/util.py").write_bytes(b"x = 2\n")
        changed, deleted = mat.scan(self.entries)
        self.assertEqual(changed, ["src/app/util.py"])
        self.assertEqual(deleted, [])

    def test_scan_detects_rename_replace(self) -> None:
        dest = self.root / "w"
        mat = self.materializer.materialize(self.entries, dest)
        tmp = dest / "src/app/.main.py.swp"
        tmp.write_bytes(b"print('MAIN')\n")
        os.replace(tmp, dest / "src/app/main.py")
        changed, _ = mat.scan(self.entries)
        self.assertEqual(changed, ["src/app/main.py"])

    def test_scan_detects_delete_new_and_chmod(self) -> None:
        dest = self.root / "w"
        mat = self.materializer.materialize(self.entries, dest)
        (dest / "README.md").unlink()
        (dest / "src/new.txt").write_text("new\n")
        (dest / "src/app/main.py").chmod(0o755)
        changed, deleted = mat.scan(self.entries)
        self.assertEqual(changed, ["src/app/main.py", "src/new.txt"] if POSIX else ["src/new.txt"])
        self.assertEqual(deleted, ["README.md"])

    def test_scan_unchanged_tree_reports_nothing(self) -> None:
        dest = self.root / "w"
        mat = self.materializer.materialize(self.entries, dest)
        self.assertEqual(mat.scan(self.entries), ([], []))

    def test_scan_skips_ignored_new_files_but_not_tracked(self) -> None:
        from tri.ignore import IgnoreRules

        entries = dict(self.entries)
        entries["build/keep.txt"] = self.blobs.entry(b"keep\n")
        rules = IgnoreRules.from_patterns(["build/", "*.log"])
        dest = self.root / "w"
        mat = self.materializer.materialize(entries, dest)
        (dest / "build/out.o").write_bytes(b"obj")
        (dest / "debug.log").write_text("log")
        (dest / "build/keep.txt").write_text("changed\n")
        changed, deleted = mat.scan(entries, ignore=rules)
        self.assertEqual(changed, ["build/keep.txt"])
        self.assertEqual(deleted, [])

    def test_scan_skips_git_dirs_and_attachments(self) -> None:
        from tri.materialize import Attachment

        env = self.root / "env" / "node_modules"
        (env / "pkg").mkdir(parents=True)
        (env / "pkg" / "index.js").write_text("module.exports = 1\n")
        dest = self.root / "w"
        mat = self.materializer.materialize(self.entries, dest, attachments=[Attachment("node_modules", env, "clone")])
        self.assertEqual((dest / "node_modules/pkg/index.js").read_text(), "module.exports = 1\n")
        (dest / ".git").mkdir()
        (dest / ".git" / "HEAD").write_text("ref")
        (dest / "node_modules/pkg/index.js").write_text("changed")
        self.assertEqual(mat.scan(self.entries), ([], []))
        self.assertEqual(env.joinpath("pkg/index.js").read_text(), "module.exports = 1\n", "attachment source must not change")

    @unittest.skipUnless(POSIX, "symlinks need Developer Mode on Windows (copy fallback is tested separately)")
    def test_link_attachment_points_at_shared_source(self) -> None:
        from tri.materialize import Attachment

        env = self.root / "env" / "deps"
        env.mkdir(parents=True)
        dest = self.root / "w"
        self.materializer.materialize(self.entries, dest, attachments=[Attachment("deps", env, "link")], backend="copy")
        self.assertTrue((dest / "deps").is_symlink())
        self.assertEqual(Path(os.readlink(dest / "deps")), env)

    def test_baseline_roundtrip_in_new_process(self) -> None:
        from tri.materialize import load_materialization

        dest = self.root / "w"
        baseline = self.root / "baseline.json"
        mat = self.materializer.materialize(self.entries, dest)
        mat.save_baseline(baseline)
        (dest / "README.md").write_text("HELLO\n")
        loaded = load_materialization(dest, baseline)
        self.assertEqual(loaded.backend, mat.backend)
        self.assertEqual(loaded.scan(self.entries), (["README.md"], []))

    def test_release_removes_directory(self) -> None:
        dest = self.root / "w"
        mat = self.materializer.materialize(self.entries, dest)
        (dest / "src").chmod(0o555)
        mat.release()
        self.assertFalse(dest.exists())

    def test_remove_tree_handles_unreadable_directories(self) -> None:
        from tri.materialize import remove_tree

        root = self.root / "ovl"
        locked = root / "work" / "work"
        locked.mkdir(parents=True)
        (locked / "f").write_text("x")
        locked.chmod(0o000)
        (root / "work").chmod(0o000)
        remove_tree(root)
        self.assertFalse(root.exists())

    def test_symlink_escape_rejected(self) -> None:
        from tri.materialize import MaterializeError

        entries = {"evil": self.blobs.entry(b"../../etc/passwd", mode="120000", target="../../etc/passwd")}
        with self.assertRaises(MaterializeError) as ctx:
            self.materializer.materialize(entries, self.root / "w")
        self.assertEqual(ctx.exception.code, "path_escaped_mount")

    def test_base_dir_reused_and_delta_applied(self) -> None:
        from tri.materialize import clone_dir_supported

        base_entries = self.entries
        head = dict(base_entries)
        head["src/app/util.py"] = self.blobs.entry(b"x = 99\n")
        del head["README.md"]
        head["link.md"] = self.blobs.entry(b"bin/run.sh", mode="120000", target="bin/run.sh")
        head["src/new/file.txt"] = self.blobs.entry(b"new\n")
        first = self.materializer.materialize(head, self.root / "a", base=("tree_base1", base_entries))
        second = self.materializer.materialize(head, self.root / "b", base=("tree_base1", base_entries))
        self.assert_tree_matches(self.root / "a", head)
        self.assert_tree_matches(self.root / "b", head)
        self.assertFalse((self.root / "a" / "README.md").exists())
        if first.backend == "clone" and clone_dir_supported():
            self.assertTrue(first.used_base)
            self.assertTrue(second.used_base)
            self.assertEqual(sorted(p.name for p in (self.home / "bases").iterdir() if not p.name.startswith(".")), ["tree_base1"])
        self.assertEqual(first.scan(head), ([], []))

    @unittest.skipUnless(sys.platform == "darwin", "APFS block sharing check")
    def test_clone_backend_shares_blocks(self) -> None:
        from tri.materialize import probe_clone

        if not probe_clone(self.root):
            self.skipTest("volume cannot clone")
        big = {"big.bin": self.blobs.entry(os.urandom(20 * 1024 * 1024))}
        self.materializer.materialize(big, self.root / "warm")
        before = os.statvfs(self.root)
        for i in range(20):
            self.materializer.materialize(big, self.root / f"c{i}")
        after = os.statvfs(self.root)
        used = (before.f_bavail - after.f_bavail) * after.f_frsize
        self.assertLess(used, 100 * 1024 * 1024, f"20 clones of 20 MiB used {used} bytes")


class TriHarness(unittest.TestCase):
    """Runs Tri commands in-process against an isolated TRI_HOME."""

    files: dict[str, str] = {"README.md": "hello\n", "src/app.txt": "one\n"}
    executables: tuple[str, ...] = ()

    def setUp(self) -> None:
        import subprocess

        self.tmp = tempfile.TemporaryDirectory(prefix="tri-bd-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._env_backup = {k: os.environ.get(k) for k in ("TRI_HOME", "TRI_ACTOR", "TRI_AUTO_REAP", "TRI_MATERIALIZE_BACKEND")}
        os.environ["TRI_HOME"] = str(self.root / "tri-home")
        os.environ.pop("TRI_ACTOR", None)
        os.environ.pop("TRI_MATERIALIZE_BACKEND", None)
        os.environ["TRI_AUTO_REAP"] = "0"
        run = lambda *a: subprocess.run(a, cwd=self.repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        run("git", "init", "-b", "main")
        run("git", "config", "user.email", "tri@example.com")
        run("git", "config", "user.name", "Tri Test")
        for path, text in self.files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        for path in self.executables:
            (self.repo / path).chmod(0o755)
        run("git", "add", "-f", ".")
        run("git", "commit", "-m", "initial")
        self.tri("import", str(self.repo), "--name", "app")

    def tearDown(self) -> None:
        from tri.materialize import remove_tree

        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        remove_tree(self.root)
        self.tmp.cleanup()

    def tri(self, *argv: str, ok: bool = True) -> dict:
        from tri.cli import execute_argv

        output, status = execute_argv(list(argv))
        if ok:
            self.assertEqual(status, 0, output.get("diagnostics"))
        else:
            self.assertNotEqual(status, 0, output)
        return output

    def workspace(self) -> str:
        return self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]

    def run_cmd(self, ws: str, script: str, ok: bool = True) -> dict:
        return self.tri("run", ws, "--", "sh", "-c", script, ok=ok)["data"]

    def changed(self, check: dict) -> list[str]:
        paths: list[str] = []
        for op in check["file_changes"]:
            paths.extend(self.tri("operations", "inspect", op)["data"]["paths"])
        return sorted(paths)


class RunImportTests(TriHarness):
    files = {
        "README.md": "hello\n",
        ".gitignore": "build/\n*.log\n",
        "build/keep.txt": "keep\n",
        "bin/tool.sh": "#!/bin/sh\necho tool\n",
    }
    executables = ("bin/tool.sh",)

    def test_run_does_not_import_gitignored_outputs(self) -> None:
        ws = self.workspace()
        check = self.run_cmd(ws, "mkdir -p build/x && echo o > build/x/out.o && echo l > app.log && echo n > new.txt")
        self.assertEqual(self.changed(check), ["new.txt"])

    def test_run_imports_edit_to_tracked_file_under_ignored_dir(self) -> None:
        ws = self.workspace()
        check = self.run_cmd(ws, "echo changed > build/keep.txt && rm README.md")
        self.assertEqual(self.changed(check), ["README.md", "build/keep.txt"])
        files = {f["path"] for f in self.tri("files", "list", ws)["data"]["files"]}
        self.assertNotIn("README.md", files)

    def test_run_preserves_executable_mode(self) -> None:
        ws = self.workspace()
        check = self.run_cmd(ws, "./bin/tool.sh")
        self.assertEqual(check["status"], "passed")
        self.assertEqual(self.changed(check), [])

    def test_check_reports_backend(self) -> None:
        from tri.materialize import probe_clone

        ws = self.workspace()
        check = self.run_cmd(ws, "true")
        expected = "clone" if probe_clone(Path(os.environ["TRI_HOME"])) else "copy"
        self.assertEqual(check["materialized_directory"]["backend"], expected)
        self.assertEqual(check["materialized_directory"]["degraded"], expected == "copy")
        os.environ["TRI_MATERIALIZE_BACKEND"] = "copy"
        check = self.run_cmd(ws, "true")
        self.assertEqual(check["materialized_directory"]["backend"], "copy")
        self.assertTrue(check["materialized_directory"]["degraded"])

    def test_run_directory_removed_after_run(self) -> None:
        ws = self.workspace()
        self.run_cmd(ws, "true")
        runs = Path(os.environ["TRI_HOME"]) / "runs"
        self.assertEqual([p for p in runs.iterdir()], [])

    def test_sync_uses_baseline_and_ignore_rules(self) -> None:
        ws = self.workspace()
        mount = self.root / "mounted"
        mounted = self.tri("workspace", "mount", ws, str(mount))["data"]
        self.assertIn(mounted["materialization"]["backend"], ("clone", "copy"))
        (mount / "README.md").write_text("HELLO\n")
        (mount / "debug.log").write_text("noise\n")
        sync = self.tri("workspace", "sync", ws)["data"]
        self.assertEqual(sync["paths"], ["README.md"])
        again = self.tri("workspace", "sync", ws)["data"]
        self.assertEqual(again["paths"], [])

    def test_doctor_reports_materialization(self) -> None:
        data = self.tri("doctor")["data"]
        self.assertIn(data["materialization"]["store_volume"]["backend"], ("clone", "copy"))
        self.assertIn("checkout_cache_bytes", data["materialization"]["caches"])

    def test_ignore_commands(self) -> None:
        added = self.tri("ignore", "add", "app", "coverage/**", "*.tmp")["data"]
        self.assertEqual(added["patterns"], ["*.tmp", "coverage/**"])
        ws = self.workspace()
        check = self.run_cmd(ws, "mkdir -p coverage && echo c > coverage/x && echo t > a.tmp && echo k > keep.md")
        self.assertEqual(self.changed(check), ["keep.md"])
        removed = self.tri("ignore", "remove", "app", "*.tmp")["data"]
        self.assertEqual(removed["patterns"], ["coverage/**"])
        self.assertEqual(self.tri("ignore", "list", "app")["data"]["patterns"], ["coverage/**"])


class LeaseLifecycleTests(TriHarness):
    files = {"README.md": "hello\n", ".gitignore": "*.log\nnode_modules/\n", "src/app.txt": "one\n"}

    def expire(self, lease: str) -> None:
        from tri.cli import Store

        store = Store()
        try:
            with store.conn:
                store.conn.execute("UPDATE materialization_leases SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00Z", lease))
        finally:
            store.close()

    def mount(self, ws: str, name: str = "mounted", *extra: str) -> tuple[Path, str]:
        path = self.root / name
        data = self.tri("workspace", "mount", ws, str(path), *extra)["data"]
        return path, data["lease"]

    def test_mount_defaults_to_24h_ttl(self) -> None:
        from datetime import datetime, timezone

        ws = self.workspace()
        _, lease = self.mount(ws)
        data = self.tri("leases", "inspect", lease)["data"]["lease"]
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(remaining, 86400 - 120)
        self.assertLessEqual(remaining, 86400)
        self.assertEqual(data["ttl_seconds"], 86400)

    def test_run_lease_released_and_dir_removed(self) -> None:
        ws = self.workspace()
        self.run_cmd(ws, "true")
        leases = self.tri("leases", "list", "--workspace", ws)["data"]
        runs = [lease for lease in leases["leases"] if lease["kind"] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "released")
        self.assertFalse(Path(runs[0]["path"]).exists())

    @unittest.skipUnless(POSIX, "uses kill -9 to crash the run")
    def test_orphaned_run_dir_is_salvaged_and_removed(self) -> None:
        import subprocess

        ws = self.workspace()
        head = self.tri("workspace", "inspect", ws)["data"]["head_snapshot"]
        crashed = subprocess.run(
            [sys.executable, "-m", "tri", "run", ws, "--", "sh", "-c", "echo rescued > new.txt && echo junk > crash.log && kill -9 $PPID"],
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(crashed.returncode, -9)
        run_dirs = [p for p in (Path(os.environ["TRI_HOME"]) / "runs").iterdir()]
        self.assertEqual(len(run_dirs), 1)
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual(len(reaped["salvaged"]), 1)
        salvage = reaped["salvaged"][0]
        self.assertEqual(salvage["paths"], ["new.txt"])
        self.assertEqual([p for p in (Path(os.environ["TRI_HOME"]) / "runs").iterdir()], [])
        self.assertEqual(self.tri("workspace", "inspect", ws)["data"]["head_snapshot"], head)
        listed = self.tri("salvage", "list", ws)["data"]["salvages"]
        self.assertEqual([s["id"] for s in listed], [salvage["id"]])

    def test_live_run_lease_is_not_reaped(self) -> None:
        from tri.cli import Store, reap_leases
        from tri.locking import FileLock

        ws = self.workspace()
        store = Store()
        try:
            run_dir = store.runs / "check_live"
            (run_dir / "work").mkdir(parents=True)
            holder = FileLock(run_dir / ".holder.lock")
            holder.acquire(exclusive=True)
            with store.conn:
                store.conn.execute(
                    "INSERT INTO disk_leases (id, kind, path, workspace_id, check_id, before_snapshot_id, baseline_path, holder_lock, backend, status, created_at) VALUES (?, 'run', ?, ?, 'check_live', NULL, NULL, ?, 'copy', 'active', ?)",
                    ("lease_live", str(run_dir / "work"), ws, str(run_dir / ".holder.lock"), "2000-01-01T00:00:00Z"),
                )
            result = reap_leases(store, actor="actor_local")
            self.assertEqual(result["reaped"], [])
            self.assertTrue(run_dir.exists())
            holder.close()
            result = reap_leases(store, actor="actor_local")
            self.assertEqual([r["lease"] for r in result["reaped"]], ["lease_live"])
            self.assertFalse(run_dir.exists())
        finally:
            store.close()

    def test_expired_mount_salvaged_then_deleted(self) -> None:
        ws = self.workspace()
        head = self.tri("workspace", "inspect", ws)["data"]["head_snapshot"]
        mount, lease = self.mount(ws)
        (mount / "README.md").write_text("unsynced edit\n")
        (mount / ".DS_Store").write_bytes(b"finder")
        (mount / "node_modules" / "dep").mkdir(parents=True)
        (mount / "node_modules" / "dep" / "index.js").write_text("x\n")
        (mount / "node_modules" / "dep" / ".env.example").write_text("A=1\n")
        self.expire(lease)
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual([r["lease"] for r in reaped["reaped"]], [lease])
        self.assertFalse(mount.exists())
        salvage = reaped["salvaged"][0]
        self.assertEqual(salvage["paths"], ["README.md"])
        self.assertIn("node_modules/", salvage["deleted_ignored"]["paths"])
        self.assertEqual(reaped["reaped"][0]["deleted_ignored"]["count"], 3)
        self.assertEqual(self.tri("workspace", "inspect", ws)["data"]["head_snapshot"], head)
        inspected = self.tri("leases", "inspect", lease)["data"]["lease"]
        self.assertEqual(inspected["status"], "reaped")
        self.assertEqual(inspected["salvage"], salvage["id"])
        content = self.tri("files", "read", salvage["snapshot"], "README.md")["data"]
        self.assertIn("unsynced edit", json.dumps(content))

    def test_secret_like_file_blocks_reap_until_forced(self) -> None:
        ws = self.workspace()
        mount, lease = self.mount(ws)
        (mount / ".env.local").write_text("TOKEN=hand-written\n")
        self.expire(lease)
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual(reaped["reaped"], [])
        self.assertEqual([r["lease"] for r in reaped["needs_attention"]], [lease])
        self.assertTrue((mount / ".env.local").exists())
        inspected = self.tri("leases", "inspect", lease)["data"]["lease"]
        self.assertEqual(inspected["status"], "expired_needs_attention")
        self.assertEqual(inspected["attention"]["secret_like_paths"], [".env.local"])
        doctor = self.tri("doctor")["data"]["materialization"]["leases"]
        self.assertEqual(doctor["needs_attention"], [lease])
        forced = self.tri("leases", "reap", lease, "--force")["data"]
        self.assertEqual([r["lease"] for r in forced["reaped"]], [lease])
        self.assertFalse(mount.exists())

    def test_user_git_dir_blocks_reap(self) -> None:
        ws = self.workspace()
        mount, lease = self.mount(ws)
        (mount / ".git").mkdir()
        (mount / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        self.expire(lease)
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual([r["lease"] for r in reaped["needs_attention"]], [lease])
        self.assertTrue(mount.exists())

    def test_no_ttl_and_grandfathered_mounts_not_reaped(self) -> None:
        from tri.cli import Store

        ws = self.workspace()
        mount_a, lease_a = self.mount(ws, "a", "--no-ttl")
        mount_b, lease_b = self.mount(ws, "b")
        self.assertIsNone(self.tri("leases", "inspect", lease_a)["data"]["lease"]["expires_at"])
        store = Store()
        try:
            with store.conn:
                store.conn.execute("UPDATE materialization_leases SET grandfathered = 1, expires_at = ?, status = 'revoked' WHERE id = ?", ("2000-01-01T00:00:00Z", lease_b))
        finally:
            store.close()
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual(reaped["reaped"], [])
        self.assertTrue(mount_a.exists())
        self.assertTrue(mount_b.exists())
        self.assertIn(lease_b, self.tri("doctor")["data"]["materialization"]["leases"]["grandfathered"])

    def test_sync_and_renew_extend_lease(self) -> None:
        ws = self.workspace()
        mount, lease = self.mount(ws, "m", "--ttl-seconds", "100")
        first = self.tri("leases", "inspect", lease)["data"]["lease"]["expires_at"]
        import time

        time.sleep(1.1)
        (mount / "README.md").write_text("changed\n")
        self.tri("workspace", "sync", ws)
        second = self.tri("leases", "inspect", lease)["data"]["lease"]["expires_at"]
        self.assertGreater(second, first)
        renewed = self.tri("leases", "renew", lease, "--ttl-seconds", "5000")["data"]
        self.assertGreater(renewed["expires_at"], second)
        self.assertEqual(renewed["ttl_seconds"], 5000)

    def test_discard_reaps_mounts_with_salvage(self) -> None:
        ws = self.workspace()
        mount, lease = self.mount(ws)
        (mount / "src" / "app.txt").write_text("unsynced\n")
        result = self.tri("workspace", "discard", ws)["data"]
        self.assertFalse(mount.exists())
        self.assertEqual([r["lease"] for r in result["reaped_leases"]["reaped"]], [lease])
        self.assertEqual(result["reaped_leases"]["salvaged"][0]["paths"], ["src/app.txt"])

    def test_salvage_adopt(self) -> None:
        ws = self.workspace()
        mount, lease = self.mount(ws)
        (mount / "README.md").write_text("rescued\n")
        self.expire(lease)
        salvage = self.tri("leases", "reap")["data"]["salvaged"][0]
        adopted = self.tri("salvage", "adopt", salvage["id"])["data"]
        self.assertEqual(adopted["paths"], ["README.md"])
        self.assertEqual(self.tri("workspace", "inspect", ws)["data"]["head_snapshot"], adopted["snapshot"])
        listed = self.tri("salvage", "list", ws)["data"]["salvages"]
        self.assertIsNotNone(listed[0]["adopted_at"])
        again = self.tri("salvage", "adopt", salvage["id"], ok=False)
        self.assertEqual(again["diagnostics"][0]["code"], "salvage_already_adopted")

    def test_salvage_adopt_refuses_moved_head(self) -> None:
        ws = self.workspace()
        mount, lease = self.mount(ws)
        (mount / "README.md").write_text("rescued\n")
        self.expire(lease)
        salvage = self.tri("leases", "reap")["data"]["salvaged"][0]
        self.tri("files", "write", ws, "src/app.txt", "--text", "moved\n")
        refused = self.tri("salvage", "adopt", salvage["id"], ok=False)
        self.assertEqual(refused["diagnostics"][0]["code"], "stale_workspace_head")

    def test_auto_reap_runs_after_commands_when_due(self) -> None:
        from tri.cli import Store

        ws = self.workspace()
        mount, lease = self.mount(ws)
        self.expire(lease)
        os.environ["TRI_AUTO_REAP"] = "1"
        store = Store()
        try:
            with store.conn:
                store.conn.execute("DELETE FROM store_meta WHERE key = 'last_reap_at'")
        finally:
            store.close()
        self.tri("workspace", "list")
        self.assertFalse(mount.exists())


class EnvCacheTests(TriHarness):
    files = {"README.md": "hello\n", "deps.lock": "v1\n", "src/app.txt": "one\n"}

    def setUp(self) -> None:
        super().setUp()
        self.build_log = self.root / "builds.log"
        os.environ["TRI_TEST_BUILD_LOG"] = str(self.build_log)

    def tearDown(self) -> None:
        os.environ.pop("TRI_TEST_BUILD_LOG", None)
        super().tearDown()

    def define(self, *extra: str, install: str | None = None) -> dict:
        install = install or 'sh -c "echo build >> $TRI_TEST_BUILD_LOG && mkdir -p deps/pkg && cat deps.lock > deps/pkg/lib.txt"'
        return self.tri("env", "define", "app", "deps", "--key-file", "deps.lock", "--install", install, "--output", "deps", *extra)["data"]

    def builds(self) -> int:
        return len(self.build_log.read_text().splitlines()) if self.build_log.exists() else 0

    def test_env_commands(self) -> None:
        recipe = self.define()
        self.assertEqual(recipe["outputs"], ["deps"])
        self.assertEqual(recipe["key_files"], ["deps.lock"])
        self.assertEqual(recipe["mode"], "auto")
        listed = self.tri("env", "list", "app")["data"]["recipes"]
        self.assertEqual([r["name"] for r in listed], ["deps"])
        inspected = self.tri("env", "inspect", "app", "deps")["data"]
        self.assertEqual(inspected["recipe"]["name"], "deps")
        self.assertEqual(inspected["builds"], [])
        self.tri("env", "remove", "app", "deps")
        self.assertEqual(self.tri("env", "list", "app")["data"]["recipes"], [])

    def test_env_built_once_and_shared_across_workspaces(self) -> None:
        self.define()
        for _ in range(2):
            ws = self.workspace()
            check = self.run_cmd(ws, "cat deps/pkg/lib.txt")
            self.assertEqual(check["status"], "passed")
            self.assertEqual(self.changed(check), [])
            envs = check["materialized_directory"]["environments"]
            self.assertEqual([e["recipe"] for e in envs], ["deps"])
        self.assertEqual(self.builds(), 1)
        builds = self.tri("env", "inspect", "app", "deps")["data"]["builds"]
        self.assertEqual([b["status"] for b in builds], ["ready"])

    def test_env_rebuilt_when_key_file_changes(self) -> None:
        self.define()
        ws_a = self.workspace()
        ws_b = self.workspace()
        self.run_cmd(ws_a, "true")
        self.tri("files", "write", ws_b, "deps.lock", "--text", "v2\n")
        check = self.run_cmd(ws_b, "grep v2 deps/pkg/lib.txt")
        self.assertEqual(check["status"], "passed")
        self.assertEqual(self.builds(), 2)
        self.run_cmd(self.workspace(), "grep v1 deps/pkg/lib.txt")
        self.assertEqual(self.builds(), 2)

    def test_env_outputs_not_imported_and_source_untouched(self) -> None:
        self.define()
        ws = self.workspace()
        check = self.run_cmd(ws, "echo mutated > deps/pkg/lib.txt && echo x > deps/new.txt && echo y > app.out")
        self.assertEqual(self.changed(check), ["app.out"])
        builds = self.tri("env", "inspect", "app", "deps")["data"]["builds"]
        shared = Path(builds[0]["path"]) / "deps" / "pkg" / "lib.txt"
        self.assertEqual(shared.read_text(), "v1\n")

    def test_env_build_failure_fails_run_with_diagnostic(self) -> None:
        self.define(install="sh -c 'echo nope >&2; exit 3'")
        ws = self.workspace()
        failed = self.tri("run", ws, "--", "true", ok=False)
        diagnostic = failed["diagnostics"][0]
        self.assertEqual(diagnostic["code"], "env_build_failed")
        self.assertEqual(diagnostic["exit_status"], 3)
        self.assertEqual([p for p in (Path(os.environ["TRI_HOME"]) / "runs").iterdir()], [])

    def test_env_missing_output_fails_build(self) -> None:
        self.define(install="true")
        failed = self.tri("run", self.workspace(), "--", "true", ok=False)
        self.assertEqual(failed["diagnostics"][0]["code"], "env_output_missing")

    def test_no_env_flag_skips_env(self) -> None:
        self.define()
        check = self.tri("run", self.workspace(), "--no-env", "--", "test", "!", "-e", "deps")["data"]
        self.assertEqual(check["status"], "passed")
        self.assertEqual(self.builds(), 0)

    def test_env_private_mode_installs_per_run(self) -> None:
        self.define("--mode", "private")
        ws = self.workspace()
        for _ in range(2):
            check = self.run_cmd(ws, "cat deps/pkg/lib.txt")
            self.assertEqual(check["status"], "passed")
            self.assertEqual(self.changed(check), [])
        self.assertEqual(self.builds(), 2)

    def test_concurrent_runs_share_one_env_build(self) -> None:
        import subprocess

        self.define(install='sh -c "sleep 1 && echo build >> $TRI_TEST_BUILD_LOG && mkdir -p deps/pkg && cat deps.lock > deps/pkg/lib.txt"')
        workspaces = [self.workspace(), self.workspace()]
        procs = [
            subprocess.Popen([sys.executable, "-m", "tri", "run", ws, "--", "cat", "deps/pkg/lib.txt"], cwd=ROOT, env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for ws in workspaces
        ]
        results = [json.loads(proc.communicate(timeout=60)[0]) for proc in procs]
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(self.builds(), 1)
        statuses = sorted(result["data"]["materialized_directory"]["environments"][0]["status"] for result in results)
        self.assertEqual(statuses, ["built", "reused"])

    def test_copy_backend_runs_can_write_into_env_outputs(self) -> None:
        self.define()
        os.environ["TRI_MATERIALIZE_BACKEND"] = "copy"
        check = self.run_cmd(self.workspace(), "echo mutated > deps/pkg/lib.txt && echo y > app.out")
        self.assertEqual(check["status"], "passed", "tools write into dependency/build dirs (cargo target/, node_modules/.cache)")
        self.assertEqual(self.changed(check), ["app.out"])
        build = self.tri("env", "inspect", "app", "deps")["data"]["builds"][0]
        self.assertEqual((Path(build["path"]) / "deps" / "pkg" / "lib.txt").read_text(), "v1\n")

    def test_overlay_env_layer_is_a_writable_copy(self) -> None:
        from tri.cli import writable_env_layer

        self.define()
        self.run_cmd(self.workspace(), "true")
        build = self.tri("env", "inspect", "app", "deps")["data"]["builds"][0]
        frozen = Path(build["path"]) / "deps" / "pkg" / "lib.txt"
        layer = writable_env_layer(Path(build["path"]))
        self.assertNotEqual(layer, Path(build["path"]))
        self.assertTrue((layer / "deps" / "pkg" / "lib.txt").stat().st_mode & 0o200, "overlay layers must be writable by the caller")
        self.assertFalse(frozen.stat().st_mode & 0o222, "the shared env stays frozen for symlinked use")
        self.assertEqual(writable_env_layer(Path(build["path"])), layer, "built once per env")

    def test_env_attached_to_mount(self) -> None:
        from tri.materialize import probe_clone

        self.define()
        ws = self.workspace()
        mount = self.root / "m"
        self.tri("workspace", "mount", ws, str(mount))
        if probe_clone(Path(os.environ["TRI_HOME"])):
            self.assertTrue((mount / "deps").is_dir())
            self.assertFalse((mount / "deps").is_symlink())
            (mount / "deps" / "pkg" / "lib.txt").write_text("agent edit\n")
            self.assertEqual(self.tri("workspace", "sync", ws)["data"]["paths"], [])
        os.environ["TRI_MATERIALIZE_BACKEND"] = "copy"
        plain = self.root / "plain"
        self.tri("workspace", "mount", ws, str(plain))
        self.assertFalse((plain / "deps").exists())
        linked = self.root / "linked"
        self.tri("workspace", "mount", ws, str(linked), "--env-link")
        self.assertTrue((linked / "deps").is_symlink())
        self.assertEqual((linked / "deps" / "pkg" / "lib.txt").read_text(), "v1\n")


class GcTests(TriHarness):
    files = {"README.md": "hello\n", "deps.lock": "v1\n"}

    def blob_path(self, ws: str, path: str) -> Path:
        stat = self.tri("files", "stat", ws, path)["data"]
        digest = stat["hash"].removeprefix("sha256:") if "hash" in stat else None
        if digest is None:
            from tri.cli import Store

            store = Store()
            try:
                digest = store.row("SELECT hash FROM blobs WHERE id = ?", (stat["blob"],))["hash"]
            finally:
                store.close()
        return Path(os.environ["TRI_HOME"]) / "objects" / "blobs" / digest[:2] / digest

    def discarded_workspace_with(self, text: str) -> tuple[str, Path]:
        ws = self.workspace()
        self.tri("files", "write", ws, "unique.txt", "--text", text)
        blob = self.blob_path(ws, "unique.txt")
        self.assertTrue(blob.exists())
        self.tri("workspace", "discard", ws)
        return ws, blob

    def test_gc_dry_run_changes_nothing(self) -> None:
        ws, blob = self.discarded_workspace_with("only in a discarded workspace " * 20)
        plan = self.tri("gc", "--retention-days", "0", "--grace-seconds", "0", "--unsafe-no-grace")["data"]
        self.assertFalse(plan["applied"])
        self.assertGreaterEqual(plan["plan"]["blobs"]["count"], 1)
        self.assertTrue(blob.exists())

    def test_gc_purges_discarded_workspace_content_after_retention(self) -> None:
        ws, blob = self.discarded_workspace_with("only in a discarded workspace " * 20)
        result = self.tri("gc", "--apply", "--retention-days", "0", "--grace-seconds", "0", "--unsafe-no-grace")["data"]
        self.assertTrue(result["applied"])
        self.assertGreaterEqual(result["purged"]["blobs"], 1)
        self.assertFalse(blob.exists())
        denied = self.tri("files", "read", ws, "unique.txt", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "content_purged")
        self.assertEqual(self.tri("files", "read", self.workspace(), "README.md")["data"]["text"], "hello\n")

    def test_gc_keeps_live_recent_and_retained(self) -> None:
        live = self.workspace()
        self.tri("files", "write", live, "live.txt", "--text", "live content " * 20)
        live_blob = self.blob_path(live, "live.txt")
        _, retained_blob = self.discarded_workspace_with("discarded but within retention " * 20)
        self.tri("gc", "--apply", "--grace-seconds", "0", "--unsafe-no-grace")
        self.assertTrue(live_blob.exists())
        self.assertTrue(retained_blob.exists())
        _, recent_blob = self.discarded_workspace_with("discarded just now " * 20)
        self.tri("gc", "--apply", "--retention-days", "0")
        self.assertTrue(recent_blob.exists(), "grace period protects freshly written objects")

    def test_put_blob_after_purge_rewrites(self) -> None:
        text = "content that comes back " * 20
        _, blob = self.discarded_workspace_with(text)
        self.tri("gc", "--apply", "--retention-days", "0", "--grace-seconds", "0", "--unsafe-no-grace")
        self.assertFalse(blob.exists())
        ws = self.workspace()
        self.tri("files", "write", ws, "again.txt", "--text", text)
        self.assertEqual(self.tri("files", "read", ws, "again.txt")["data"]["text"], text)
        self.assertTrue(blob.exists())

    def test_env_cache_eviction_respects_budget_and_leases(self) -> None:
        self.tri("env", "define", "app", "deps", "--key-file", "deps.lock", "--install", "sh -c 'mkdir -p deps && echo x > deps/x'", "--output", "deps", "--mode", "link")
        ws = self.workspace()
        mount = self.root / "m"
        self.tri("workspace", "mount", ws, str(mount))
        os.environ["TRI_ENV_CACHE_MAX_BYTES"] = "1"
        os.environ["TRI_ENV_EVICT_GRACE_SECONDS"] = "0"
        try:
            self.tri("gc", "--reap")
            build = self.tri("env", "inspect", "app", "deps")["data"]["builds"][0]
            self.assertEqual(build["status"], "ready", "env linked into a live mount must not be evicted")
            self.tri("leases", "reap", self.tri("leases", "list", "--workspace", ws)["data"]["leases"][0]["id"], "--force")
            result = self.tri("gc", "--reap")["data"]
            self.assertEqual([e["key"] for e in result["evicted"]["envs"]], [build["key"]])
            self.assertEqual(self.tri("env", "inspect", "app", "deps")["data"]["builds"][0]["status"], "evicted")
            self.assertFalse(Path(build["path"]).exists())
            check = self.run_cmd(self.workspace(), "cat deps/x")
            self.assertEqual(check["status"], "passed")
        finally:
            os.environ.pop("TRI_ENV_CACHE_MAX_BYTES", None)
            os.environ.pop("TRI_ENV_EVICT_GRACE_SECONDS", None)

    def test_checkout_cache_and_base_eviction(self) -> None:
        ws = self.workspace()
        self.run_cmd(ws, "true")
        os.environ["TRI_CHECKOUT_CACHE_MAX_BYTES"] = "1"
        os.environ["TRI_BASE_CACHE_MAX_COUNT"] = "0"
        try:
            result = self.tri("gc", "--reap")["data"]
            self.assertIn("checkout_cache_bytes_freed", result["evicted"])
            doctor = self.tri("doctor")["data"]["materialization"]["caches"]
            self.assertEqual(doctor["checkout_cache_bytes"], 0)
            self.assertEqual(doctor["base_dirs"], 0)
            self.assertEqual(self.run_cmd(ws, "cat README.md")["status"], "passed")
        finally:
            os.environ.pop("TRI_CHECKOUT_CACHE_MAX_BYTES", None)
            os.environ.pop("TRI_BASE_CACHE_MAX_COUNT", None)


class HierarchicalTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        from tri.cli import Store

        self.tmp = tempfile.TemporaryDirectory(prefix="tri-trees-")
        self.store = Store(Path(self.tmp.name) / "home")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def entries(self, files: dict[str, bytes]) -> dict[str, dict]:
        out = {}
        for path, data in files.items():
            blob = self.store.put_blob(data)
            out[path] = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": "100644"}
        return dict(sorted(out.items()))

    def tree_count(self) -> int:
        return self.store.row("SELECT COUNT(*) AS c FROM trees", ())["c"]

    def test_hier_tree_roundtrip(self) -> None:
        from tri.cli import symlink_entry

        entries = self.entries({"README.md": b"r", "a/b/c.txt": b"c", "a/d.txt": b"d", "z/y/x/w.txt": b"w"})
        entries["link"] = symlink_entry(self.store, "README.md")
        entries = dict(sorted(entries.items()))
        tree = self.store.put_tree(entries)
        self.store.conn.commit()
        self.store._tree_cache.clear()
        self.assertEqual(self.store.load_tree(tree["id"]), entries)
        trees, blobs = self.store.tree_references(tree["id"])
        self.assertEqual(blobs, {entry["blob"] for entry in entries.values()})
        self.assertGreaterEqual(len(trees), 6)

    def test_one_edit_adds_depth_nodes_only(self) -> None:
        files = {f"d{i}/sub{j}/f{k}.txt": f"{i}-{j}-{k}".encode() for i in range(10) for j in range(3) for k in range(5)}
        entries = self.entries(files)
        self.store.put_tree(entries)
        before = self.tree_count()
        edited = dict(entries)
        edited.update(self.entries({"d3/sub1/f2.txt": b"changed"}))
        self.store.put_tree(edited)
        self.assertEqual(self.tree_count() - before, 3, "root + d3 + d3/sub1")

    def test_identical_content_reuses_tree(self) -> None:
        entries = self.entries({"a/b.txt": b"b", "c.txt": b"c"})
        first = self.store.put_tree(entries)
        count = self.tree_count()
        second = self.store.put_tree(dict(entries))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.tree_count(), count)

    def test_flat_trees_still_load(self) -> None:
        from tri.cli import encode_tree_json, sha256, utcnow

        entries = self.entries({"a/b.txt": b"b", "c.txt": b"c"})
        payload, stored = encode_tree_json(entries)
        digest = sha256(payload)
        self.store.conn.execute("INSERT INTO trees (id, hash, entries_json, created_at) VALUES (?, ?, ?, ?)", (f"tree_{digest[:16]}", digest, stored, utcnow()))
        self.assertEqual(self.store.load_tree(f"tree_{digest[:16]}"), entries)
        self.assertEqual(self.store.tree_references(f"tree_{digest[:16]}")[0], {f"tree_{digest[:16]}"})


class ReviewFixTests(TriHarness):
    files = {"README.md": "hello\n", ".gitignore": "*.log\nnode_modules/\n", "src/app.txt": "one\n", "config.json": "{\"a\": 1}\n", "config.example.json": "{\"b\": 2}\n"}

    def expire(self, lease: str) -> None:
        from tri.cli import Store

        store = Store()
        try:
            with store.conn:
                store.conn.execute("UPDATE materialization_leases SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00Z", lease))
        finally:
            store.close()

    # C1 -----------------------------------------------------------------
    def discard_unique(self, text: str) -> None:
        ws = self.workspace()
        self.tri("files", "write", ws, "unique.txt", "--text", text)
        self.tri("workspace", "discard", ws)

    def test_gc_mark_does_not_hold_the_write_lock(self) -> None:
        import sqlite3
        from tri.cli import Store, gc_apply

        self.discard_unique("purge me " * 40)
        store = Store()
        wrote: list[bool] = []

        def during_mark() -> None:
            other = sqlite3.connect(store.db_path, timeout=1)
            try:
                other.execute("INSERT INTO store_meta (key, value) VALUES ('probe', '1') ON CONFLICT(key) DO UPDATE SET value = '2'")
                other.commit()
                wrote.append(True)
            finally:
                other.close()

        try:
            result = gc_apply(store, retention_days=0, grace_seconds=0, _after_mark=during_mark)
        finally:
            store.close()
        self.assertEqual(wrote, [True])
        self.assertGreaterEqual(result["blobs"], 1)

    def test_blob_rereferenced_during_mark_survives(self) -> None:
        from tri.cli import Store, gc_apply

        text = "comes back during gc " * 40
        self.discard_unique(text)
        store = Store()
        writer = Store()
        try:
            def during_mark() -> None:
                writer.put_blob(text.encode())
                writer.conn.commit()

            gc_apply(store, retention_days=0, grace_seconds=0, _after_mark=during_mark)
            blob = writer.put_blob(text.encode())
            writer.conn.commit()
            self.assertEqual(writer.read_blob(blob["id"]), text.encode())
            self.assertTrue(Path(blob["object_path"]).exists())
            self.assertIsNone(store.row("SELECT purged_at FROM blobs WHERE id = ?", (blob["id"],))["purged_at"])
        finally:
            writer.close()
            store.close()

    # C2 -----------------------------------------------------------------
    @unittest.skipUnless(POSIX, "uses ln -s to make an escaping symlink")
    def test_failed_import_keeps_run_dir_for_salvage(self) -> None:
        ws = self.workspace()
        failed = self.tri("run", ws, "--", "sh", "-c", "echo work > a.txt && ln -s /usr/bin/true esc", ok=False)
        self.assertEqual(failed["diagnostics"][0]["code"], "path_escaped_mount")
        runs = Path(os.environ["TRI_HOME"]) / "runs"
        self.assertEqual(len(list(runs.iterdir())), 1, "run dir with unimported work must not be deleted")
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual(reaped["salvaged"][0]["paths"], ["a.txt"])
        self.assertEqual(list(runs.iterdir()), [])

    # I1 -----------------------------------------------------------------
    def test_mount_refuses_path_owned_by_live_lease(self) -> None:
        ws = self.workspace()
        path = self.root / "x"
        self.tri("workspace", "mount", ws, str(path))
        (path / "unsynced.txt").write_text("keep me\n")
        refused = self.tri("workspace", "mount", ws, str(path), ok=False)
        self.assertEqual(refused["diagnostics"][0]["code"], "mount_path_in_use")
        self.assertTrue((path / "unsynced.txt").exists())

    def test_remount_over_revoked_lease_salvages_first(self) -> None:
        ws = self.workspace()
        path = self.root / "x"
        old = self.tri("workspace", "mount", ws, str(path))["data"]["lease"]
        (path / "unsynced.txt").write_text("keep me\n")
        self.tri("workspace", "lease-revoke", old)
        self.tri("workspace", "mount", ws, str(path))
        self.assertEqual([s["paths"] for s in self.tri("salvage", "list", ws)["data"]["salvages"]], [["unsynced.txt"]])
        self.assertEqual(self.tri("leases", "inspect", old)["data"]["lease"]["status"], "reaped")

    def test_remount_never_force_deletes_secret_files(self) -> None:
        ws = self.workspace()
        path = self.root / "x"
        old = self.tri("workspace", "mount", ws, str(path))["data"]["lease"]
        (path / ".env.local").write_text("TOKEN=hand\n")
        self.tri("workspace", "lease-revoke", old)
        refused = self.tri("workspace", "mount", ws, str(path), ok=False)
        self.assertEqual(refused["diagnostics"][0]["code"], "mount_path_in_use")
        self.assertTrue((path / ".env.local").exists())

    def test_reap_skips_directory_replaced_by_user(self) -> None:
        from tri.materialize import remove_tree

        ws = self.workspace()
        path = self.root / "x"
        lease = self.tri("workspace", "mount", ws, str(path))["data"]["lease"]
        remove_tree(path)
        path.mkdir()
        (path / "mine.txt").write_text("not tri's\n")
        self.expire(lease)
        self.tri("leases", "reap")
        self.assertTrue((path / "mine.txt").exists())
        self.assertEqual(self.tri("leases", "inspect", lease)["data"]["lease"]["status"], "gone")

    # I2 -----------------------------------------------------------------
    def test_reaper_skips_lease_released_after_it_was_read(self) -> None:
        from tri.cli import RunLease, Store, reap_disk_lease

        ws = self.workspace()
        store = Store()
        try:
            lease = RunLease(store, kind="run", key="check_race", workspace_id=ws, before_snapshot=None)
            lease.work.mkdir()
            (lease.work / "f.txt").write_text("x")
            stale = store.row("SELECT * FROM disk_leases WHERE id = ?", (lease.id,))
            lease.release()
            self.assertIsNone(reap_disk_lease(store, stale, actor="actor_local"))
            self.assertEqual(store.row("SELECT status FROM disk_leases WHERE id = ?", (lease.id,))["status"], "released")
        finally:
            store.close()

    # I3 -----------------------------------------------------------------
    def test_reaper_lock_serializes_reapers(self) -> None:
        from tri.cli import Store, reap_leases
        from tri.locking import FileLock

        ws = self.workspace()
        mount = self.root / "m"
        lease = self.tri("workspace", "mount", ws, str(mount))["data"]["lease"]
        self.expire(lease)
        store = Store()
        held = FileLock(store.home / "reaper.lock")
        try:
            held.acquire(exclusive=True)
            busy = reap_leases(store, actor="actor_local")
            self.assertTrue(busy["busy"])
            self.assertTrue(mount.exists())
            held.release()
            done = reap_leases(store, actor="actor_local")
            self.assertEqual([r["lease"] for r in done["reaped"]], [lease])
        finally:
            held.close()
            store.close()

    # I4 -----------------------------------------------------------------
    @unittest.skipUnless(POSIX, "Windows st_ctime is creation time, so cp -p rewrites are not detectable there")
    def test_same_size_copy_preserving_mtime_is_imported(self) -> None:
        ws = self.workspace()
        check = self.run_cmd(ws, "cp -p config.example.json config.json")
        self.assertEqual(self.changed(check), ["config.json"])

    # I5 -----------------------------------------------------------------
    def test_ignored_hand_written_file_blocks_reap(self) -> None:
        ws = self.workspace()
        mount = self.root / "m"
        lease = self.tri("workspace", "mount", ws, str(mount))["data"]["lease"]
        (mount / "debug.log").write_text("hand-written notes\n")
        self.expire(lease)
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual([r["lease"] for r in reaped["needs_attention"]], [lease])
        attention = self.tri("leases", "inspect", lease)["data"]["lease"]["attention"]
        self.assertEqual(attention["ignored_paths"], ["debug.log"])
        self.assertTrue((mount / "debug.log").exists())


class GcMarkScalingTests(unittest.TestCase):
    def test_gc_marks_each_shared_node_once(self) -> None:
        import tri.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            store = cli.Store(Path(tmp) / "home")
            try:
                with store.conn:
                    store.conn.execute("INSERT INTO projects (id, name, root_path, created_at) VALUES ('proj_1', 'p', '/tmp', '2000-01-01T00:00:00Z')")
                entries = {}
                for d in range(10):
                    for f in range(10):
                        blob = store.put_blob(f"{d}-{f}".encode())
                        entries[f"d{d}/f{f}.txt"] = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": "100644"}
                for i in range(30):
                    blob = store.put_blob(f"edit {i}".encode())
                    entries["d0/f0.txt"] = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": "100644"}
                    with store.conn:
                        store.create_snapshot("proj_1", dict(entries), parents=[], source="imported", actor="actor_local")
                unique_nodes = store.row("SELECT COUNT(*) AS c FROM trees", ())["c"]
                calls = []
                original = cli.decode_node
                cli.decode_node = lambda text: calls.append(1) or original(text)
                try:
                    store._tree_cache.clear()
                    cli.gc_compute(store, retention_days=0, grace_seconds=0)
                finally:
                    cli.decode_node = original
                self.assertLessEqual(len(calls), unique_nodes)
            finally:
                store.close()


class CleanupGapTests(TriHarness):
    files = {"README.md": "hello\n", "deps.lock": "v1\n"}

    def home(self) -> Path:
        return Path(os.environ["TRI_HOME"])

    def test_orphan_run_and_staging_dirs_are_swept(self) -> None:
        from tri.locking import FileLock

        self.workspace()
        old = time.time() - 7200
        orphan = self.home() / "runs" / "check_orphan"
        (orphan / "work").mkdir(parents=True)
        (orphan / "work" / "x.txt").write_text("x")
        os.utime(orphan, (old, old))
        live = self.home() / "runs" / "check_live_no_row"
        live.mkdir(parents=True)
        holder = FileLock(live / ".holder.lock")
        holder.acquire(exclusive=True)
        os.utime(live, (old, old))
        staging = self.home() / "envs" / ".env_abc.1234.tmp"
        (staging / "deps").mkdir(parents=True)
        os.utime(staging, (old, old))
        fresh = self.home() / "runs" / "check_fresh"
        fresh.mkdir(parents=True)
        try:
            report = self.tri("leases", "reap")["data"]
            self.assertFalse(orphan.exists())
            self.assertFalse(staging.exists())
            self.assertTrue(live.exists(), "a directory whose holder is alive must stay")
            self.assertTrue(fresh.exists(), "a just-created directory may not have its lease row yet")
            self.assertEqual(sorted(Path(p).name for p in report["swept"]), [".env_abc.1234.tmp", "check_orphan"])
        finally:
            holder.close()

    def test_env_claims_are_conditional(self) -> None:
        from tri.cli import Store, claim_env_for_eviction, claim_env_for_use

        self.tri("env", "define", "app", "deps", "--key-file", "deps.lock", "--install", "sh -c 'mkdir -p deps && echo x > deps/x'", "--output", "deps")
        self.run_cmd(self.workspace(), "true")
        store = Store()
        try:
            row = store.row("SELECT * FROM env_builds", ())
            self.assertTrue(claim_env_for_use(store, row["key"]))
            stale = store.row("SELECT * FROM env_builds", ())
            with store.conn:
                store.conn.execute("UPDATE env_builds SET last_used_at = '2999-01-01T00:00:00Z' WHERE key = ?", (row["key"],))
            self.assertFalse(claim_env_for_eviction(store, stale), "a reuse after the eviction read must win")
            fresh = store.row("SELECT * FROM env_builds", ())
            self.assertTrue(claim_env_for_eviction(store, fresh))
            self.assertFalse(claim_env_for_use(store, row["key"]), "an env being evicted must not be handed out")
        finally:
            store.close()

    def test_gc_purge_also_removes_cached_copies(self) -> None:
        ws = self.workspace()
        text = "secret that leaked " * 40
        self.tri("files", "write", ws, "leak.txt", "--text", text)
        self.run_cmd(ws, "true")
        import hashlib

        digest = hashlib.sha256(text.encode()).hexdigest()
        from tri.cli import Store

        store = Store()
        try:  # make sure the cache holds it whatever backend this host uses
            blob_id = store.row("SELECT id FROM blobs WHERE hash = ?", (digest,))["id"]
            cached = store.materializer.cache.path_for(digest, blob_id, False)
        finally:
            store.close()
        self.assertTrue(cached.exists())
        self.tri("workspace", "discard", ws)
        self.tri("gc", "--apply", "--retention-days", "0", "--grace-seconds", "0", "--unsafe-no-grace")
        self.assertFalse(cached.exists())

    def test_gc_refuses_short_grace_without_opt_in(self) -> None:
        refused = self.tri("gc", "--apply", "--grace-seconds", "5", ok=False)
        self.assertEqual(refused["diagnostics"][0]["code"], "invalid_command")
        self.tri("gc", "--apply", "--grace-seconds", "5", "--unsafe-no-grace")

    def test_put_tree_batches_node_writes(self) -> None:
        from tri.cli import Store

        store = Store()
        try:
            entries = {}
            for i in range(20):
                blob = store.put_blob(f"{i}".encode())
                entries[f"d{i % 5}/f{i}.txt"] = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": "100644"}
            store.put_tree(entries)
            statements: list[str] = []
            store.conn.set_trace_callback(statements.append)
            tree = store.put_tree(dict(entries))
            store.conn.set_trace_callback(None)
            tree_statements = [s for s in statements if "trees" in s]
            self.assertLessEqual(len(tree_statements), 2, "an unchanged tree of 6 nodes needs one lookup and one bulk touch, not 2 statements per node")
            store.conn.commit()
            store._tree_cache.clear()
            self.assertEqual(store.load_tree(tree["id"]), {k: dict(v) for k, v in entries.items()})
        finally:
            store.close()

    def test_reap_errors_surface_in_doctor(self) -> None:
        from tri.cli import Store, maybe_auto_reap
        import tri.cli as cli

        os.environ["TRI_AUTO_REAP"] = "1"
        store = Store()
        original = cli.reap_leases
        try:
            cli.reap_leases = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            with store.conn:
                store.conn.execute("DELETE FROM store_meta")
            maybe_auto_reap(store)
        finally:
            cli.reap_leases = original
            store.close()
        doctor = self.tri("doctor")["data"]["materialization"]["leases"]
        self.assertIn("boom", doctor["last_reap_error"])


class ClearCachesTests(TriHarness):
    files = {"README.md": "hello\n", "deps.lock": "v1\n"}

    def test_clear_caches_removes_unused_caches_but_keeps_live_ones(self) -> None:
        home = Path(os.environ["TRI_HOME"])
        self.tri("env", "define", "app", "deps", "--key-file", "deps.lock", "--install", "sh -c 'mkdir -p deps && echo x > deps/x'", "--output", "deps", "--mode", "link")
        self.run_cmd(self.workspace(), "true")
        self.tri("files", "write", self.workspace(), "deps.lock", "--text", "v2\n")
        live_ws = self.workspace()
        self.tri("files", "write", live_ws, "deps.lock", "--text", "v3\n")
        mount = self.root / "m"
        self.tri("workspace", "mount", live_ws, str(mount))
        builds = {b["key"]: b for b in self.tri("env", "inspect", "app", "deps")["data"]["builds"]}
        self.assertEqual(len(builds), 2)
        result = self.tri("gc", "--clear-caches")["data"]
        evicted = {e["key"] for e in result["evicted"]["envs"]}
        self.assertEqual(len(evicted), 1, "the env linked into the live mount stays")
        self.assertTrue((mount / "deps" / "x").exists())
        self.assertEqual(self.tri("doctor")["data"]["materialization"]["caches"]["checkout_cache_bytes"], 0)
        import shutil

        self.tri("leases", "reap", self.tri("leases", "list", "--workspace", live_ws)["data"]["leases"][0]["id"], "--force")
        self.tri("gc", "--clear-caches")
        shutil.rmtree(home)  # a cleared store deletes with plain tools (no read-only leftovers)
        self.assertFalse(home.exists())


class ServeAndDoctorTests(TriHarness):
    def test_server_bind_skips_reverse_dns(self) -> None:
        import socket
        from tri.cli import TriAPIHandler, TriHTTPServer

        original = socket.getfqdn
        socket.getfqdn = lambda *a, **k: (_ for _ in ()).throw(AssertionError("reverse DNS lookup during bind"))
        try:
            server = TriHTTPServer(("127.0.0.1", 0), TriAPIHandler)
            server.server_close()
        finally:
            socket.getfqdn = original

    def test_doctor_degraded_warning_is_advertised(self) -> None:
        from tri.cli import SCHEMA_CATALOG

        os.environ["TRI_MATERIALIZE_BACKEND"] = "copy"
        warnings = self.tri("doctor")["warnings"]
        self.assertIn("materialization_degraded", [w["code"] for w in warnings])
        self.assertIn("materialization_degraded", SCHEMA_CATALOG["tri.diagnostic"]["codes"])


class StoreOpenConcurrencyTests(unittest.TestCase):
    def test_opening_a_packed_store_never_waits_for_long_lived_stores(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, TRI_HOME=home, TRI_AUTO_REAP="0")
            open_close = "from tri.cli import Store; Store().close()"
            subprocess.run([sys.executable, "-c", open_close], cwd=ROOT, env=env, check=True)
            self.assertTrue((Path(home) / "tri.sqlite3.tri").exists(), "fixture: database starts packed")
            # The holder unpacks slowly (patched here, in the test's subprocess), so the
            # openers below always arrive while the database is still packed; it then
            # stays open for a long time, like a long `tri run`.
            holder_code = (
                "import time; from tri.cli import Store\n"
                "original = Store.unpack_db\n"
                "def slow(self):\n    time.sleep(1.0)\n    original(self)\n"
                "Store.unpack_db = slow\n"
                "s = Store(); time.sleep(6); s.close()\n"
            )
            holder = subprocess.Popen([sys.executable, "-c", holder_code], cwd=ROOT, env=env)
            time.sleep(0.5)
            openers = [subprocess.Popen([sys.executable, "-c", open_close], cwd=ROOT, env=env) for _ in range(4)]
            started = time.time()
            for opener in openers:
                opener.wait(timeout=30)
            elapsed = time.time() - started
            holder.wait(timeout=30)
            self.assertLess(elapsed, 3.5, "an opener waited for another store's whole lifetime")


class CoarseTimestampTests(unittest.TestCase):
    def test_update_baseline_skips_racily_clean_entries(self) -> None:
        from tri.materialize import Materialization

        mat = Materialization(path=Path("/nonexistent"), backend="copy", degraded=True, baseline={"a": [1, 0, 1, 33188, 0]})
        now_ns = time.time_ns()
        mat.update_baseline({"a": [2, now_ns, 1, 33188, now_ns], "b": [3, now_ns - 10_000_000_000, 2, 33188, now_ns - 10_000_000_000]})
        self.assertEqual(mat.baseline["a"], [1, 0, 1, 33188, 0], "an entry written within the timestamp tick is not trusted")
        self.assertEqual(mat.baseline["b"][0], 3)


class WindowsFallbackTests(unittest.TestCase):
    def test_link_attachment_falls_back_to_copy_when_symlinks_fail(self) -> None:
        import tri.materialize as m

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "env" / "deps"
            src.mkdir(parents=True)
            (src / "x").write_text("x")
            blobs = FakeBlobs()
            cache = m.CheckoutCache(root / "home" / "checkout", blobs.read)
            mat = m.Materializer(root / "home", cache, read_blob=blobs.read)
            original = m.os.symlink

            def deny(*args: object, **kwargs: object) -> None:
                raise OSError(1314, "A required privilege is not held by the client")

            m.os.symlink = deny
            try:
                result = mat.materialize({"a.txt": blobs.entry(b"a")}, root / "w", attachments=[m.Attachment("deps", src, "link")], backend="copy")
            finally:
                m.os.symlink = original
            self.assertEqual((root / "w" / "deps" / "x").read_text(), "x")
            self.assertIn("attachment_link_failed", [n["code"] for n in result.notes])


def overlay_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    from tri.overlay import probe_overlay

    with tempfile.TemporaryDirectory() as tmp:
        return probe_overlay(Path(tmp)) is not None


@unittest.skipUnless(overlay_available(), "needs Linux user+mount namespaces with overlayfs")
class OverlayTests(TriHarness):
    files = {"README.md": "hello\n", ".gitignore": "*.log\n", "src/app.txt": "one\n", "src/gone.txt": "bye\n", "deps.lock": "v1\n"}

    def setUp(self) -> None:
        super().setUp()
        os.environ["TRI_MATERIALIZE_BACKEND"] = "overlay"

    def test_overlay_run_sees_head_and_imports_edits_and_deletes(self) -> None:
        ws = self.workspace()
        self.tri("files", "write", ws, "src/app.txt", "--text", "head edit\n")
        self.tri("files", "delete", ws, "src/gone.txt")
        check = self.run_cmd(ws, "grep -q 'head edit' src/app.txt && test ! -e src/gone.txt && echo n > new.txt && rm README.md && echo l > x.log && echo e >> src/app.txt")
        self.assertEqual(check["status"], "passed", check)
        self.assertEqual(check["materialized_directory"]["backend"], "overlay")
        self.assertEqual(self.changed(check), ["README.md", "new.txt", "src/app.txt"])
        text = self.tri("files", "read", ws, "src/app.txt")["data"]["text"]
        self.assertEqual(text, "head edit\ne\n")

    def test_overlay_command_runs_as_caller_when_possible(self) -> None:
        ws = self.workspace()
        check = self.run_cmd(ws, f"test \"$(id -u)\" = {os.getuid()}")
        overlay = check["materialized_directory"]["overlay"]
        if overlay["mode"] == "userns" and not overlay["drop_root"]:
            self.skipTest("util-linux on this host cannot map the caller's uid in a nested namespace")
        self.assertEqual(check["status"], "passed", "commands must not run as root over the overlay")

    def test_overlay_upper_holds_only_changes(self) -> None:
        from tri.cli import Store
        from tri.overlay import prepare_overlay_run, probe_overlay

        store = Store()
        try:
            snapshot = self.tri("workspace", "inspect", self.workspace())["data"]["head_snapshot"]
            row = store.row("SELECT root_tree_id FROM snapshots WHERE id = ?", (snapshot,))
            entries = store.load_tree(row["root_tree_id"])
            run_root = store.runs / "manual"
            mat = prepare_overlay_run(run_root=run_root, bases=store.home / "bases", cache=store.materializer.cache, read_blob=store.read_blob, entries=entries, base=(row["root_tree_id"], entries), attachments=[], info=probe_overlay(store.runs))
            import subprocess

            proc = subprocess.run(mat.wrap(["sh", "-c", "echo z > src/app.txt && cat README.md"]), cwd=run_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, b"hello\n")
            upper_files = sorted(str(p.relative_to(mat.upper)) for p in mat.upper.rglob("*") if p.is_file())
            self.assertEqual(upper_files, ["src/app.txt"])
            self.assertEqual(mat.scan(entries), (["src/app.txt"], []))
            mat.release()
        finally:
            store.close()

    def test_overlay_env_layer(self) -> None:
        self.tri("env", "define", "app", "deps", "--key-file", "deps.lock", "--install", "sh -c 'mkdir -p deps && cat deps.lock > deps/lib.txt'", "--output", "deps")
        ws = self.workspace()
        check = self.run_cmd(ws, "grep v1 deps/lib.txt && echo changed > deps/lib.txt")
        self.assertEqual(check["status"], "passed")
        self.assertEqual(self.changed(check), [])
        build = self.tri("env", "inspect", "app", "deps")["data"]["builds"][0]
        self.assertEqual((Path(build["path"]) / "deps" / "lib.txt").read_text(), "v1\n")

    def test_overlay_crash_is_salvaged(self) -> None:
        import subprocess

        ws = self.workspace()
        crashed = subprocess.run(
            [sys.executable, "-m", "tri", "run", ws, "--", "sh", "-c", "echo rescued > new.txt && kill -9 $PPID"],
            cwd=ROOT, env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(crashed.returncode, 0)
        reaped = self.tri("leases", "reap")["data"]
        self.assertEqual(reaped["salvaged"][0]["paths"], ["new.txt"])


if __name__ == "__main__":
    unittest.main()
