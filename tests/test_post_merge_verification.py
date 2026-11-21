"""Post-merge verification: merge before checks, verify after, revert on failure."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402

# The project's test: every file under src/ must not contain the word BROKEN.
CHECK = f"{sys.executable.replace(chr(92), '/')} -c \"import pathlib,sys; sys.exit(any('BROKEN' in p.read_text() for p in pathlib.Path('src').rglob('*.txt')))\""


class PostMergeVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-verify-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "a.txt").write_bytes(("a\n").encode())
        (repo / "src" / "b.txt").write_bytes(("b\n").encode())
        (repo / "src" / "shared.txt").write_bytes(("1\n2\n3\n4\n5\n6\n7\n").encode())
        (repo / "ops").mkdir()
        (repo / "ops" / "deploy.txt").write_bytes(("deploy\n").encode())
        patcher = mock.patch.dict(os.environ, {"TRI_HOME": str(self.root / "tri-home"), "TRI_AUTO_REAP": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        self.tri("import", str(repo), "--name", "app", "--target", "main")
        self.tri("checks", "set-create", "app", "unit", "--required-check", CHECK)
        self.tri("merge-rules", "add", "app", "fast-src", "--path", "src/*", "--verify-after", "unit")

    def tri(self, *argv: str, ok: bool = True) -> dict:
        output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        for diagnostic in output["diagnostics"]:
            self.assertIn(diagnostic["code"], SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        schema = SCHEMA_CATALOG.get(output["schema"])
        if schema and output["ok"]:
            missing = [key for key in schema.get("required", []) if key not in output["data"]]
            self.assertEqual(missing, [], f"{output['schema']} missing {missing}")
        return output

    def target_file(self, path: str) -> str | None:
        snapshot = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]
        output, _ = cli.execute_argv(["files", "read", snapshot, path])
        return output["data"].get("text") if output["ok"] else None

    def merge_unchecked(self, path: str, text: str, *, actor: str = "agent:a", ok: bool = True) -> dict:
        ws = self.tri("--actor", actor, "workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("--actor", actor, "files", "write", ws, path, "--text", text)
        cs = self.tri("--actor", actor, "changeset", "create", ws, "--title", f"Edit {path}")["data"]["id"]
        merged = self.tri("--actor", actor, "changeset", "merge", cs, "--target", "main", ok=ok)
        merged["changeset"] = cs
        return merged

    def test_changeset_merges_before_checks_and_is_verified_after(self) -> None:
        merged = self.merge_unchecked("src/a.txt", "a2\n")
        self.assertEqual(merged["data"]["status"], "merged")
        verification = merged["data"]["verification"]
        self.assertIsNotNone(verification)
        self.assertEqual(self.target_file("src/a.txt"), "a2\n")
        self.assertEqual(self.tri("verify", "list", "app", "--status", "pending")["data"]["verifications"][0]["id"], verification)
        run = self.tri("verify", "run", "app")["data"]
        self.assertEqual([item["status"] for item in run["processed"]], ["verified"])
        self.assertEqual(run["pending"], 0)
        self.assertEqual(self.target_file("src/a.txt"), "a2\n")
        self.assertEqual(self.tri("changeset", "inspect", merged["changeset"])["data"]["status"], "merged")

    def test_failed_verification_reverts_only_that_change_and_keeps_later_merges(self) -> None:
        first = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main")["data"]["id"]
        second = self.tri("--actor", "agent:b", "workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", first, "src/shared.txt", "--text", "BROKEN\n2\n3\n4\n5\n6\n7\n")
        self.tri("--actor", "agent:b", "files", "write", second, "src/shared.txt", "--text", "1\n2\n3\n4\n5\n6\nseven\n")
        broken = {"changeset": self.tri("--actor", "agent:a", "changeset", "create", first, "--title", "break")["data"]["id"]}
        later = {"changeset": self.tri("--actor", "agent:b", "changeset", "create", second, "--title", "seven")["data"]["id"]}
        broken["data"] = self.tri("--actor", "agent:a", "changeset", "merge", broken["changeset"], "--target", "main")["data"]
        self.assertEqual(self.tri("--actor", "agent:b", "changeset", "merge", later["changeset"], "--target", "main")["data"]["text_merged_paths"], ["src/shared.txt"])
        self.assertEqual(self.target_file("src/shared.txt"), "BROKEN\n2\n3\n4\n5\n6\nseven\n")
        run = self.tri("verify", "run", "app")["data"]
        statuses = {item["changeset"]: item["status"] for item in run["processed"]}
        self.assertEqual(statuses[broken["changeset"]], "reverted")
        # The later change is tested without the reverted one, so it is not blamed.
        self.assertEqual(statuses[later["changeset"]], "verified")
        self.assertEqual(self.target_file("src/shared.txt"), "1\n2\n3\n4\n5\n6\nseven\n")
        self.assertEqual(self.tri("changeset", "inspect", broken["changeset"])["data"]["status"], "reverted")
        reverted = self.tri("verify", "inspect", broken["data"]["verification"])["data"]
        self.assertIsNotNone(reverted["revert_snapshot"])
        self.assertEqual(reverted["checks"][0]["status"], "failed")

    def test_revert_conflict_leaves_the_target_alone(self) -> None:
        broken = self.merge_unchecked("src/b.txt", "BROKEN\n")
        self.merge_unchecked("src/b.txt", "BROKEN and more\n", actor="agent:b")
        before = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]
        run = self.tri("verify", "run", "app", "--limit", "1")["data"]
        self.assertEqual(run["processed"][0]["changeset"], broken["changeset"])
        self.assertEqual(run["processed"][0]["status"], "revert_conflict")
        self.assertEqual(run["processed"][0]["result"]["conflicts"][0]["path"], "src/b.txt")
        self.assertEqual(self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"], before)

    def test_paths_outside_the_rule_still_need_checks_and_review(self) -> None:
        blocked = self.merge_unchecked("ops/deploy.txt", "changed\n", ok=False)
        codes = {diagnostic["code"] for diagnostic in blocked["diagnostics"]}
        self.assertEqual(codes, {"missing_required_check", "missing_review_approval"})

    def test_a_failing_pre_merge_check_is_not_deferred(self) -> None:
        ws = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", ws, "src/a.txt", "--text", "BROKEN\n")
        self.tri("--actor", "agent:a", "run", ws, "--", sys.executable, "-c", "raise SystemExit(1)")
        cs = self.tri("--actor", "agent:a", "changeset", "create", ws, "--title", "known bad")["data"]["id"]
        blocked = self.tri("--actor", "agent:a", "changeset", "merge", cs, "--target", "main", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "missing_required_check")
        self.assertEqual(blocked["diagnostics"][0]["reason"], "failing")

    def test_exports_still_require_passing_checks(self) -> None:
        merged = self.merge_unchecked("src/a.txt", "a3\n")
        export = self.tri("export", "git-patch", merged["changeset"], ok=False)
        self.assertIn("missing_required_check", {diagnostic["code"] for diagnostic in export["diagnostics"]})

    def test_only_maintainers_or_services_run_verification(self) -> None:
        self.merge_unchecked("src/a.txt", "a4\n")
        denied = self.tri("--actor", "agent:a", "verify", "run", "app", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(len(self.tri("--actor", "service:ci", "verify", "run", "app")["data"]["processed"]), 1)

    def test_verifications_of_private_changesets_stay_private(self) -> None:
        merged = self.merge_unchecked("src/a.txt", "a5\n")
        listed = self.tri("--actor", "agent:outsider", "verify", "list", "app")["data"]
        self.assertEqual(listed["verifications"], [])
        self.assertEqual(listed["redacted"], 1)
        denied = self.tri("--actor", "agent:outsider", "verify", "inspect", merged["data"]["verification"], ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")

    def test_mirror_titles_the_revert_commit(self) -> None:
        steps_kind = cli.MIRROR_TARGET_EVENTS
        self.assertIn("changeset.reverted", steps_kind)


if __name__ == "__main__":
    unittest.main()
