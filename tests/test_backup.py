"""Backups are complete Tri homes: restore by pointing TRI_HOME at one."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-backup-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.txt").write_text("v1\n")
        self.home = self.root / "home"
        self.tri("import", str(repo), "--name", "app", "--target", "main")
        self.ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("files", "write", self.ws, "src/app.txt", "--text", "v2\n")
        self.check = self.tri("run", self.ws, "--", sys.executable, "-c", "print('logged output')")["data"]["id"]

    def tri(self, *args: str, home: Path | None = None, ok: bool = True) -> dict:
        env = {**os.environ, "TRI_HOME": str(home or self.home), "TRI_AUTO_REAP": "0"}
        for key in ("TRI_REMOTE", "TRI_ACTOR"):
            env.pop(key, None)
        result = subprocess.run([sys.executable, "-m", "tri", *args], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = json.loads(result.stdout)
        self.assertEqual(output["ok"], ok, output)
        return output

    def test_backup_restores_by_pointing_tri_home_at_it(self) -> None:
        backup = self.root / "backup"
        made = self.tri("backup", "create", str(backup))["data"]
        self.assertGreater(made["copied"]["objects"]["files"], 0)
        self.assertTrue(self.tri("backup", "verify", str(backup), "--deep")["data"]["ok"])
        # Move the original away so nothing can be read from it.
        self.home.rename(self.root / "gone")
        self.assertEqual(self.tri("files", "read", self.ws, "src/app.txt", home=backup)["data"]["text"], "v2\n")
        log = self.tri("checks", "stream", self.check, "--log", "stdout", home=backup)["data"]
        self.assertIn("logged output", json.dumps(log))

    def test_repeat_backups_copy_only_new_objects(self) -> None:
        backup = self.root / "backup"
        self.tri("backup", "create", str(backup))
        again = self.tri("backup", "create", str(backup))["data"]
        self.assertEqual(again["copied"]["objects"]["files"], 0)
        self.tri("files", "write", self.ws, "src/new.txt", "--text", "new content\n")
        third = self.tri("backup", "create", str(backup))["data"]
        self.assertGreaterEqual(third["copied"]["objects"]["files"], 1)
        self.assertEqual(self.tri("files", "read", self.ws, "src/new.txt", home=backup)["data"]["text"], "new content\n")

    def test_verify_reports_missing_objects(self) -> None:
        backup = self.root / "backup"
        self.tri("backup", "create", str(backup))
        victim = next(path for path in (backup / "objects").rglob("*") if path.is_file())
        victim.unlink()
        broken = self.tri("backup", "verify", str(backup), ok=False)
        self.assertEqual(broken["diagnostics"][0]["code"], "backup_incomplete")
        self.assertEqual(broken["data"]["problems"][0]["kind"], "missing_blob")

    def test_only_maintainers_back_up_and_never_into_the_store(self) -> None:
        denied = self.tri("--actor", "agent:a", "backup", "create", str(self.root / "b"), ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        inside = self.tri("backup", "create", str(self.home / "nested"), ok=False)
        self.assertEqual(inside["diagnostics"][0]["code"], "invalid_command")


if __name__ == "__main__":
    unittest.main()
