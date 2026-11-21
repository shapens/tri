"""CLI conformance: argv in, one JSON envelope out, exit status 0 exactly when ok.

The CLI under test is TRI_CONFORMANCE_BIN (default: python -m tri)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .harness import Contracts, base_env, make_repo, run_cli

AGENT = "agent:conformance"


class CLIConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts = Contracts()
        cls.tmp = Path(tempfile.mkdtemp(prefix="tri-conformance-cli-"))
        cls.env = base_env(cls.tmp / "home")
        cls.repo = make_repo(cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tri(self, *argv: str, ok: bool = True, stdin: bytes | None = None) -> dict:
        status, output, stderr = run_cli(list(argv), self.env, cwd=self.tmp, stdin=stdin)
        self.assertIsNotNone(output, f"stdout is not one JSON envelope; stderr: {stderr[-600:]}")
        summary = "--response" in argv and argv[argv.index("--response") + 1] == "summary"
        errors = self.contracts.envelope_errors(output, summary=summary, expect_schema=self.contracts.command_schema(list(argv)))
        self.assertEqual(errors, [], str(output)[:600])
        self.assertEqual(output["ok"], ok, str(output)[:600])
        self.assertEqual(status == 0, output["ok"], f"exit status {status} for ok={output['ok']}")
        return output

    def test_discovery_matches_the_contract_files(self) -> None:
        commands = self.tri("commands")
        self.assertEqual([item["name"] for item in commands["data"]["commands"]], list(self.contracts.by_name))
        for item in commands["data"]["commands"]:
            self.assertEqual(item["usage"], self.contracts.by_name[item["name"]]["usage"])
        schemas = self.tri("schema")
        self.assertTrue(all(self.contracts.has_schema(name) for name in schemas["data"]["schemas"]))
        self.tri("schema", "tri.files.read")
        self.tri("doctor")

    def test_agent_loop(self) -> None:
        self.tri("import", str(self.repo), "--name", "app")
        self.tri("projects", "list")
        self.tri("targets", "list", "app")
        ws = self.tri("workspace", "create", "app", "--from", "main", "--actor", AGENT)["data"]["id"]
        self.tri("files", "write", ws, "src/new.py", "--text", "NEW = 1\n", "--actor", AGENT)
        self.assertEqual(self.tri("files", "read", ws, "src/new.py", "--actor", AGENT)["data"]["text"], "NEW = 1\n")
        self.tri("files", "list", ws, "--actor", AGENT)
        self.tri("files", "search", ws, "conformance", "--actor", AGENT)
        self.tri("diff", ws, "--actor", AGENT)
        cs = self.tri("changeset", "create", ws, "--title", "cli conformance", "--actor", AGENT)["data"]["id"]
        self.tri("changeset", "inspect", cs, "--actor", AGENT)
        self.tri("--response", "summary", "workspace", "list", "--actor", AGENT)
        plan = b'{"commands": [{"argv": ["workspace", "inspect", "' + ws.encode() + b'"], "as": "w"}], "return": {"id": "${w.data.id}"}}'
        batch = self.tri("batch", "-", "--actor", AGENT, stdin=plan)
        self.assertEqual(batch["data"]["returns"]["id"], ws)

    def test_errors(self) -> None:
        for argv, code in ((["workspace", "inspect", "ws_doesnotexist0"], "path_not_found"), (["files"], "invalid_command"), (["nope"], "invalid_command")):
            with self.subTest(argv=argv):
                output = self.tri(*argv, ok=False)
                self.assertEqual(output["schema"], "tri.error")
                self.assertIn(code, [item["code"] for item in output["diagnostics"]])


if __name__ == "__main__":
    unittest.main()
