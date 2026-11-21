"""Partial checkouts, deferred cleanup, and incremental base directories for large trees."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli  # noqa: E402
from tri.materialize import clone_dir_supported  # noqa: E402

COUNT_FILES = (
    "import os\n"
    "n = sum(len(files) for _, _, files in os.walk('.'))\n"
    "open('count.txt', 'w').write(str(n))\n"
)


class PartialCheckoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-partial-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        for package in ("a", "b", "c"):
            (repo / package).mkdir(parents=True)
            for index in range(5):
                (repo / package / f"f{index}.txt").write_bytes((f"{package}{index}\n").encode())
        (repo / "README.md").write_bytes(("root\n").encode())
        env = {"TRI_HOME": str(self.root / "tri-home"), "TRI_AUTO_REAP": "0"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        self.tri("import", str(repo), "--name", "app", "--target", "main")
        self.tri("merge-rules", "add", "app", "all", "--path", "*")

    def tri(self, *argv: str, ok: bool = True) -> dict:
        output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        return output

    def workspace(self) -> str:
        return self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]

    def read(self, ref: str, path: str) -> str | None:
        output, _ = cli.execute_argv(["files", "read", ref, path])
        return output["data"].get("text") if output["ok"] else None

    def test_partial_run_lays_out_only_matching_files_and_keeps_the_rest(self) -> None:
        ws = self.workspace()
        script = COUNT_FILES + "open('b/new.txt', 'w').write('new')\nos.makedirs('c', exist_ok=True)\nopen('c/made.txt', 'w').write('made')\n"
        check = self.tri("run", ws, "--paths", "b", "--", sys.executable, "-c", script)["data"]
        self.assertEqual(check["status"], "passed")
        self.assertEqual(self.read(ws, "count.txt"), "5")  # only b/* was laid out
        self.assertEqual(self.read(ws, "b/new.txt"), "new")
        self.assertEqual(self.read(ws, "c/made.txt"), "made")  # writes outside the partial set still land
        for path in ("a/f0.txt", "c/f0.txt", "README.md", "b/f0.txt"):
            self.assertIsNotNone(self.read(ws, path), path)  # nothing outside the set reads as deleted
        record = self.tri("checks", "inspect", check["id"])["data"]["materialized_directory"]
        self.assertEqual(record["sparse_paths"], ["b"])
        self.assertEqual(record["materialized_files"], 5)
        self.assertEqual(record["total_files"], 16)

    def test_partial_run_imports_deletions_inside_the_set(self) -> None:
        ws = self.workspace()
        self.tri("run", ws, "--paths", "b", "--", sys.executable, "-c", "import os\nos.remove('b/f1.txt')\n")
        self.assertIsNone(self.read(ws, "b/f1.txt"))
        self.assertIsNotNone(self.read(ws, "a/f1.txt"))

    def test_partial_mount_syncs_without_dropping_unmounted_files(self) -> None:
        ws = self.workspace()
        mount = self.root / "mnt"
        lease = self.tri("workspace", "mount", ws, str(mount), "--paths", "a/f0.txt", "--paths", "c")["data"]
        self.assertEqual(lease["materialization"]["sparse_paths"], ["a/f0.txt", "c"])
        self.assertTrue((mount / "a" / "f0.txt").exists())
        self.assertFalse((mount / "a" / "f1.txt").exists())
        self.assertFalse((mount / "b").exists())
        (mount / "c" / "f0.txt").write_bytes(("edited\n").encode())
        (mount / "c" / "f1.txt").unlink()
        self.tri("workspace", "sync", ws)
        self.assertEqual(self.read(ws, "c/f0.txt"), "edited\n")
        self.assertIsNone(self.read(ws, "c/f1.txt"))
        self.assertEqual(self.read(ws, "b/f3.txt"), "b3\n")
        self.assertEqual(self.read(ws, "a/f4.txt"), "a4\n")

    def test_run_directories_are_removed_without_blocking_the_command(self) -> None:
        ws = self.workspace()
        check = self.tri("run", ws, "--", sys.executable, "-c", "pass")["data"]
        runs = Path(os.environ["TRI_HOME"]) / "runs"
        self.assertFalse((runs / check["id"]).exists())
        trash = Path(os.environ["TRI_HOME"]) / "trash"
        deadline = time.time() + 20
        while trash.exists() and any(trash.iterdir()) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(trash.exists() and any(trash.iterdir()), "background removal left trash behind")

    def test_inline_cleanup_when_deferral_is_disabled(self) -> None:
        ws = self.workspace()
        with mock.patch.dict(os.environ, {"TRI_DEFER_CLEANUP": "0"}):
            self.tri("run", ws, "--", sys.executable, "-c", "pass")
        trash = Path(os.environ["TRI_HOME"]) / "trash"
        self.assertFalse(trash.exists() and any(trash.iterdir()))

    def test_gc_empties_stale_trash(self) -> None:
        trash = Path(os.environ["TRI_HOME"]) / "trash"
        stale = trash / "1-old-run-deadbeef" / "work"
        stale.mkdir(parents=True)
        (stale / "file.txt").write_bytes(("x").encode())
        fresh = trash / f"{int(time.time())}-fresh-run-cafebabe"
        fresh.mkdir()
        data = self.tri("gc", "--reap")["data"]
        self.assertEqual(data["trash_removed"], 1)
        self.assertFalse((trash / "1-old-run-deadbeef").exists())
        self.assertTrue(fresh.exists())

    @unittest.skipUnless(clone_dir_supported(), "base directories are used where whole-directory clones exist")
    def test_new_target_base_is_derived_from_the_previous_base(self) -> None:
        first = self.workspace()
        self.tri("run", first, "--", sys.executable, "-c", "pass")
        bases = Path(os.environ["TRI_HOME"]) / "bases"
        self.assertEqual(len([p for p in bases.iterdir() if not p.name.startswith(".")]), 1)
        self.tri("files", "write", first, "a/f0.txt", "--text", "changed\n")
        self.tri("run", first, "--", sys.executable, "-c", "pass")
        changeset = self.tri("changeset", "create", first, "--title", "edit")["data"]["id"]
        self.tri("changeset", "merge", changeset, "--target", "main")
        second = self.workspace()
        with mock.patch.object(cli.Materializer, "_place", wraps=cli.Materializer._place, autospec=True) as placed:
            self.tri("run", second, "--", sys.executable, "-c", "print(open('a/f0.txt').read())")
        # Derived from the old base: only the changed file is placed, not all 16.
        self.assertLessEqual(placed.call_count, 2)
        self.assertEqual(len([p for p in bases.iterdir() if not p.name.startswith(".")]), 2)


if __name__ == "__main__":
    unittest.main()
