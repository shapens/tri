from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import argparse
import ast
from pathlib import Path
from urllib import request
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri.cli import SCHEMA_CATALOG, SCHEMA_VERSION, Store, TriError, cmd_conflicts_resolve, cmd_workspace_snapshot, cmd_workspace_update, utcnow


class TriCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-test-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "tri@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Tri Test"], cwd=self.repo, check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.txt").write_text("one\n")
        (self.repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.env = os.environ.copy()
        self.env["TRI_HOME"] = str(self.root / "tri-home")

    def tearDown(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            try:
                path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.tmp.cleanup()

    def tri(self, *args: str, ok: bool = True) -> dict:
        result = subprocess.run(
            ["python3", "-m", "tri", *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if ok and result.returncode != 0:
            self.fail(f"tri {' '.join(args)} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
        if not ok and result.returncode == 0:
            self.fail(f"tri {' '.join(args)} unexpectedly passed\nSTDOUT:\n{result.stdout}")
        response = json.loads(result.stdout)
        self.assert_response_contract(response)
        return response

    def import_project(self) -> dict:
        return self.tri("import", str(self.repo), "--name", "app")

    def assert_schema_required_keys(self, schema_name: str, data: dict) -> None:
        schema = self.tri("schema", schema_name)["data"]["schema"]
        self.assert_schema_data_contract(schema_name, schema, data)

    def assert_schema_data_contract(self, schema_name: str, schema: dict, data: object) -> None:
        self.assertIsInstance(data, dict, f"{schema_name} data must be an object")
        if not isinstance(data, dict):
            return
        if "required" in schema:
            missing = [key for key in schema["required"] if key not in data]
            self.assertEqual(missing, [], f"{schema_name} missing required keys: {missing}")
        if "required_any" in schema:
            variants = schema["required_any"]
            self.assertTrue(
                any(all(key in data for key in variant) for variant in variants),
                f"{schema_name} did not match any required_any variant: {variants}",
            )

    def assert_response_contract(self, response: dict) -> None:
        for key in SCHEMA_CATALOG["tri.response"]["required"]:
            self.assertIn(key, response)
        self.assertEqual(response["schema_version"], SCHEMA_VERSION)
        self.assertIsInstance(response["events"], list)
        self.assertIsInstance(response["diagnostics"], list)
        self.assertIsInstance(response["warnings"], list)
        self.assertIsInstance(response["refs"], dict)
        self.assertIsInstance(response["links"], dict)
        visible_refs = {object_id for refs in response["refs"].values() for object_id in refs}
        for object_id, link in response["links"].items():
            self.assertIn(object_id, visible_refs)
            self.assertEqual(link["object"], object_id)
            self.assertEqual(link["inspect_argv"], ["objects", "inspect", object_id])
            self.assertEqual(link["inspect_command"], ["tri", "objects", "inspect", object_id])
            self.assertEqual(link["inspect_route"], f"/v0/objects/{object_id}")
        advertised_codes = set(SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        for diagnostic in [*response["diagnostics"], *response["warnings"]]:
            self.assert_schema_data_contract("tri.diagnostic", SCHEMA_CATALOG["tri.diagnostic"], diagnostic)
            self.assertIn(diagnostic["code"], advertised_codes)
        schema = SCHEMA_CATALOG.get(response["schema"])
        if schema is not None and (response["ok"] or response["data"]):
            self.assert_schema_data_contract(response["schema"], schema, response["data"])

    def test_batch_runs_dependent_agent_commands(self) -> None:
        plan = self.root / "batch.json"
        plan.write_text(
            json.dumps(
                {
                    "commands": [
                        {"as": "import", "response": "summary", "argv": ["import", str(self.repo), "--name", "app"]},
                        {
                            "as": "workspace",
                            "actor": "agent:batch",
                            "response": "summary",
                            "argv": ["workspace", "create", "app", "--from", "${import.data.target.name}", "--task", "batch edit"],
                        },
                        {
                            "as": "write",
                            "actor": "agent:batch",
                            "response": "summary",
                            "argv": ["files", "write", "${workspace.data.id}", "src/batch.txt", "--text", "batch edit\n"],
                        },
                        {
                            "as": "changeset",
                            "actor": "agent:batch",
                            "response": "summary",
                            "argv": ["changeset", "create", "${workspace.data.id}", "--title", "Batch edit"],
                        },
                    ]
                }
            )
        )
        batch = self.tri("batch", str(plan))
        self.assertTrue(batch["ok"])
        self.assertEqual(batch["data"]["executed"], 4)
        results = {item["as"]: item for item in batch["data"]["results"]}
        self.assertEqual(results["changeset"]["schema"], "tri.changeset.inspect")
        self.assertTrue(results["changeset"]["data"]["id"].startswith("cs_"))

    def test_packed_store_reopens_between_commands(self) -> None:
        imported = self.import_project()
        home = Path(self.env["TRI_HOME"])
        packed = home / "tri.sqlite3.tri"
        raw = home / "tri.sqlite3"
        self.assertTrue(packed.exists())
        self.assertFalse(raw.exists())
        listed = self.tri("projects", "list")
        self.assertEqual(listed["data"]["projects"][0]["id"], imported["data"]["project"]["id"])
        self.assertTrue(packed.exists())
        self.assertFalse(raw.exists())

    def open_store(self) -> Store:
        previous = os.environ.get("TRI_HOME")
        os.environ["TRI_HOME"] = str(self.root / "tri-home")
        try:
            return Store()
        finally:
            if previous is None:
                os.environ.pop("TRI_HOME", None)
            else:
                os.environ["TRI_HOME"] = previous

    def install_head_race(self, store: Store, workspace: str) -> None:
        original_create_snapshot = store.create_snapshot
        raced = {"done": False}

        def racing_create_snapshot(*args, **kwargs):
            snapshot = original_create_snapshot(*args, **kwargs)
            if not raced["done"]:
                raced["done"] = True
                ws = store.get_workspace(workspace)
                entries = store.load_snapshot_tree(ws["head_snapshot_id"])
                concurrent = original_create_snapshot(
                    ws["project_id"],
                    entries,
                    parents=[ws["head_snapshot_id"]],
                    source="workspace",
                    actor="agent:race",
                    metadata={"workspace": workspace, "operation": "race"},
                )
                store.conn.execute(
                    "UPDATE workspaces SET head_snapshot_id = ?, updated_at = ? WHERE id = ?",
                    (concurrent["id"], utcnow(), workspace),
                )
            return snapshot

        store.create_snapshot = racing_create_snapshot

    def free_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def api_get(self, port: int, path: str, *, actor: str | None = None) -> dict:
        headers = {"X-Tri-Actor": actor} if actor else {}
        req = request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        with request.urlopen(req, timeout=2) as response:
            output = json.loads(response.read().decode())
            self.assert_response_contract(output)
            return output

    def api_post(self, port: int, path: str, payload: dict) -> dict:
        req = request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            output = json.loads(response.read().decode())
            self.assert_response_contract(output)
            return output

    def start_server(self) -> tuple[subprocess.Popen[str], int]:
        port = self.free_port()
        proc = subprocess.Popen(
            ["python3", "-m", "tri", "serve", "--host", "127.0.0.1", "--port", str(port), "--quiet"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                self.fail(f"tri serve exited early\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            try:
                health = self.api_get(port, "/v0/health")
                if health["ok"]:
                    return proc, port
            except (ConnectionError, TimeoutError, URLError):
                time.sleep(0.05)
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        self.fail(f"tri serve did not become ready\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")

    def test_command_contracts_have_discoverable_schemas(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(pyproject["project"]["scripts"]["tri"], "tri.cli:main")

        commands_response = self.tri("commands")
        schema_response = self.tri("schema")
        self.assertEqual(commands_response["data"]["schema_version"], commands_response["schema_version"])
        self.assertEqual(schema_response["data"]["schema_version"], schema_response["schema_version"])
        commands = commands_response["data"]["commands"]
        schemas = schema_response["data"]["schemas"]
        command_names = {command["name"] for command in commands}
        self.assertTrue(all(command["schema_version"] == commands_response["schema_version"] for command in commands))
        self.assertTrue(all(schema["schema_version"] == schema_response["schema_version"] for schema in schemas.values()))
        self.assertEqual({name: schema["name"] for name, schema in schemas.items()}, {name: name for name in schemas})
        self.assertIn("links", schemas["tri.response"]["required"])
        self.assertIn("actors-inspect", command_names)
        self.assertIn("selectors", schemas["tri.actor.inspect"]["required"])
        self.assertIn("capabilities", schemas["tri.actor.inspect"]["required"])
        self.assertIn("packages-declare", command_names)
        self.assertIn("packages-list", command_names)
        self.assertIn("packages-inspect", command_names)
        self.assertIn("path_globs", schemas["tri.package.inspect"]["required"])
        self.assertIn("packages", schemas["tri.package.list"]["required"])
        self.assertIn("task-run-inspect", command_names)
        self.assertIn("leases-inspect", command_names)
        self.assertIn("review-action-inspect", command_names)
        self.assertIn("policy-grant-inspect", command_names)
        self.assertIn("objects-inspect", command_names)
        self.assertIn("root_path", schemas["tri.project.inspect"]["required"])
        self.assertIn("resolved_schema", schemas["tri.object.inspect"]["required"])
        self.assertIn("default_target", schemas["tri.project.inspect"]["required"])
        self.assertIn("counts", schemas["tri.project.inspect"]["required"])
        self.assertIn("root_tree", schemas["tri.snapshot.inspect"]["required"])
        self.assertIn("parents", schemas["tri.snapshot.inspect"]["required"])
        self.assertIn("source_events", schemas["tri.snapshot.inspect"]["required"])
        self.assertIn("access", schemas["tri.snapshot.inspect"]["required"])
        self.assertIn("entries", schemas["tri.tree.inspect"]["required"])
        self.assertIn("origins", schemas["tri.blob.inspect"]["required"])
        self.assertIn("snapshot_data", schemas["tri.targets.inspect"]["required"])
        self.assertIn("workspace_redacted", schemas["tri.operation.inspect"]["required"])
        self.assertIn("input_snapshot", schemas["tri.operation.inspect"]["required"])
        self.assertIn("output_snapshot", schemas["tri.operation.inspect"]["required"])
        self.assertIn("source_events", schemas["tri.operation.inspect"]["required"])
        self.assertIn("access", schemas["tri.operation.inspect"]["required"])
        self.assertIn("body", schemas["tri.task.inspect"]["required"])
        self.assertIn("workspaces", schemas["tri.task.inspect"]["required"])
        self.assertIn("runs", schemas["tri.task.inspect"]["required"])
        self.assertIn("changesets", schemas["tri.task.inspect"]["required"])
        self.assertIn("redacted_workspace", schemas["tri.task_run.inspect"]["required"])
        self.assertIn("task", schemas["tri.workspace.inspect"]["required"])
        self.assertIn("last_operation", schemas["tri.workspace.inspect"]["required"])
        self.assertIn("redacted_checks", schemas["tri.workspace.inspect"]["required"])
        self.assertIn("redacted_changesets", schemas["tri.workspace.inspect"]["required"])
        self.assertIn("ignored_paths", schemas["tri.workspace.materialization_lease"]["required"])
        self.assertIn("generated_artifacts", schemas["tri.workspace.materialization_lease"]["required"])
        self.assertIn("created_at", schemas["tri.workspace.materialization_lease"]["required"])
        self.assertIn("revoked_at", schemas["tri.workspace.materialization_lease"]["required"])
        self.assertIn("kind", schemas["tri.lease.inspect"]["required"])
        self.assertIn("lease", schemas["tri.lease.inspect"]["required"])
        self.assertIn("snapshot", schemas["tri.files.read"]["required"])
        self.assertIn("operation_details", schemas["tri.changeset.inspect"]["required"])
        self.assertIn("redacted_workspace", schemas["tri.changeset.inspect"]["required"])
        self.assertIn("actor_identity", schemas["tri.review.inspect"]["required"])
        self.assertIn("redacted_changeset", schemas["tri.review.inspect"]["required"])
        self.assertIn("metadata", schemas["tri.review_action.inspect"]["required"])
        self.assertIn("object_refs", schemas["tri.events.inspect"]["required"])
        self.assertIn("input_fingerprints", schemas["tri.events.inspect"]["required"])
        self.assertIn("policy_result", schemas["tri.events.inspect"]["required"])
        self.assertIn("object_type", schemas["tri.check.list"]["required"])
        self.assertIn("changeset", schemas["tri.check.list"]["required"])
        self.assertIn("task_run", schemas["tri.check.inspect"]["required"])
        self.assertIn("changeset", schemas["tri.check.inspect"]["required"])
        self.assertIn("redacted_workspace", schemas["tri.check.inspect"]["required"])
        self.assertIn("redacted_changeset", schemas["tri.check.inspect"]["required"])
        self.assertIn("secret_leases", schemas["tri.check.inspect"]["required"])
        self.assertIn("inputs", schemas["tri.check.inspect"]["required"])
        self.assertIn("outputs", schemas["tri.check.inspect"]["required"])
        self.assertIn("reproducibility", schemas["tri.check.inspect"]["required"])
        self.assertIn("fingerprint", schemas["tri.artifacts.inspect"]["required"])
        self.assertIn("created_at", schemas["tri.artifacts.inspect"]["required"])
        self.assertIn("affected_packages", schemas["tri.advisory.inspect"]["required"])
        self.assertIn("trusted_reviewers", schemas["tri.advisory.inspect"]["required"])
        self.assertIn("redacted", schemas["tri.advisory.inspect"]["required"])
        self.assertIn("packages", schemas["tri.release.inspect"]["required"])
        self.assertIn("checks", schemas["tri.release.inspect"]["required"])
        self.assertIn("artifacts", schemas["tri.release.inspect"]["required"])
        self.assertIn("policy_decisions", schemas["tri.release.inspect"]["required"])
        self.assertIn("redacted", schemas["tri.release.inspect"]["required"])
        self.assertIn("provider", schemas["tri.secrets.inspect"]["required"])
        self.assertIn("created_by", schemas["tri.secrets.inspect"]["required"])
        self.assertIn("revoked_at", schemas["tri.secrets.inspect"]["required"])
        self.assertIn("actor", schemas["tri.secret_lease.inspect"]["required"])
        self.assertIn("created_at", schemas["tri.secret_lease.inspect"]["required"])
        self.assertIn("revoked_at", schemas["tri.secret_lease.inspect"]["required"])
        self.assertIn("diagnostics", schemas["tri.export.inspect"]["required"])
        self.assertIn("target_remote", schemas["tri.export.inspect"]["required"])
        self.assertIn("target_ref", schemas["tri.export.inspect"]["required"])
        self.assertIn("created_at", schemas["tri.export.inspect"]["required"])
        self.assertIn("completed_at", schemas["tri.export.inspect"]["required"])
        self.assertIn("matched_grants", schemas["tri.policy.decision"]["required_any"][0])
        self.assertIn("object_access", schemas["tri.policy.decision"]["required_any"][1])
        emitted_diagnostic_codes = set()
        tree = ast.parse((ROOT / "tri" / "cli.py").read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = node.func.id if isinstance(node.func, ast.Name) else None
            if function_name in {"TriError", "decision_denial"} and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                emitted_diagnostic_codes.add(node.args[0].value)
        advertised_diagnostic_codes = set(schemas["tri.diagnostic"]["codes"])
        self.assertEqual(sorted(emitted_diagnostic_codes - advertised_diagnostic_codes), [])
        command_names = {item["name"] for item in commands}
        from tri.cli import build_parser

        def walk_parser(parser: argparse.ArgumentParser, parts: list[str]) -> list[tuple[list[str], argparse.ArgumentParser]]:
            subparsers = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
            if not subparsers:
                return [(parts, parser)]
            leaves = []
            for action in subparsers:
                for name, child in action.choices.items():
                    leaves.extend(walk_parser(child, [*parts, name]))
            return leaves

        parser_leaves = walk_parser(build_parser(), [])
        parser_commands = {"-".join(parts) for parts, _ in parser_leaves}
        self.assertEqual(sorted(parser_commands - command_names), [])
        missing = sorted({item["schema"] for item in commands if item["schema"] not in schemas})
        self.assertEqual(missing, [])
        usage_by_name = {item["name"]: item["usage"] for item in commands}
        undocumented_options = {}
        for parts, parser in parser_leaves:
            name = "-".join(parts)
            usage = usage_by_name[name]
            missing_options = []
            for action in parser._actions:
                options = [option for option in action.option_strings if option.startswith("--") and option not in ("--help", "--actor")]
                if options and not any(option in usage for option in options):
                    missing_options.append("/".join(options))
            if missing_options:
                undocumented_options[name] = missing_options
        self.assertEqual(undocumented_options, {})

    def test_actor_flag_matches_documented_subcommand_position(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        actor_view = self.tri("actors", "inspect", "agent:a")
        self.assertEqual(actor_view["data"]["id"], "agent:a")
        self.assertEqual(actor_view["data"]["kind"], "agent")
        self.assertEqual(actor_view["data"]["principal"], "actor:agent:a")
        self.assertIn("role:agent", actor_view["data"]["roles"])
        self.assertIn("actor:agent:a", actor_view["data"]["selectors"])
        self.assertFalse(actor_view["data"]["capabilities"]["maintainer"])
        actor_object = self.tri("actors", "inspect", "actor_abcdef123456")
        self.assertIn("actor_abcdef123456", actor_object["refs"]["actors"])
        self.assertEqual(actor_object["links"]["actor_abcdef123456"]["object_type"], "actor")
        generic_actor = self.tri("objects", "inspect", "actor_abcdef123456")
        self.assertEqual(generic_actor["data"]["object_type"], "actor")
        self.assertEqual(generic_actor["data"]["resolved_schema"], "tri.actor.inspect")
        self.assertEqual(generic_actor["data"]["result"]["id"], "actor_abcdef123456")

        created = self.tri("workspace", "create", "app", "--from", target, "--task", "agent attempt", "--actor", "agent:a")
        self.assertEqual(created["actor"], "agent:a")
        workspace = created["data"]["id"]

        inspected = self.tri("workspace", "inspect", workspace, "--actor=agent:a")
        self.assertEqual(inspected["actor"], "agent:a")
        self.assertEqual(inspected["data"]["owner"], "agent:a")

        self.tri("files", "write", workspace, "src/app.txt", "--text", "agent edit\n", "--actor", "agent:a")
        check = self.tri(
            "run",
            workspace,
            "--actor",
            "agent:a",
            "--",
            "python3",
            "-c",
            "import sys; assert '--actor' in sys.argv",
            "--actor",
        )
        self.assertEqual(check["actor"], "agent:a")
        self.assertEqual(check["data"]["status"], "passed")

        task_actor_view = self.tri("--actor", f"task:{created['data']['task']}", "actors", "inspect", f"task:{created['data']['task']}")
        self.assertEqual(task_actor_view["data"]["kind"], "task")
        self.assertEqual(task_actor_view["data"]["task_scope"], created["data"]["task"])
        self.assertEqual(task_actor_view["data"]["principal"], f"task:{created['data']['task']}")
        self.assertFalse(task_actor_view["data"]["capabilities"]["project_level"])

    def test_unknown_actor_identity_fails_closed(self) -> None:
        invalid = self.tri("--actor", "mystery", "commands", ok=False)
        self.assertEqual(invalid["actor"], "mystery")
        self.assertEqual(invalid["diagnostics"][0]["code"], "unknown_actor")
        self.assertIn("agent:<id>", invalid["diagnostics"][0]["accepted_formats"])
        self.assertIn("task:<task-id>", invalid["diagnostics"][0]["accepted_formats"])

        invalid_subject = self.tri("actors", "inspect", "mystery", ok=False)
        self.assertEqual(invalid_subject["diagnostics"][0]["code"], "unknown_actor")
        self.assertEqual(invalid_subject["diagnostics"][0]["actor"], "mystery")

        env = self.env.copy()
        env["TRI_ACTOR"] = "mystery"
        result = subprocess.run(
            ["python3", "-m", "tri", "commands"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        from_env = json.loads(result.stdout)
        self.assertEqual(from_env["actor"], "mystery")
        self.assertEqual(from_env["diagnostics"][0]["code"], "unknown_actor")

        valid = self.tri("--actor", "service:ci", "commands")
        self.assertEqual(valid["actor"], "service:ci")

    def test_import_maps_git_tree_to_tri_snapshot(self) -> None:
        imported = self.import_project()
        snapshot = imported["data"]["snapshot"]
        source = imported["data"]["source"]
        git_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        git_tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        git_files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=self.repo, check=True, text=True, stdout=subprocess.PIPE).stdout.splitlines()
        git_author = source["git_author"]

        self.assertEqual(source["source_path"], str(self.repo.resolve()))
        self.assertEqual(source["git_commit"], git_commit)
        self.assertEqual(source["git_tree"], git_tree)
        self.assertEqual(git_author["name"], "Tri Test")
        self.assertEqual(git_author["email"], "tri@example.com")
        self.assertEqual(git_author["actor"], "human:tri_example_com")
        self.assertEqual(source["tri_snapshot"], snapshot["id"])
        self.assertEqual(source["tri_root_tree"], snapshot["root_tree"])
        self.assertEqual(source["tri_content_fingerprint"], snapshot["content_fingerprint"])

        inspected_project = self.tri("projects", "inspect", "app")
        self.assert_schema_required_keys("tri.project.inspect", inspected_project["data"])
        self.assertEqual(inspected_project["data"]["id"], imported["data"]["project"]["id"])
        self.assertEqual(inspected_project["data"]["name"], "app")
        self.assertEqual(inspected_project["data"]["root_path"], str(self.repo.resolve()))
        self.assertEqual(inspected_project["data"]["default_target"], imported["data"]["target"]["name"])
        self.assertEqual(inspected_project["data"]["targets"][0]["snapshot"], snapshot["id"])
        self.assertEqual(inspected_project["data"]["counts"]["snapshots"], 1)
        self.assertEqual(inspected_project["data"]["counts"]["targets"], 1)
        self.assertEqual(inspected_project["data"]["counts"]["workspaces"], 0)
        inspected_target = self.tri("targets", "inspect", "app", imported["data"]["target"]["name"])
        self.assert_schema_required_keys("tri.targets.inspect", inspected_target["data"])
        self.assertEqual(inspected_target["data"]["id"], imported["data"]["target"]["id"])
        self.assertEqual(inspected_target["data"]["project"], imported["data"]["project"]["id"])
        self.assertEqual(inspected_target["data"]["snapshot"], snapshot["id"])
        self.assertEqual(inspected_target["data"]["snapshot_data"]["content_fingerprint"], snapshot["content_fingerprint"])
        inspected_target_by_id = self.tri("targets", "inspect", "app", imported["data"]["target"]["id"])
        self.assertEqual(inspected_target_by_id["data"]["name"], imported["data"]["target"]["name"])
        inspected_target_direct = self.tri("targets", "inspect", imported["data"]["target"]["id"])
        self.assertEqual(inspected_target_direct["data"]["snapshot"], snapshot["id"])
        inspected_target_object = self.tri("objects", "inspect", imported["data"]["target"]["id"])
        self.assertEqual(inspected_target_object["data"]["object_type"], "target")
        self.assertEqual(inspected_target_object["data"]["resolved_schema"], "tri.targets.inspect")
        self.assertEqual(inspected_target_object["data"]["result"]["snapshot"], snapshot["id"])
        listed_projects = self.tri("projects", "list")
        self.assert_schema_required_keys("tri.project.list", listed_projects["data"])
        self.assertEqual([item["name"] for item in listed_projects["data"]["projects"]], ["app"])

        inspected_snapshot = self.tri("snapshots", "inspect", snapshot["id"])
        self.assert_schema_required_keys("tri.snapshot.inspect", inspected_snapshot["data"])
        self.assertEqual(inspected_snapshot["data"]["id"], snapshot["id"])
        self.assertEqual(inspected_snapshot["data"]["root_tree"], snapshot["root_tree"])
        self.assertEqual(inspected_snapshot["data"]["parents"], [])
        self.assertEqual(inspected_snapshot["data"]["source"], "imported")
        self.assertEqual(inspected_snapshot["data"]["content_fingerprint"], snapshot["content_fingerprint"])
        self.assertEqual(inspected_snapshot["data"]["file_count"], len(git_files))
        self.assertEqual(inspected_snapshot["data"]["byte_count"], 10)
        self.assertEqual(inspected_snapshot["data"]["access"]["reason"], "public_source")
        self.assertIn(imported["data"]["target"]["id"], inspected_snapshot["data"]["targets"])
        self.assertEqual(inspected_snapshot["data"]["redacted"]["source_events"], 0)
        listed_snapshots = self.tri("snapshots", "list", "app")
        self.assert_schema_required_keys("tri.snapshot.list", listed_snapshots["data"])
        self.assertEqual([item["id"] for item in listed_snapshots["data"]["snapshots"]], [snapshot["id"]])

        listed = self.tri("files", "list", snapshot["id"])["data"]
        self.assertEqual([item["path"] for item in listed["files"]], git_files)
        read_snapshot_file = self.tri("files", "read", snapshot["id"], "src/app.txt")["data"]
        self.assert_schema_required_keys("tri.files.read", read_snapshot_file)
        self.assertEqual(read_snapshot_file["snapshot"], snapshot["id"])
        self.assertEqual(read_snapshot_file["text"], "one\n")
        inspected_tree = self.tri("trees", "inspect", snapshot["root_tree"])
        self.assert_schema_required_keys("tri.tree.inspect", inspected_tree["data"])
        self.assertEqual(inspected_tree["data"]["id"], snapshot["root_tree"])
        self.assertEqual([entry["path"] for entry in inspected_tree["data"]["entries"]], git_files)
        self.assertEqual(inspected_tree["data"]["access"]["reason"], "origin_access")
        inspected_blob = self.tri("blobs", "inspect", read_snapshot_file["blob"])
        self.assert_schema_required_keys("tri.blob.inspect", inspected_blob["data"])
        self.assertEqual(inspected_blob["data"]["id"], read_snapshot_file["blob"])
        self.assertFalse(inspected_blob["data"]["binary"])
        self.assertEqual(inspected_blob["data"]["origins"][0]["read_command"], ["files", "read", snapshot["id"], "src/app.txt"])
        inspected_blob_object = self.tri("objects", "inspect", read_snapshot_file["blob"])
        self.assertEqual(inspected_blob_object["data"]["object_type"], "blob")
        self.assertEqual(inspected_blob_object["data"]["resolved_schema"], "tri.blob.inspect")
        self.assertEqual(imported["data"]["files"], len(git_files))

    @unittest.skipIf(sys.platform == "win32", "symlink behavior differs on Windows")
    def test_import_preserves_symlink_targets_without_following(self) -> None:
        (self.repo / "readme-link").symlink_to("README.md")
        subprocess.run(["git", "add", "readme-link"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "add symlink"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        imported = self.import_project()
        snapshot = imported["data"]["snapshot"]["id"]

        listed = self.tri("files", "list", snapshot)["data"]["files"]
        link_entry = next(item for item in listed if item["path"] == "readme-link")
        self.assertEqual(link_entry["mode"], "120000")
        self.assertEqual(link_entry["symlink_target"], "README.md")
        stat = self.tri("files", "stat", snapshot, "readme-link")["data"]
        self.assertEqual(stat["type"], "symlink")
        self.assertEqual(stat["symlink_target"], "README.md")
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.assertEqual(self.tri("files", "read", snapshot, "readme-link")["data"]["text"], "README.md")
        self.assertEqual(self.tri("files", "read", workspace, "readme-link")["data"]["text"], "README.md")
        mount = self.root / "symlink-mounted"
        mounted = self.tri("workspace", "mount", workspace, str(mount))["data"]
        self.assertTrue((mount / "readme-link").is_symlink())
        self.assertEqual(os.readlink(mount / "readme-link"), "README.md")
        sync = self.tri("workspace", "sync", workspace, "--lease", mounted["lease"])
        self.assertEqual(sync["data"]["paths"], [])

        plain = self.root / "plain"
        plain.mkdir()
        outside = self.root / "outside-source"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("do not import this content\n")
        (plain / "outside-link").symlink_to(sentinel)
        directory_import = self.tri("import", str(plain), "--name", "plain")
        directory_snapshot = directory_import["data"]["snapshot"]["id"]
        directory_link = self.tri("files", "stat", directory_snapshot, "outside-link")["data"]
        self.assertEqual(directory_link["type"], "symlink")
        self.assertEqual(directory_link["symlink_target"], str(sentinel))
        directory_workspace = self.tri("workspace", "create", "plain", "--from", directory_import["data"]["target"]["name"])["data"]["id"]
        directory_link_text = self.tri("files", "read", directory_workspace, "outside-link")["data"]["text"]
        self.assertEqual(directory_link_text, str(sentinel))
        self.assertNotIn("do not import this content", directory_link_text)
        blocked_mount = self.tri("workspace", "mount", directory_workspace, str(self.root / "unsafe-symlink-mounted"), ok=False)
        self.assertEqual(blocked_mount["diagnostics"][0]["code"], "path_escaped_mount")
        self.assertEqual(blocked_mount["diagnostics"][0]["target"], str(sentinel))

    def test_core_workspace_to_review_and_patch_flow(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        created_workspace = self.tri("workspace", "create", "app", "--from", target, "--task", "change app")
        workspace = created_workspace["data"]["id"]
        self.assertIn(workspace, created_workspace["refs"]["workspaces"])
        self.assertEqual(created_workspace["links"][workspace]["object_type"], "workspace")
        self.assertEqual(created_workspace["links"][workspace]["inspect_route"], f"/v0/objects/{workspace}")
        self.assertIn(created_workspace["data"]["task"], created_workspace["refs"]["tasks"])
        self.assertIn(created_workspace["data"]["task_run"], created_workspace["refs"]["task_runs"])
        write = self.tri("files", "write", workspace, "src/app.txt", "--text", "two\n")
        self.assertTrue(write["ok"])
        self.assertEqual(write["data"]["paths"], ["src/app.txt"])
        self.assertIn(write["data"]["operation"], write["refs"]["operations"])
        self.assertEqual(write["links"][write["data"]["operation"]]["inspect_argv"], ["objects", "inspect", write["data"]["operation"]])
        self.assertIn(write["data"]["snapshot"], write["refs"]["snapshots"])
        inspected_operation = self.tri("operations", "inspect", write["data"]["operation"])
        self.assert_schema_required_keys("tri.operation.inspect", inspected_operation["data"])
        self.assertEqual(inspected_operation["data"]["workspace"], workspace)
        self.assertFalse(inspected_operation["data"]["workspace_redacted"])
        self.assertEqual(inspected_operation["data"]["kind"], "write_file")
        self.assertEqual(inspected_operation["data"]["input_snapshot"], imported["data"]["snapshot"]["id"])
        self.assertEqual(inspected_operation["data"]["output_snapshot"], write["data"]["snapshot"])
        self.assertEqual(inspected_operation["data"]["paths"], ["src/app.txt"])
        self.assertEqual(inspected_operation["data"]["access"]["reason"], "workspace")
        self.assertEqual(inspected_operation["data"]["source_events"], write["events"][-1:])
        workspace_operations = self.tri("operations", "list", workspace)
        self.assert_schema_required_keys("tri.operation.list", workspace_operations["data"])
        self.assertEqual([item["id"] for item in workspace_operations["data"]["operations"]], [write["data"]["operation"]])

        diff = self.tri("diff", workspace)
        self.assertEqual(diff["data"]["summary"]["modified"], 1)
        self.assertEqual(diff["data"]["summary"]["binary"], 0)
        diff_file = diff["data"]["files"][0]
        self.assertEqual(diff_file["path"], "src/app.txt")
        self.assertFalse(diff_file["binary"])
        self.assertTrue(diff_file["old_content_fingerprint"].startswith("sha256:"))
        self.assertTrue(diff_file["new_content_fingerprint"].startswith("sha256:"))
        self.assertNotEqual(diff_file["old_content_fingerprint"], diff_file["new_content_fingerprint"])
        summary_only = self.tri("diff", workspace, "--mode", "summary", "--path", "src/**")
        self.assertEqual(summary_only["data"]["mode"], "summary")
        self.assertEqual(summary_only["data"]["path_filters"], ["src/**"])
        self.assertEqual(summary_only["data"]["summary"]["modified"], 1)
        self.assertEqual(summary_only["data"]["files"], [])
        readme_only = self.tri("diff", workspace, "--mode", "files", "--path", "README.md")
        self.assertEqual(readme_only["data"]["summary"]["modified"], 0)
        self.assertEqual(readme_only["data"]["files"], [])
        patch_diff = self.tri("diff", workspace, "--mode", "patch", "--path", "src/app.txt")
        self.assertIn("diff --git a/src/app.txt b/src/app.txt", patch_diff["data"]["patch"])
        self.assertIn("+two", patch_diff["data"]["files"][0]["patch"])
        snapshot_diff = self.tri("diff", imported["data"]["snapshot"]["id"], write["data"]["snapshot"])
        self.assertEqual(snapshot_diff["data"]["base_snapshot"], imported["data"]["snapshot"]["id"])
        self.assertEqual(snapshot_diff["data"]["head_snapshot"], write["data"]["snapshot"])
        self.assertEqual(snapshot_diff["data"]["summary"], diff["data"]["summary"])
        self.assertEqual(snapshot_diff["data"]["files"], diff["data"]["files"])

        check = self.tri(
            "run",
            workspace,
            "--assumption",
            "src/app.txt is the only intended behavior surface",
            "--",
            "python3",
            "-c",
            "from pathlib import Path; assert Path('src/app.txt').read_text() == 'two\\n'",
        )
        self.assertEqual(check["data"]["status"], "passed")
        self.assert_schema_required_keys("tri.check.inspect", check["data"])
        self.assertEqual(check["data"]["actor"], "actor_local")
        self.assertEqual(check["data"]["snapshot_before"], write["data"]["snapshot"])
        self.assertEqual(check["data"]["task_run"], created_workspace["data"]["task_run"])
        self.assertIsNone(check["data"]["changeset"])
        self.assertFalse(check["data"]["redacted_workspace"])
        self.assertFalse(check["data"]["redacted_changeset"])
        self.assertEqual(check["data"]["secret_leases"], [])
        self.assertGreaterEqual(check["data"]["cost"]["wall_ms"], 0)
        self.assertGreater(check["data"]["cost"]["input_bytes"], 0)
        self.assertEqual(check["data"]["cost"]["external_cost"], "0 USD")
        self.assertEqual(check["data"]["assumptions"], ["src/app.txt is the only intended behavior surface"])
        self.assertEqual(check["data"]["inputs"]["snapshot"], write["data"]["snapshot"])
        self.assertEqual(check["data"]["inputs"]["workspace"], workspace)
        self.assertEqual(check["data"]["outputs"]["snapshot"], check["data"]["snapshot_after"])
        self.assertEqual(check["data"]["outputs"]["file_changes"], [])
        self.assertEqual(check["data"]["outputs"]["logs"], check["data"]["logs"])
        self.assertEqual(check["data"]["environment_policy"]["mode"], "process_env_plus_scoped_secret_leases")
        self.assertEqual(check["data"]["environment_policy"]["secret_leases"], [])
        self.assertEqual(check["data"]["environment_policy"]["secret_env"], [])
        self.assertEqual(check["data"]["materialized_directory"]["mode"], "ephemeral_workspace_check")
        self.assertEqual(check["data"]["materialized_directory"]["input_snapshot"], write["data"]["snapshot"])
        self.assertEqual(check["data"]["materialized_directory"]["output_snapshot"], check["data"]["snapshot_after"])
        self.assertFalse(check["data"]["materialized_directory"]["mutated"])
        self.assertTrue(check["data"]["materialized_directory"]["fingerprint_before"].startswith("sha256:"))
        self.assertEqual(check["data"]["materialized_directory"]["fingerprint_before"], check["data"]["materialized_directory"]["fingerprint_after"])
        self.assertEqual(
            [self.tri("events", "inspect", event)["data"]["kind"] for event in check["events"]],
            ["task_run.checking", "policy.evaluated", "check.started", "check.completed"],
        )
        self.assertIn(check["data"]["id"], check["refs"]["checks"])
        self.assertEqual(set(check["refs"]["artifacts"]), {check["data"]["logs"]["stdout"], check["data"]["logs"]["stderr"]})
        inspected_check = self.tri("checks", "inspect", check["data"]["id"])["data"]
        self.assert_schema_required_keys("tri.check.inspect", inspected_check)
        self.assertEqual(inspected_check["actor"], "actor_local")
        self.assertEqual(inspected_check["cost"], check["data"]["cost"])
        self.assertEqual(inspected_check["assumptions"], check["data"]["assumptions"])
        self.assertEqual(inspected_check["materialized_directory"], check["data"]["materialized_directory"])
        self.assertEqual(inspected_check["environment_policy"], check["data"]["environment_policy"])

        changeset = self.tri("changeset", "create", workspace, "--title", "Change app")
        changeset_id = changeset["data"]["id"]
        self.assert_schema_required_keys("tri.changeset.inspect", changeset["data"])
        self.assertEqual(changeset["data"]["status"], "ready")
        self.assertIn("src/app.txt", changeset["data"]["touched_paths"])
        self.assertIn(changeset_id, changeset["refs"]["changesets"])

        review = self.tri("review", "create", changeset_id)
        self.assertEqual(review["data"]["changeset"], changeset_id)
        self.assertEqual(review["data"]["check_summary"]["passed"], 1)
        self.assertIn(review["data"]["id"], review["refs"]["reviews"])
        review_surface = self.tri("review", "inspect", review["data"]["id"])
        self.assert_schema_required_keys("tri.review.inspect", review_surface["data"])
        self.assertEqual(review_surface["data"]["changeset_summary"]["touched_paths"], ["src/app.txt"])
        self.assertEqual(review_surface["data"]["task"]["title"], "change app")
        self.assertEqual(review_surface["data"]["task_run"]["assumptions"], ["src/app.txt is the only intended behavior surface"])
        self.assertEqual(review_surface["data"]["commands"][0]["command"][0], "python3")
        self.assertEqual(review_surface["data"]["commands"][0]["assumptions"], ["src/app.txt is the only intended behavior surface"])
        self.assertEqual(len(review_surface["data"]["artifacts"]), 2)
        self.assertEqual(review_surface["data"]["policy_findings"]["decision"]["reason_code"], "missing_review_approval")
        self.assertEqual(review_surface["data"]["visibility_impact"]["diff_summary"]["modified"], 1)
        self.tri("review", "approve", review["data"]["id"])
        approved_surface = self.tri("review", "inspect", review["data"]["id"])
        self.assertTrue(approved_surface["data"]["policy_findings"]["decision"]["allowed"])

        patch_path = self.root / "change.patch"
        export = self.tri("export", "git-patch", changeset_id, "--output", str(patch_path))
        self.assertTrue(export["ok"])
        self.assertIn(export["data"]["export"], export["refs"]["exports"])
        self.assertTrue(patch_path.exists())
        self.assertIn("diff --git a/src/app.txt b/src/app.txt", patch_path.read_text())
        self.assert_schema_required_keys("tri.export.inspect", export["data"]["record"])
        self.assertEqual(export["data"]["record"]["diagnostics"], [])
        self.assertIsNone(export["data"]["record"]["target_remote"])
        self.assertIsNone(export["data"]["record"]["target_ref"])
        self.assertTrue(export["data"]["record"]["determinism"]["deterministic"])
        self.assertEqual(export["data"]["record"]["determinism"]["reason_code"], "content_addressed_patch")
        exported_changeset = self.tri("changeset", "inspect", changeset_id)["data"]
        self.assertEqual(exported_changeset["status"], "exported")
        self.assertEqual(exported_changeset["export_status"], "exported")
        self.assertEqual(exported_changeset["lifecycle"]["previous_status"], "approved")
        self.assertEqual(exported_changeset["lifecycle"]["transition"], "approved->exported")
        self.assertEqual(exported_changeset["lifecycle"]["last_export"], export["data"]["export"])

        repeat = self.tri("export", "git-patch", changeset_id)
        self.assertEqual(repeat["data"]["record"]["determinism"]["repeat_of"], export["data"]["export"])
        self.assertTrue(repeat["data"]["record"]["determinism"]["same_output_as_repeat"])
        self.assertEqual(repeat["data"]["record"]["policy_decision"]["changeset_lifecycle"]["status"], "exported")
        self.assertEqual(
            repeat["data"]["record"]["determinism"]["artifact_fingerprint"],
            export["data"]["record"]["determinism"]["artifact_fingerprint"],
        )

    def test_git_patch_export_supports_binary_files(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        binary_path = self.root / "blob.bin"
        binary_bytes = b"tri\x00binary\x01payload\n"
        binary_path.write_bytes(binary_bytes)
        self.tri("files", "write", workspace, "assets/blob.bin", "--body", str(binary_path))
        changeset_id = self.tri("changeset", "create", workspace, "--title", "Add binary")["data"]["id"]

        patch_path = self.root / "binary.patch"
        export = self.tri("export", "git-patch", changeset_id, "--override-reason", "binary export smoke", "--output", str(patch_path))
        self.assertTrue(export["ok"])
        patch_text = patch_path.read_text()
        self.assertIn("diff --git a/assets/blob.bin b/assets/blob.bin", patch_text)
        self.assertIn("GIT binary patch", patch_text)
        subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=self.repo, check=True)
        subprocess.run(["git", "apply", str(patch_path)], cwd=self.repo, check=True)
        self.assertEqual((self.repo / "assets" / "blob.bin").read_bytes(), binary_bytes)

    def test_http_api_runs_agent_command_workflow(self) -> None:
        proc, port = self.start_server()
        try:
            root = self.api_get(port, "/v0", actor="agent:api")
            self.assertEqual(root["schema"], "tri.api.server")
            self.assertEqual(root["actor"], "agent:api")
            advertised = root["data"]["endpoints"]
            for name in (
                "health",
                "commands",
                "schema",
                "schema_named",
                "command",
                "batch",
                "object_inspect",
                "actor",
                "projects",
                "project",
                "project_packages",
                "package",
                "project_targets",
                "project_target",
                "target",
                "project_snapshots",
                "project_workspaces",
                "project_search",
                "project_check_sets",
                "check_set",
                "project_advisories",
                "advisory",
                "project_releases",
                "release",
                "project_exports",
                "export",
                "project_secrets",
                "secret",
                "project_events",
                "task",
                "task_run",
                "task_attempts",
                "snapshot",
                "snapshot_files",
                "snapshot_file",
                "tree",
                "blob",
                "operation",
                "workspace",
                "workspace_files",
                "workspace_file",
                "workspace_search",
                "workspace_diff",
                "workspace_operations",
                "workspace_checks",
                "workspace_conflicts",
                "workspace_leases",
                "lease",
                "changeset",
                "changeset_operations",
                "changeset_checks",
                "changeset_diff",
                "review",
                "review_action",
                "check",
                "check_artifacts",
                "check_log",
                "conflicts",
                "conflict",
                "grant",
                "artifacts",
                "artifact",
                "artifact_content",
                "events",
                "event",
                "policy_decision",
            ):
                self.assertIn(name, advertised)
                self.assertIn("/v0/", advertised[name])

            commands = self.api_get(port, "/v0/commands")
            self.assertTrue(any(item["name"] == "serve" for item in commands["data"]["commands"]))

            schema = self.api_get(port, "/v0/schema/tri.workspace.inspect")
            self.assertTrue(schema["ok"])
            self.assertIn("required", schema["data"]["schema"])

            imported = self.api_post(
                port,
                "/v0/command",
                {"actor": "agent:api", "argv": ["import", str(self.repo), "--name", "app"]},
            )
            self.assertEqual(imported["actor"], "agent:api")
            target = imported["data"]["target"]["name"]
            actor_view = self.api_get(port, "/v0/actors/agent%3Aapi", actor="agent:api")
            self.assertEqual(actor_view["data"]["id"], "agent:api")
            self.assertIn("role:agent", actor_view["data"]["roles"])
            projects = self.api_get(port, "/v0/projects", actor="agent:api")
            self.assertEqual(projects["data"]["projects"][0]["name"], "app")
            project_view = self.api_get(port, "/v0/projects/app", actor="agent:api")
            self.assertEqual(project_view["data"]["id"], imported["data"]["project"]["id"])
            self.assertEqual(project_view["data"]["default_target"], target)
            self.assertEqual(self.api_get(port, "/v0/projects/app/packages", actor="agent:api")["data"]["packages"], [])
            package = self.api_post(
                port,
                "/v0/command",
                {"actor": "actor_local", "argv": ["packages", "declare", "app", "api", "--path", "src/**"]},
            )["data"]
            self.assertEqual(self.api_get(port, f"/v0/packages/{package['id']}", actor="agent:api")["data"]["name"], "api")
            self.assertEqual(self.api_get(port, "/v0/projects/app/packages/api", actor="agent:api")["data"]["id"], package["id"])
            targets = self.api_get(port, "/v0/projects/app/targets", actor="agent:api")
            self.assertEqual(targets["data"]["targets"][0]["name"], target)
            target_view = self.api_get(port, f"/v0/projects/app/targets/{target}", actor="agent:api")
            self.assertEqual(target_view["data"]["snapshot"], imported["data"]["snapshot"]["id"])
            self.assertEqual(target_view["data"]["snapshot_data"]["root_tree"], imported["data"]["snapshot"]["root_tree"])
            direct_target_view = self.api_get(port, f"/v0/targets/{imported['data']['target']['id']}", actor="agent:api")
            self.assertEqual(direct_target_view["data"]["name"], target)
            generic_target_view = self.api_get(port, f"/v0/objects/{imported['data']['target']['id']}", actor="agent:api")
            self.assertEqual(generic_target_view["data"]["resolved_schema"], "tri.targets.inspect")
            project_snapshots = self.api_get(port, "/v0/projects/app/snapshots", actor="agent:api")
            self.assertEqual(project_snapshots["data"]["snapshots"][0]["id"], imported["data"]["snapshot"]["id"])
            snapshot_view = self.api_get(port, f"/v0/snapshots/{imported['data']['snapshot']['id']}", actor="agent:api")
            self.assertEqual(snapshot_view["data"]["root_tree"], imported["data"]["snapshot"]["root_tree"])
            self.assertEqual(snapshot_view["data"]["access"]["reason"], "public_source")
            tree_view = self.api_get(port, f"/v0/trees/{imported['data']['snapshot']['root_tree']}", actor="agent:api")
            self.assertEqual([entry["path"] for entry in tree_view["data"]["entries"]], ["README.md", "src/app.txt"])
            snapshot_files = self.api_get(port, f"/v0/snapshots/{imported['data']['snapshot']['id']}/files", actor="agent:api")
            self.assertEqual([item["path"] for item in snapshot_files["data"]["files"]], ["README.md", "src/app.txt"])
            snapshot_file = self.api_get(port, f"/v0/snapshots/{imported['data']['snapshot']['id']}/files/src/app.txt", actor="agent:api")
            self.assertEqual(snapshot_file["data"]["text"], "one\n")
            blob_view = self.api_get(port, f"/v0/blobs/{snapshot_file['data']['blob']}", actor="agent:api")
            self.assertEqual(blob_view["data"]["origins"][0]["read_route"], f"/v0/snapshots/{imported['data']['snapshot']['id']}/files/src/app.txt")
            generic_blob_view = self.api_get(port, f"/v0/objects/{snapshot_file['data']['blob']}", actor="agent:api")
            self.assertEqual(generic_blob_view["data"]["object_type"], "blob")
            self.assertEqual(self.api_get(port, "/v0/projects/app/workspaces", actor="agent:api")["data"]["workspaces"], [])
            self.assertEqual(self.api_get(port, "/v0/projects/app/check-sets", actor="agent:api")["data"]["check_sets"], [])
            self.assertEqual(self.api_get(port, "/v0/projects/app/advisories", actor="agent:api")["data"]["advisories"], [])
            self.assertEqual(self.api_get(port, "/v0/projects/app/releases", actor="agent:api")["data"]["releases"], [])
            self.assertEqual(self.api_get(port, "/v0/projects/app/exports", actor="agent:api")["data"]["exports"], [])
            self.assertEqual(self.api_get(port, "/v0/projects/app/secrets", actor="agent:api")["data"]["secrets"], [])
            self.assertGreaterEqual(len(self.api_get(port, "/v0/projects/app/events?limit=5", actor="agent:api")["data"]["events"]), 1)
            self.assertGreaterEqual(len(self.api_get(port, "/v0/events?project=app&limit=5", actor="agent:api")["data"]["events"]), 1)

            check_set = self.api_post(
                port,
                "/v0/command",
                {"actor": "actor_local", "argv": ["checks", "set-create", "app", "api-smoke", "--required-check", "python3 -c pass"]},
            )["data"]
            check_set_view = self.api_get(port, f"/v0/check-sets/{check_set['id']}", actor="agent:api")
            self.assertEqual(check_set_view["data"]["id"], check_set["id"])
            self.assertEqual(check_set_view["data"]["required_checks"][0]["command"], ["python3", "-c", "pass"])

            secret_file = self.root / "api-token.txt"
            secret_file.write_text("api-secret")
            secret = self.api_post(
                port,
                "/v0/command",
                {"actor": "actor_local", "argv": ["secrets", "put", "app", "API_TOKEN", "--value-file", str(secret_file)]},
            )["data"]
            secret_view = self.api_get(port, f"/v0/secrets/{secret['id']}", actor="actor_local")
            self.assertEqual(secret_view["data"]["id"], secret["id"])
            blocked_secret = self.api_get(port, f"/v0/secrets/{secret['id']}", actor="agent:api")
            self.assertFalse(blocked_secret["ok"])
            self.assertEqual(blocked_secret["diagnostics"][0]["code"], "policy_denied")

            workspace = self.api_post(
                port,
                "/v0/command",
                {"actor": "agent:api", "argv": ["workspace", "create", "app", "--from", target, "--task", "api edit"]},
            )["data"]["id"]
            project_workspaces = self.api_get(port, "/v0/projects/app/workspaces", actor="agent:api")
            self.assertEqual(project_workspaces["data"]["workspaces"][0]["id"], workspace)
            workspace_view = self.api_get(port, f"/v0/workspaces/{workspace}", actor="agent:api")
            self.assertEqual(workspace_view["actor"], "agent:api")
            self.assertEqual(workspace_view["data"]["id"], workspace)
            task = workspace_view["data"]["task"]
            run = workspace_view["data"]["task_run"]
            self.assertEqual(self.api_get(port, f"/v0/tasks/{task}", actor="agent:api")["data"]["id"], task)
            task_run = self.api_get(port, f"/v0/task-runs/{run}", actor="agent:api")
            self.assertEqual(task_run["data"]["id"], run)
            self.assertEqual(task_run["data"]["workspace"], workspace)
            task_attempts = self.api_get(port, f"/v0/tasks/{task}/attempts", actor="agent:api")
            self.assertEqual(task_attempts["data"]["workspaces"][0]["id"], workspace)
            mounted = self.api_post(
                port,
                "/v0/command",
                {"actor": "agent:api", "argv": ["workspace", "mount", workspace, str(self.root / "api-mounted")]},
            )["data"]
            lease_view = self.api_get(port, f"/v0/leases/{mounted['lease']}", actor="agent:api")
            self.assertEqual(lease_view["data"]["id"], mounted["lease"])
            self.assertEqual(lease_view["data"]["kind"], "materialization")
            self.assertEqual(lease_view["data"]["lease"]["workspace"], workspace)
            grant = self.api_post(
                port,
                "/v0/command",
                {"actor": "actor_local", "argv": ["policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read"]},
            )["data"]
            grant_view = self.api_get(port, f"/v0/grants/{grant['id']}", actor="agent:b")
            self.assertEqual(grant_view["data"]["id"], grant["id"])
            self.assertEqual(grant_view["data"]["object"], workspace)
            leases = self.api_get(port, f"/v0/workspaces/{workspace}/leases", actor="agent:api")
            self.assertEqual(leases["data"]["workspace"], workspace)

            self.api_post(port, "/v0/command", {"actor": "agent:api", "argv": ["files", "write", workspace, "src/app.txt", "--text", "api edit\n"]})
            operation = self.api_get(port, f"/v0/workspaces/{workspace}/operations", actor="agent:api")["data"]["operations"][0]
            self.assertEqual(operation["kind"], "write_file")
            self.assertEqual(self.api_get(port, f"/v0/operations/{operation['id']}", actor="agent:api")["data"]["workspace"], workspace)

            read = self.api_post(port, "/v0/command", {"actor": "agent:api", "argv": ["files", "read", workspace, "src/app.txt"]})
            self.assertEqual(read["data"]["text"], "api edit\n")
            batch = self.api_post(
                port,
                "/v0/batch",
                {
                    "actor": "agent:api",
                    "commands": [
                        {"as": "workspace", "argv": ["workspace", "inspect", workspace]},
                        {"as": "stat", "argv": ["files", "stat", "${workspace.data.id}", "src/app.txt"]},
                    ],
                },
            )
            self.assertTrue(batch["ok"])
            self.assertEqual(batch["schema"], "tri.api.batch")
            self.assertEqual(batch["data"]["executed"], 2)
            self.assertEqual(batch["data"]["results"][1]["data"]["exists"], True)
            workspace_search = self.api_get(port, f"/v0/workspaces/{workspace}/search?q=api&glob=src/*&limit=5", actor="agent:api")
            self.assertEqual(workspace_search["data"]["matches"][0]["path"], "src/app.txt")
            project_search = self.api_get(port, "/v0/projects/app/search?q=api&scope=workspaces&limit=5", actor="agent:api")
            self.assertEqual(project_search["data"]["matches"][0]["scope"], "workspaces")
            get_read = self.api_get(port, f"/v0/workspaces/{workspace}/files/src/app.txt", actor="agent:api")
            self.assertEqual(get_read["data"]["text"], "api edit\n")
            get_files = self.api_get(port, f"/v0/workspaces/{workspace}/files", actor="agent:api")
            self.assertEqual([item["path"] for item in get_files["data"]["files"]], ["README.md", "src/app.txt"])
            api_diff = self.api_get(port, f"/v0/workspaces/{workspace}/diff?mode=summary&path=src/app.txt", actor="agent:api")
            self.assertEqual(api_diff["data"]["mode"], "summary")
            self.assertEqual(api_diff["data"]["summary"]["modified"], 1)

            check = self.api_post(
                port,
                "/v0/command",
                {"actor": "agent:api", "argv": ["run", workspace, "--assumption", "api route smoke", "--", "python3", "-c", "print('api ok')"]},
            )["data"]
            check_view = self.api_get(port, f"/v0/checks/{check['id']}", actor="agent:api")
            self.assertEqual(check_view["data"]["assumptions"], ["api route smoke"])
            self.assertIsNone(check_view["data"]["materialized_directory"]["path"])
            self.assertTrue(check_view["data"]["materialized_directory"]["path_redacted"])
            workspace_checks = self.api_get(port, f"/v0/workspaces/{workspace}/checks", actor="agent:api")
            self.assertEqual(workspace_checks["data"]["checks"][0]["id"], check["id"])
            check_artifacts = self.api_get(port, f"/v0/checks/{check['id']}/artifacts", actor="agent:api")
            self.assertEqual(len(check_artifacts["data"]["artifacts"]), 2)
            log = self.api_get(port, f"/v0/checks/{check['id']}/logs/stdout?limit=6", actor="agent:api")
            self.assertEqual(log["data"]["text"], "api ok")
            stdout_artifact = check["logs"]["stdout"]
            artifacts = self.api_get(port, "/v0/artifacts?kind=stdout&limit=10", actor="agent:api")
            self.assertIn(stdout_artifact, [item["id"] for item in artifacts["data"]["artifacts"]])
            artifact_view = self.api_get(port, f"/v0/artifacts/{stdout_artifact}", actor="agent:api")
            self.assertEqual(artifact_view["data"]["id"], stdout_artifact)
            self.assertIsNone(artifact_view["data"]["path"])
            self.assertTrue(artifact_view["data"]["fingerprint"].startswith("sha256:"))
            self.assertEqual(artifact_view["data"]["storage"]["content_route"], f"/v0/artifacts/{stdout_artifact}/content")
            artifact_content = self.api_get(port, f"/v0/artifacts/{stdout_artifact}/content", actor="agent:api")
            self.assertEqual(artifact_content["data"]["text"], "api ok\n")
            self.assertIsNone(artifact_content["data"]["path"])
            self.assertEqual(artifact_content["data"]["fingerprint"], artifact_view["data"]["fingerprint"])
            workspace_events = self.api_get(port, f"/v0/workspaces/{workspace}/events?limit=5", actor="agent:api")
            self.assertGreaterEqual(len(workspace_events["data"]["events"]), 1)

            changeset = self.api_post(
                port,
                "/v0/command",
                {"actor": "agent:api", "argv": ["changeset", "create", workspace, "--title", "API edit"]},
            )["data"]["id"]
            self.assertEqual(self.api_get(port, f"/v0/changesets/{changeset}", actor="agent:api")["data"]["id"], changeset)
            changeset_operations = self.api_get(port, f"/v0/changesets/{changeset}/operations", actor="agent:api")
            self.assertEqual(changeset_operations["data"]["operations"][0]["id"], operation["id"])
            changeset_checks = self.api_get(port, f"/v0/changesets/{changeset}/checks", actor="agent:api")
            self.assertEqual(changeset_checks["data"]["changeset"], changeset)
            self.assertEqual(changeset_checks["data"]["checks"][0]["id"], check["id"])
            changeset_diff = self.api_get(port, f"/v0/changesets/{changeset}/diff?mode=patch&path=src/app.txt", actor="agent:api")
            self.assertIn("diff --git a/src/app.txt b/src/app.txt", changeset_diff["data"]["patch"])
            decision = self.api_post(port, "/v0/command", {"actor": "agent:api", "argv": ["policy", "evaluate", "export", changeset]})
            self.assertFalse(decision["ok"])
            explained = self.api_get(port, f"/v0/policy/decisions/{decision['data']['id']}", actor="agent:api")
            self.assertEqual(explained["data"]["id"], decision["data"]["id"])
            review = self.api_post(port, "/v0/command", {"actor": "agent:api", "argv": ["review", "create", changeset]})["data"]["id"]
            self.assertEqual(self.api_get(port, f"/v0/reviews/{review}", actor="agent:api")["data"]["id"], review)
            action = self.api_post(
                port,
                "/v0/command",
                {"actor": "agent:api", "argv": ["review", "comment", review, "--body", "API review note"]},
            )["data"]["action"]["id"]
            action_view = self.api_get(port, f"/v0/review-actions/{action}", actor="agent:api")
            self.assertEqual(action_view["data"]["id"], action)
            self.assertEqual(action_view["data"]["review"], review)

            blocked = self.api_post(port, "/v0/command", {"actor": "agent:api", "argv": ["serve"]})
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["diagnostics"][0]["code"], "invalid_command")

            unknown = self.api_post(port, "/v0/command", {"actor": "mystery", "argv": ["commands"]})
            self.assertFalse(unknown["ok"])
            self.assertEqual(unknown["actor"], "mystery")
            self.assertEqual(unknown["diagnostics"][0]["code"], "unknown_actor")
            unknown_get = self.api_get(port, f"/v0/workspaces/{workspace}", actor="mystery")
            self.assertFalse(unknown_get["ok"])
            self.assertEqual(unknown_get["diagnostics"][0]["code"], "unknown_actor")
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)

    def test_git_commit_export_applies_changeset_to_clean_repo(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "two\n")
        self.tri(
            "run",
            workspace,
            "--",
            "python3",
            "-c",
            "from pathlib import Path; assert Path('src/app.txt').read_text() == 'two\\n'",
        )
        changeset_id = self.tri("changeset", "create", workspace, "--title", "Change app")["data"]["id"]
        review_id = self.tri("review", "create", changeset_id)["data"]["id"]
        self.tri("review", "approve", review_id)

        export = self.tri("export", "git-commit", changeset_id, "--message", "tri export")
        self.assertTrue(export["data"]["commit"])
        self.assertEqual(export["data"]["repo"], str(self.repo.resolve()))
        self.assert_schema_required_keys("tri.export.inspect", export["data"]["record"])
        self.assertEqual(export["data"]["record"]["export_kind"], "git_commit")
        self.assertEqual(export["data"]["record"]["target_remote"], str(self.repo.resolve()))
        self.assertIsNone(export["data"]["record"]["target_ref"])
        self.assertEqual(export["data"]["record"]["diagnostics"], [])
        self.assertIn(export["data"]["commit"], export["data"]["record"]["exported_object_ids"])
        self.assertGreaterEqual(export["data"]["source_events"]["count"], 1)
        self.assertEqual(self.tri("changeset", "inspect", changeset_id)["data"]["status"], "exported")
        self.assertEqual((self.repo / "src" / "app.txt").read_text(), "two\n")

        inspected = self.tri("export", "inspect", export["data"]["export"])
        self.assert_schema_required_keys("tri.export.inspect", inspected["data"])
        self.assertEqual(inspected["data"]["id"], export["data"]["export"])
        self.assertEqual(inspected["data"]["changeset"], changeset_id)
        self.assertEqual(inspected["data"]["policy_decision"]["reason_code"], "allowed")
        self.assertIn("export.git_commit", {event["kind"] for event in inspected["data"]["source_events"]["events"]})
        listed = self.tri("export", "list", "app", "--changeset", changeset_id)
        self.assertEqual([item["id"] for item in listed["data"]["exports"]], [export["data"]["export"]])
        self.assert_schema_required_keys("tri.export.inspect", listed["data"]["exports"][0])

        denied = self.tri("--actor", "agent:b", "export", "inspect", export["data"]["export"], ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        redacted = self.tri("--actor", "agent:b", "export", "list", "app")
        self.assertEqual(redacted["data"]["exports"], [])
        self.assertEqual(redacted["data"]["redacted"], 1)
        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        allowed = self.tri("--actor", "agent:b", "export", "inspect", export["data"]["export"])
        self.assertEqual(allowed["data"]["id"], export["data"]["export"])

        repeat = self.tri("export", "git-commit", changeset_id, "--repo", str(self.repo), "--message", "tri export", ok=False)
        self.assertEqual(repeat["data"]["record"]["determinism"]["repeat_of"], export["data"]["export"])
        self.assertFalse(repeat["data"]["record"]["determinism"]["deterministic"])
        self.assertEqual(repeat["data"]["record"]["determinism"]["reason_code"], "git_commit_external_state")

    def test_github_pr_export_prepares_branch_and_request_artifact(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "pr edit\n")

        blocked_changeset = self.tri("changeset", "create", workspace, "--title", "Blocked PR")["data"]["id"]
        blocked = self.tri("export", "github-pr", blocked_changeset, "--repo", str(self.repo), "--branch", "tri/blocked", "--base", target, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "missing_required_check")

        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'pr edit\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Prepare PR")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "approve", review)

        export = self.tri(
            "export",
            "github-pr",
            changeset,
            "--repo",
            str(self.repo),
            "--branch",
            "tri/pr-export",
            "--base",
            target,
            "--title",
            "Prepared by Tri",
            "--body",
            "PR body",
            "--draft",
        )
        self.assertEqual(export["data"]["branch"], "tri/pr-export")
        self.assertTrue(export["data"]["commit"])
        self.assertEqual(
            subprocess.run(["git", "branch", "--show-current"], cwd=self.repo, text=True, stdout=subprocess.PIPE, check=True).stdout.strip(),
            "tri/pr-export",
        )
        self.assertEqual((self.repo / "src" / "app.txt").read_text(), "pr edit\n")

        artifact = self.tri("artifacts", "read", export["data"]["artifact"])
        payload = json.loads(artifact["data"]["text"])
        self.assertEqual(payload["kind"], "github_pr_request")
        self.assertEqual(payload["title"], "Prepared by Tri")
        self.assertEqual(payload["body"], "PR body")
        self.assertTrue(payload["draft"])
        self.assertIn(payload["patch_artifact"], export["data"]["record"]["exported_object_ids"])

        self.tri("policy", "grant", changeset, "--principal", "actor:agent:b", "--action", "changeset.read")
        patch_artifact = self.tri("--actor", "agent:b", "artifacts", "read", payload["patch_artifact"])
        self.assertIn("+pr edit", patch_artifact["data"]["text"])
        self.assertEqual(patch_artifact["data"]["access"]["matched_origins"][0]["type"], "export")

    def test_changeset_merge_promotes_approved_work_into_target(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target, "--task", "merge me")["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "merged\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'merged\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Merge into target")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "approve", review)

        merged = self.tri("changeset", "merge", changeset, "--target", target)
        self.assertEqual(merged["data"]["status"], "merged")
        self.assertEqual(merged["data"]["mode"], "fast_forward")
        self.assertEqual(self.tri("changeset", "inspect", changeset)["data"]["status"], "merged")
        self.assertEqual(self.tri("workspace", "inspect", workspace)["data"]["status"], "promoted")

        targets = self.tri("targets", "list", "app")["data"]["targets"]
        self.assertEqual(targets[0]["snapshot"], merged["data"]["snapshot"])
        next_workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.assertEqual(self.tri("files", "read", next_workspace, "src/app.txt")["data"]["text"], "merged\n")

        blocked = self.tri("files", "write", workspace, "src/app.txt", "--text", "blocked\n", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "policy_denied")

    def test_changeset_merge_handles_non_conflicting_target_movement(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "changeset edit\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'changeset edit\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Merge after target move")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "approve", review)

        (self.repo / "README.md").write_text("target advanced\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "advance target"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.tri("targets", "refresh", "app", target)

        merged = self.tri("changeset", "merge", changeset, "--target", target)
        self.assertEqual(merged["data"]["status"], "merged")
        self.assertEqual(merged["data"]["mode"], "merge")
        self.assertEqual(merged["data"]["changed_paths"], ["src/app.txt"])

        next_workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.assertEqual(self.tri("files", "read", next_workspace, "src/app.txt")["data"]["text"], "changeset edit\n")
        self.assertEqual(self.tri("files", "read", next_workspace, "README.md")["data"]["text"], "target advanced\n")

    def test_targets_refresh_rejects_stale_expected_snapshot(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        original = imported["data"]["snapshot"]["id"]

        (self.repo / "README.md").write_text("target moved once\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "move target once"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        first_refresh = self.tri("targets", "refresh", "app", target, "--expect-snapshot", original)
        current = first_refresh["data"]["snapshot"]["id"]
        self.assertNotEqual(current, original)

        (self.repo / "README.md").write_text("target moved twice\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "move target twice"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        stale = self.tri("targets", "refresh", "app", target, "--expect-snapshot", original, ok=False)
        self.assertEqual(stale["diagnostics"][0]["code"], "stale_target_snapshot")
        self.assertEqual(stale["diagnostics"][0]["expected"], original)
        self.assertEqual(stale["diagnostics"][0]["actual"], current)
        self.assertEqual(self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"], current)

    def test_changeset_merge_reports_conflict_when_target_touched_same_path(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "changeset edit\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'changeset edit\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Conflicting merge")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "approve", review)
        previous_target_snapshot = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]

        (self.repo / "src" / "app.txt").write_text("target edit\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "conflict target"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        refreshed = self.tri("targets", "refresh", "app", target)
        self.assertNotEqual(refreshed["data"]["snapshot"]["id"], previous_target_snapshot)
        refreshed_tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        self.assertEqual(refreshed["data"]["source"]["git_tree"], refreshed_tree)
        self.assertEqual(refreshed["data"]["source"]["git_author"]["actor"], "human:tri_example_com")
        self.assertEqual(refreshed["data"]["source"]["tri_snapshot"], refreshed["data"]["snapshot"]["id"])

        blocked = self.tri("changeset", "merge", changeset, "--target", target, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "conflict_required")
        self.assertEqual(blocked["data"]["status"], "conflict_required")
        self.assertEqual(blocked["data"]["conflicts"][0]["path"], "src/app.txt")
        self.assertEqual(self.tri("changeset", "inspect", changeset)["data"]["status"], "approved")

    def test_changeset_stack_blocks_child_merge_until_parent_merged(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        parent_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", parent_ws, "README.md", "--text", "parent edit\n")
        self.tri("run", parent_ws, "--", "python3", "-c", "from pathlib import Path; assert Path('README.md').read_text() == 'parent edit\\n'")
        parent = self.tri("changeset", "create", parent_ws, "--title", "Parent change")["data"]["id"]
        parent_review = self.tri("review", "create", parent)["data"]["id"]
        self.tri("review", "approve", parent_review)

        child_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", child_ws, "src/app.txt", "--text", "child edit\n")
        self.tri("run", child_ws, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'child edit\\n'")
        child = self.tri("changeset", "create", child_ws, "--title", "Child change")["data"]["id"]
        child_review = self.tri("review", "create", child)["data"]["id"]
        self.tri("review", "approve", child_review)

        stacked = self.tri("changeset", "stack", child, "--after", parent)
        self.assertEqual(stacked["data"]["stack"]["parent"], parent)
        self.assertEqual(stacked["data"]["stack"]["dependency_state"], "pending_parent")

        blocked_policy = self.tri("policy", "evaluate", "export", child, ok=False)
        self.assertEqual(blocked_policy["diagnostics"][0]["code"], "unsatisfied_changeset_dependency")
        self.assertEqual(blocked_policy["data"]["stack"]["parent"], parent)

        blocked_merge = self.tri("changeset", "merge", child, "--target", target, ok=False)
        self.assertEqual(blocked_merge["diagnostics"][0]["code"], "unsatisfied_changeset_dependency")
        self.assertEqual(blocked_merge["data"]["status"], "policy_blocked")

        self.tri("changeset", "merge", parent, "--target", target)
        satisfied = self.tri("changeset", "inspect", child)
        self.assertEqual(satisfied["data"]["stack"]["dependency_state"], "satisfied")

        merged_child = self.tri("changeset", "merge", child, "--target", target)
        self.assertEqual(merged_child["data"]["status"], "merged")
        self.assertEqual(merged_child["data"]["mode"], "merge")
        next_workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.assertEqual(self.tri("files", "read", next_workspace, "README.md")["data"]["text"], "parent edit\n")
        self.assertEqual(self.tri("files", "read", next_workspace, "src/app.txt")["data"]["text"], "child edit\n")

    def test_changeset_stack_redacts_private_parent_from_public_child(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        parent_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", parent_ws, "README.md", "--text", "private parent\n")
        parent = self.tri("changeset", "create", parent_ws, "--title", "Private parent", "--visibility", "private")["data"]["id"]
        parent_head = self.tri("changeset", "inspect", parent)["data"]["head_snapshot"]

        child_ws = self.tri("workspace", "create", "app", "--from", target, "--visibility", "public")["data"]["id"]
        self.tri("files", "write", child_ws, "src/app.txt", "--text", "public child\n")
        child = self.tri("changeset", "create", child_ws, "--title", "Public child", "--visibility", "public")["data"]["id"]

        stacked = self.tri("changeset", "stack", child, "--after", parent)
        stack_event = stacked["events"][0]

        hidden_child = self.tri("--actor", "agent:b", "changeset", "inspect", child)
        hidden_stack = hidden_child["data"]["stack"]
        self.assertIsNone(hidden_stack["parent"])
        self.assertTrue(hidden_stack["parent_redacted"])
        self.assertEqual(hidden_stack["dependency_state"], "redacted_parent")
        self.assertIsNone(hidden_stack["parent_status"])
        self.assertIsNone(hidden_stack["parent_head_snapshot"])
        self.assertNotIn(parent, json.dumps(hidden_child))
        self.assertNotIn(parent, hidden_child["refs"].get("changesets", []))

        hidden_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "export", child, ok=False)
        self.assertTrue(hidden_policy["data"]["stack"]["parent_redacted"])
        self.assertIsNone(hidden_policy["data"]["stack"]["parent"])
        self.assertNotIn(parent, json.dumps(hidden_policy))
        self.assertNotIn(parent_head, json.dumps(hidden_policy))

        hidden_event = self.tri("--actor", "agent:b", "events", "inspect", stack_event)
        self.assertEqual(hidden_event["data"]["object_refs"]["changeset"], child)
        self.assertTrue(hidden_event["data"]["object_refs"]["parent"]["redacted"])
        self.assertTrue(hidden_event["data"]["input_fingerprints"]["parent_head_snapshot"]["redacted"])
        self.assertNotIn(parent, json.dumps(hidden_event))
        self.assertNotIn(parent_head, json.dumps(hidden_event))
        self.assertNotIn(parent, hidden_event["refs"].get("changesets", []))

        blocked_export = self.tri("export", "git-patch", child, ok=False)
        hidden_export = self.tri("--actor", "agent:b", "export", "inspect", blocked_export["data"]["export"])
        self.assertTrue(hidden_export["data"]["policy_decision"]["stack"]["parent_redacted"])
        self.assertNotIn(parent, json.dumps(hidden_export))
        self.assertNotIn(parent_head, json.dumps(hidden_export))
        self.assertNotIn(parent, hidden_export["refs"].get("changesets", []))

        self.tri("policy", "grant", parent, "--principal", "actor:agent:b", "--action", "changeset.read")
        visible_child = self.tri("--actor", "agent:b", "changeset", "inspect", child)
        self.assertEqual(visible_child["data"]["stack"]["parent"], parent)
        self.assertFalse(visible_child["data"]["stack"]["parent_redacted"])
        visible_event = self.tri("--actor", "agent:b", "events", "inspect", stack_event)
        self.assertEqual(visible_event["data"]["object_refs"]["parent"], parent)
        self.assertEqual(visible_event["data"]["input_fingerprints"]["parent_head_snapshot"], parent_head)

    def test_changeset_discard_and_supersede_keep_audit_state(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        discard_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", discard_ws, "README.md", "--text", "discard me\n")
        discarded_id = self.tri("changeset", "create", discard_ws, "--title", "Discard me")["data"]["id"]
        discarded = self.tri("changeset", "discard", discarded_id, "--reason", "Wrong direction")["data"]
        self.assertEqual(discarded["previous_status"], "ready")
        self.assertEqual(discarded["status"], "discarded")
        self.assertEqual(discarded["transition"], "ready->discarded")
        self.assertEqual(discarded["lifecycle"]["reason"], "Wrong direction")
        self.assertEqual(self.tri("changeset", "inspect", discarded_id)["data"]["status"], "discarded")
        blocked_export = self.tri("export", "git-patch", discarded_id, ok=False)
        self.assertEqual(blocked_export["diagnostics"][0]["code"], "changeset_lifecycle_blocked")
        self.assertEqual(blocked_export["data"]["record"]["policy_decision"]["changeset_lifecycle"]["status"], "discarded")
        blocked_review = self.tri("review", "create", discarded_id, ok=False)
        self.assertEqual(blocked_review["diagnostics"][0]["code"], "changeset_lifecycle_blocked")

        replacement_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", replacement_ws, "src/app.txt", "--text", "replacement\n")
        replacement_id = self.tri("changeset", "create", replacement_ws, "--title", "Replacement")["data"]["id"]

        old_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", old_ws, "src/app.txt", "--text", "old attempt\n")
        old_id = self.tri("changeset", "create", old_ws, "--title", "Old attempt")["data"]["id"]
        old_review = self.tri("review", "create", old_id)["data"]["id"]
        superseded = self.tri("changeset", "supersede", old_id, "--by", replacement_id, "--reason", "Replacement is smaller")["data"]
        self.assertEqual(superseded["previous_status"], "reviewing")
        self.assertEqual(superseded["status"], "superseded")
        self.assertEqual(superseded["transition"], "reviewing->superseded")
        self.assertEqual(superseded["replacement_changeset"], replacement_id)
        self.assertEqual(superseded["lifecycle"]["superseded_by"], replacement_id)
        self.assertEqual(self.tri("review", "inspect", old_review)["data"]["status"], "closed")
        blocked_merge = self.tri("changeset", "merge", old_id, "--target", target, ok=False)
        self.assertEqual(blocked_merge["diagnostics"][0]["code"], "changeset_lifecycle_blocked")

        reviewing_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", reviewing_ws, "README.md", "--text", "reviewing\n")
        reviewing_id = self.tri("changeset", "create", reviewing_ws, "--title", "Reviewing")["data"]["id"]
        self.tri("review", "create", reviewing_id)
        blocked_discard = self.tri("changeset", "discard", reviewing_id, ok=False)
        self.assertEqual(blocked_discard["diagnostics"][0]["code"], "changeset_lifecycle_blocked")

    def test_changeset_revalidate_refreshes_stale_check_at_head(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        initial_check = self.tri(
            "run",
            workspace,
            "--",
            "python3",
            "-c",
            "from pathlib import Path; assert Path('src/app.txt').exists(); print('ok')",
        )["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "revalidated\n")
        changeset = self.tri("changeset", "create", workspace, "--title", "Needs revalidation")["data"]
        self.assertEqual(changeset["checks"], [initial_check])
        self.assertFalse(changeset["stack"]["revalidation_checks"][0]["current_for_head"])

        review_response = self.tri("review", "create", changeset["id"])
        review = review_response["data"]["id"]
        self.assertEqual(review_response["data"]["status"], "waiting_for_checks")
        self.assertEqual(self.tri("review", "inspect", review)["data"]["status"], "waiting_for_checks")
        stale = self.tri("review", "approve", review, ok=False)
        self.assertEqual(stale["diagnostics"][0]["code"], "missing_required_check")

        revalidation_response = self.tri("changeset", "revalidate", changeset["id"])
        self.assertEqual(
            [self.tri("events", "inspect", event)["data"]["kind"] for event in revalidation_response["events"]],
            ["check.revalidation_started", "check.revalidated", "changeset.revalidated"],
        )
        revalidated = revalidation_response["data"]
        new_check = revalidated["checks"][0]
        self.assertEqual(new_check["changeset"], changeset["id"])
        self.assertEqual(new_check["rerun_of"], initial_check)
        self.assertEqual(new_check["snapshot_after"], changeset["head_snapshot"])
        self.assertEqual(new_check["materialized_directory"]["mode"], "ephemeral_changeset_revalidation")
        self.assertEqual(new_check["materialized_directory"]["input_snapshot"], changeset["head_snapshot"])
        self.assertEqual(new_check["materialized_directory"]["output_snapshot"], changeset["head_snapshot"])
        self.assertFalse(new_check["materialized_directory"]["mutated"])
        self.assertEqual(new_check["environment_policy"]["secret_leases"], [])
        self.assertEqual(new_check["status"], "passed")
        self.assertTrue(any(item["id"] == new_check["id"] and item["current_for_head"] for item in revalidated["stack"]["revalidation_checks"]))
        listed_checks = self.tri("checks", "list", changeset["id"])
        self.assert_schema_required_keys("tri.check.list", listed_checks["data"])
        self.assertEqual(listed_checks["data"]["object_type"], "changeset")
        self.assertEqual(listed_checks["data"]["changeset"], changeset["id"])
        self.assertEqual([item["id"] for item in listed_checks["data"]["checks"]], [initial_check, new_check["id"]])
        refreshed_summary = self.tri("review", "attach-check-summary", review)
        self.assertEqual(refreshed_summary["data"]["status"], "open")
        self.assertTrue(refreshed_summary["data"]["check_summary"]["required_satisfied"])

        streamed = self.tri("checks", "stream", new_check["id"], "--log", "stdout")["data"]
        self.assertEqual(streamed["text"], "ok\n")
        self.tri("review", "approve", review)
        self.assertTrue(self.tri("policy", "evaluate", "export", changeset["id"])["data"]["allowed"])

    def test_secret_like_path_blocks_export(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", workspace, ".env", "--text", "TOKEN=secret\n")
        changeset_id = self.tri("changeset", "create", workspace, "--title", "Add env")["data"]["id"]

        export = self.tri("export", "git-patch", changeset_id, ok=False)
        self.assertFalse(export["ok"])
        self.assertEqual(export["diagnostics"][0]["code"], "secret_export_blocked")

    def test_non_exportable_config_artifact_blocks_changeset_export(self) -> None:
        imported = self.import_project()
        config_only_workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        config_only = self.tri("config", "materialize", config_only_workspace, "--profile", "ci")["data"]
        self.assertIsNone(config_only["lease"])
        self.assertIsNone(config_only["materialized_path"])
        config_check = self.tri(
            "run",
            config_only_workspace,
            "--",
            "python3",
            "-c",
            "from pathlib import Path; p=Path('.tri/generated-config/ci.env'); assert p.read_text().startswith('TRI_PROFILE=ci'); p.write_text('mutated config\\n')",
        )["data"]
        self.assertEqual(config_check["status"], "passed")
        self.assertEqual(config_check["environment_policy"]["generated_config_paths"], [config_only["path"]])
        self.assertEqual(config_check["environment_policy"]["generated_configs"][0]["artifact"], config_only["artifact"])
        self.assertFalse(config_check["materialized_directory"]["mutated"])
        config_only_files = {item["path"] for item in self.tri("files", "list", config_only_workspace)["data"]["files"]}
        self.assertNotIn(config_only["path"], config_only_files)

        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        mount = self.root / "mounted"
        mounted = self.tri("workspace", "mount", workspace, str(mount))["data"]
        config = self.tri("config", "materialize", workspace, "--profile", "test")
        artifact_id = config["data"]["artifact"]
        config_path = config["data"]["path"]
        self.assertEqual(config["data"]["lease"], mounted["lease"])
        self.assertEqual(config["data"]["materialized_path"], str((mount / config_path).resolve()))
        self.assertEqual((mount / config_path).read_text(), f"TRI_PROFILE=test\nTRI_WORKSPACE={workspace}\n")
        leases = self.tri("workspace", "leases", workspace)["data"]["leases"]
        self.assert_schema_required_keys("tri.workspace.materialization_lease", leases[0])
        self.assertIn(artifact_id, leases[0]["generated_artifacts"])
        self.assertIn(config_path, leases[0]["ignored_paths"])
        sync = self.tri("workspace", "sync", workspace)
        self.assertEqual(sync["data"]["paths"], [])
        files = {item["path"] for item in self.tri("files", "list", workspace)["data"]["files"]}
        self.assertNotIn(config_path, files)

        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        blocked_reader_config = self.tri("--actor", "agent:b", "config", "materialize", workspace, "--profile", "reader", ok=False)
        self.assertEqual(blocked_reader_config["diagnostics"][0]["code"], "policy_denied")
        self.assertFalse((mount / ".tri" / "generated-config" / "reader.env").exists())
        escaped_profile = self.tri("config", "materialize", workspace, "--profile", "../prod", ok=False)
        self.assertEqual(escaped_profile["diagnostics"][0]["code"], "invalid_path")

        self.tri("files", "write", workspace, "src/app.txt", "--text", "config guarded edit\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'config guarded edit\\n'")
        changeset_id = self.tri("changeset", "create", workspace, "--title", "Config guarded")["data"]["id"]
        review_id = self.tri("review", "create", changeset_id)["data"]["id"]
        self.tri("review", "approve", review_id)

        decision = self.tri("policy", "evaluate", "export", changeset_id, ok=False)
        self.assertIn("non_exportable_artifact", decision["data"]["denials"])
        self.assertEqual(decision["data"]["non_exportable_artifacts"][0]["id"], artifact_id)

        export = self.tri("export", "git-patch", changeset_id, "--override-reason", "local test", ok=False)
        self.assertEqual(export["diagnostics"][0]["code"], "non_exportable_artifact")
        self.assertEqual(export["diagnostics"][0]["object"], artifact_id)

    def test_export_policy_requires_checks_and_approved_review_or_override(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "policy edit\n")
        changeset_id = self.tri("changeset", "create", workspace, "--title", "Policy edit")["data"]["id"]
        review_id = self.tri("review", "create", changeset_id)["data"]["id"]

        approval = self.tri("review", "approve", review_id, ok=False)
        self.assertEqual(approval["diagnostics"][0]["code"], "missing_required_check")

        decision = self.tri("policy", "evaluate", "export", changeset_id, ok=False)
        self.assertFalse(decision["data"]["allowed"])
        self.assertEqual(decision["data"]["event"], decision["events"][0])
        self.assertIn(decision["data"]["event"], decision["refs"]["events"])
        self.assertIn("policy_required_checks_passed", decision["data"]["policies"])
        self.assertIn("policy_review_approved", decision["data"]["policies"])
        self.assertFalse(set(decision["data"]["policies"]).intersection(decision["refs"].get("policies", [])))
        self.assertEqual(decision["data"]["redactions"], [])
        self.assertEqual(decision["data"]["denials"], ["missing_required_check", "missing_review_approval"])

        blocked = self.tri("export", "git-patch", changeset_id, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "missing_required_check")
        self.assertEqual(blocked["data"]["changeset"], changeset_id)

        override = self.tri("export", "git-patch", changeset_id, "--override-reason", "local smoke export")
        self.assertTrue(override["ok"])
        self.assertEqual(override["data"]["changeset"], changeset_id)

        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'policy edit\\n'")
        changeset_id = self.tri("changeset", "create", workspace, "--title", "Policy edit checked")["data"]["id"]
        review_id = self.tri("review", "create", changeset_id)["data"]["id"]
        self.tri("review", "approve", review_id)
        allowed = self.tri("policy", "evaluate", "export", changeset_id)
        self.assertTrue(allowed["data"]["allowed"])
        self.assertEqual(allowed["data"]["reason_code"], "allowed")
        self.assertEqual(allowed["data"]["event"], allowed["events"][0])

    def test_advisory_embargo_blocks_export_until_release_disclosure(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        command_names = {item["name"] for item in self.tri("commands")["data"]["commands"]}
        self.assertIn("policy-create-embargo", command_names)
        self.assertIn("policy-disclose-embargo", command_names)
        advisory = self.tri(
            "policy",
            "create-embargo",
            "app",
            "--title",
            "Auth bypass",
            "--affected-package",
            "app",
            "--trusted-reviewer",
            "human:security",
        )["data"]
        self.assert_schema_required_keys("tri.policy.embargo", advisory)
        self.assertEqual(advisory["status"], "reported")
        self.assertEqual(advisory["disclosure_state"], "embargoed")
        self.assertEqual(advisory["affected_packages"], ["app"])
        self.assertEqual(advisory["trusted_reviewers"], ["actor:human:security"])

        blocked_workspace = self.tri("--actor", "agent:b", "workspace", "create", "app", "--from", target, "--advisory", advisory["id"], ok=False)
        self.assertEqual(blocked_workspace["diagnostics"][0]["code"], "policy_denied")

        trusted_workspace = self.tri("--actor", "human:security", "workspace", "create", "app", "--from", target, "--advisory", advisory["id"])["data"]
        self.assertEqual(trusted_workspace["advisory"], advisory["id"])
        trusted_workspace_inspect = self.tri("--actor", "human:security", "workspace", "inspect", trusted_workspace["id"])["data"]
        self.assertEqual(trusted_workspace_inspect["owner"], "human:security")

        workspace = self.tri("workspace", "create", "app", "--from", target, "--advisory", advisory["id"])["data"]
        self.assertEqual(workspace["advisory"], advisory["id"])
        self.assertEqual(workspace["visibility"], ["embargoed"])

        self.tri("files", "write", workspace["id"], "src/app.txt", "--text", "security fix\n")
        self.tri(
            "run",
            workspace["id"],
            "--",
            "python3",
            "-c",
            "from pathlib import Path; assert Path('src/app.txt').read_text() == 'security fix\\n'",
        )
        changeset = self.tri("changeset", "create", workspace["id"], "--visibility", "embargoed")["data"]
        self.assertEqual(changeset["title"], "Auth bypass")
        self.assertEqual(changeset["advisory"], advisory["id"])
        self.assertEqual(changeset["visibility"], ["embargoed"])

        advisory_after_changeset = self.tri("advisory", "inspect", advisory["id"])["data"]
        self.assert_schema_required_keys("tri.advisory.inspect", advisory_after_changeset)
        self.assertEqual(advisory_after_changeset["private_changesets"], [changeset["id"]])

        review = self.tri("review", "create", changeset["id"], "--reviewers", "human:security")["data"]["id"]
        self.tri("review", "approve", review)

        blocked_policy = self.tri("policy", "evaluate", "export", changeset["id"], ok=False)
        self.assertEqual(blocked_policy["diagnostics"][0]["code"], "advisory_embargo_active")
        self.assertEqual(blocked_policy["data"]["advisory"]["id"], advisory["id"])

        blocked_export = self.tri("export", "git-patch", changeset["id"], ok=False)
        self.assertEqual(blocked_export["diagnostics"][0]["code"], "advisory_embargo_active")

        blocked_release = self.tri("release", "prepare", "app", "--include", changeset["id"], ok=False)
        self.assert_schema_required_keys("tri.release.inspect", blocked_release["data"])
        self.assertEqual(blocked_release["diagnostics"][0]["code"], "advisory_embargo_active")
        self.assertEqual(blocked_release["data"]["status"], "policy_blocked")
        self.assertEqual(blocked_release["data"]["checks"], [changeset["checks"][0]])
        self.assertEqual(blocked_release["data"]["redacted"]["included_changesets"], 0)

        release = self.tri("policy", "disclose-embargo", "app", "--include", changeset["id"], "--advisory", advisory["id"], "--package", "app")["data"]
        self.assert_schema_required_keys("tri.policy.embargo_disclosure", release)
        self.assertEqual(release["status"], "prepared")
        self.assertEqual(release["included_changesets"], [changeset["id"]])
        self.assertEqual(release["advisories_disclosed"], [advisory["id"]])
        self.assertEqual(release["packages"], ["app"])
        self.assertEqual(release["policy_decisions"][0]["advisory"]["disclosed_for_action"], True)
        release_inspected = self.tri("release", "inspect", release["id"])["data"]
        self.assert_schema_required_keys("tri.release.inspect", release_inspected)
        self.assertEqual(release_inspected["policy_decisions"][0]["advisory"]["disclosed_for_action"], True)

        disclosed = self.tri("advisory", "inspect", advisory["id"])["data"]
        self.assert_schema_required_keys("tri.advisory.inspect", disclosed)
        self.assertEqual(disclosed["status"], "disclosed")
        self.assertEqual(disclosed["disclosure_state"], "disclosed")
        public_disclosed = self.tri("--actor", "agent:b", "advisory", "inspect", advisory["id"])
        self.assert_schema_required_keys("tri.advisory.inspect", public_disclosed["data"])
        self.assertEqual(public_disclosed["data"]["status"], "disclosed")
        self.assertEqual(public_disclosed["data"]["private_changesets"], [])
        self.assertEqual(public_disclosed["data"]["redacted"]["private_changesets"], 1)
        self.assertNotIn(changeset["id"], json.dumps(public_disclosed))
        self.assertNotIn(changeset["id"], public_disclosed["refs"].get("changesets", []))
        public_list = self.tri("--actor", "agent:b", "advisory", "list", "app")
        self.assertEqual(public_list["data"]["redacted"], 0)
        self.assert_schema_required_keys("tri.advisory.inspect", public_list["data"]["advisories"][0])
        self.assertEqual(public_list["data"]["advisories"][0]["private_changesets"], [])
        self.tri("policy", "grant", workspace["id"], "--principal", "actor:agent:b", "--action", "workspace.read")
        visible_disclosed = self.tri("--actor", "agent:b", "advisory", "inspect", advisory["id"])
        self.assertEqual(visible_disclosed["data"]["private_changesets"], [changeset["id"]])
        self.assertEqual(visible_disclosed["data"]["redacted"]["private_changesets"], 0)
        self.assertTrue(self.tri("policy", "evaluate", "export", changeset["id"])["data"]["allowed"])
        self.assertTrue(self.tri("export", "git-patch", changeset["id"])["ok"])

    def test_release_records_redact_private_nested_refs(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", workspace, "src/app.txt", "--text", "private release work\n")
        check = self.tri(
            "--actor",
            "agent:a",
            "run",
            workspace,
            "--",
            "python3",
            "-c",
            "print('private release evidence')",
        )["data"]
        changeset = self.tri("--actor", "agent:a", "changeset", "create", workspace, "--title", "Private release")["data"]["id"]
        review = self.tri("--actor", "agent:a", "review", "create", changeset)["data"]["id"]
        self.tri("--actor", "agent:a", "review", "approve", review)
        release = self.tri("release", "prepare", "app", "--include", changeset, "--package", "app")["data"]
        self.assert_schema_required_keys("tri.release.inspect", release)
        release_id = release["id"]
        self.assertEqual(release["policy_decisions"][0]["action"], "release.prepare")

        hidden = self.tri("--actor", "agent:b", "release", "inspect", release_id)
        self.assert_schema_required_keys("tri.release.inspect", hidden["data"])
        self.assertEqual(hidden["data"]["included_changesets"], [])
        self.assertEqual(hidden["data"]["checks"], [])
        self.assertEqual(hidden["data"]["artifacts"], [])
        self.assertIsNone(hidden["data"]["target_snapshot"])
        self.assertTrue(hidden["data"]["target_snapshot_redacted"])
        self.assertEqual(hidden["data"]["redacted"]["included_changesets"], 1)
        self.assertEqual(hidden["data"]["redacted"]["checks"], 1)
        self.assertEqual(hidden["data"]["redacted"]["artifacts"], 2)
        self.assertGreater(hidden["data"]["redacted"]["policy_decisions"], 0)
        self.assertNotIn(changeset, json.dumps(hidden))
        self.assertNotIn(check["id"], json.dumps(hidden))
        self.assertNotIn(check["logs"]["stdout"], json.dumps(hidden))

        hidden_list = self.tri("--actor", "agent:b", "release", "list", "app")
        self.assertEqual(hidden_list["data"]["redacted"], 0)
        self.assert_schema_required_keys("tri.release.inspect", hidden_list["data"]["releases"][0])
        self.assertEqual(hidden_list["data"]["releases"][0]["included_changesets"], [])
        hidden_search = self.tri("--actor", "agent:b", "search", "app", changeset)
        self.assertEqual(hidden_search["data"]["matches"], [])
        self.assertNotIn(changeset, hidden_search["refs"].get("changesets", []))

        blocked_artifact = self.tri("--actor", "agent:b", "artifacts", "read", check["logs"]["stdout"], ok=False)
        self.assertEqual(blocked_artifact["diagnostics"][0]["code"], "policy_denied")

        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        visible = self.tri("--actor", "agent:b", "release", "inspect", release_id)
        self.assert_schema_required_keys("tri.release.inspect", visible["data"])
        self.assertEqual(visible["data"]["included_changesets"], [changeset])
        self.assertEqual(visible["data"]["checks"], [check["id"]])
        self.assertIn(check["logs"]["stdout"], visible["data"]["artifacts"])
        self.assertFalse(visible["data"]["target_snapshot_redacted"])
        self.assertEqual(visible["data"]["redacted"]["policy_decisions"], 0)
        self.assertEqual(visible["data"]["policy_decisions"][0]["object"], changeset)
        allowed_artifact = self.tri("--actor", "agent:b", "artifacts", "read", check["logs"]["stdout"])
        self.assertIn("private release evidence", allowed_artifact["data"]["text"])

    def test_policy_grants_control_private_workspace_and_changeset_access(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target)["data"]["id"]

        blocked_read = self.tri("--actor", "agent:b", "files", "read", workspace, "src/app.txt", ok=False)
        self.assertEqual(blocked_read["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn("policy_denied", blocked_read["refs"].get("policies", []))

        listed = self.tri("--actor", "agent:b", "workspace", "list", "app")
        self.assertEqual(listed["data"]["workspaces"], [])
        self.assertEqual(listed["data"]["redacted"], 1)

        workspace_grant = self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")["data"]
        self.assertEqual(workspace_grant["principal"], "actor:agent:b")
        self.assertEqual(workspace_grant["actions"], ["workspace.read"])
        inspected_workspace_grant = self.tri("--actor", "agent:b", "policy", "grant-inspect", workspace_grant["id"])
        self.assert_schema_required_keys("tri.policy.grant", inspected_workspace_grant["data"])
        self.assertEqual(inspected_workspace_grant["data"]["id"], workspace_grant["id"])
        self.assertEqual(inspected_workspace_grant["data"]["object"], workspace)
        blocked_grant = self.tri("--actor", "agent:c", "policy", "grant-inspect", workspace_grant["id"], ok=False)
        self.assertEqual(blocked_grant["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_grant["diagnostics"][0]["required_grant"], "grant principal or object read")
        self.assertNotIn(workspace, json.dumps(blocked_grant["diagnostics"]))
        allowed_read = self.tri("--actor", "agent:b", "files", "read", workspace, "src/app.txt")
        self.assertEqual(allowed_read["data"]["text"], "one\n")

        blocked_changeset_create = self.tri("--actor", "agent:b", "changeset", "create", workspace, "--title", "Read-only attempt", ok=False)
        self.assertEqual(blocked_changeset_create["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_changeset_create["diagnostics"][0]["required_grant"], "workspace.write")

        blocked_write = self.tri("--actor", "agent:b", "files", "write", workspace, "src/app.txt", "--text", "agent b\n", ok=False)
        self.assertEqual(blocked_write["diagnostics"][0]["code"], "policy_denied")

        write_grant = self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.write")["data"]
        write = self.tri("--actor", "agent:b", "files", "write", workspace, "src/app.txt", "--text", "agent b\n")
        written_file = self.tri("--actor", "agent:b", "files", "read", workspace, "src/app.txt")["data"]
        written_tree = self.tri("--actor", "agent:b", "snapshots", "inspect", write["data"]["snapshot"])["data"]["root_tree"]
        blocked_blob = self.tri("--actor", "agent:c", "blobs", "inspect", written_file["blob"], ok=False)
        self.assertEqual(blocked_blob["diagnostics"][0]["code"], "policy_denied")
        blocked_object_blob = self.tri("--actor", "agent:c", "objects", "inspect", written_file["blob"], ok=False)
        self.assertEqual(blocked_object_blob["data"]["resolved_schema"], "tri.error")
        self.assertEqual(blocked_object_blob["diagnostics"][0]["code"], "policy_denied")
        blocked_tree = self.tri("--actor", "agent:c", "trees", "inspect", written_tree, ok=False)
        self.assertEqual(blocked_tree["diagnostics"][0]["code"], "policy_denied")
        allowed_blob = self.tri("--actor", "agent:b", "blobs", "inspect", written_file["blob"])
        self.assertEqual(allowed_blob["data"]["origins"][0]["path"], "src/app.txt")
        allowed_object_blob = self.tri("--actor", "agent:b", "objects", "inspect", written_file["blob"])
        self.assertEqual(allowed_object_blob["data"]["resolved_schema"], "tri.blob.inspect")
        allowed_tree = self.tri("--actor", "agent:b", "trees", "inspect", written_tree)
        self.assertEqual(allowed_tree["data"]["id"], written_tree)
        event_kinds = {event: self.tri("--actor", "agent:b", "events", "inspect", event)["data"]["kind"] for event in write["events"]}
        policy_event = next(event for event, kind in event_kinds.items() if kind == "policy.evaluated")
        write_event = next(event for event, kind in event_kinds.items() if kind == "workspace.write_file")
        write_event_data = self.tri("--actor", "agent:b", "events", "inspect", write_event)["data"]
        self.assertEqual(write_event_data["policy_result"]["action"], "workspace.write")
        self.assertEqual(write_event_data["policy_result"]["object"], workspace)
        self.assertEqual(write_event_data["policy_result"]["reason"], "explicit_grant")
        self.assertEqual(write_event_data["policy_result"]["event"], policy_event)
        self.assertEqual(write_event_data["policy_result"]["matched_grants"][0]["id"], write_grant["id"])
        explained_write_policy = self.tri("--actor", "agent:b", "policy", "explain", write_event_data["policy_result"]["id"])
        self.assertEqual(explained_write_policy["data"]["event"]["id"], policy_event)

        changeset_data = self.tri("--actor", "agent:a", "changeset", "create", workspace, "--title", "Private change")["data"]
        changeset = changeset_data["id"]
        operation_policy = changeset_data["operation_details"][0]["policy"]
        self.assertEqual(operation_policy["action"], "workspace.write")
        self.assertEqual(operation_policy["object"], workspace)
        self.assertEqual(operation_policy["reason"], "explicit_grant")
        self.assertEqual(operation_policy["matched_grants"][0]["id"], write_grant["id"])

        inherited = self.tri("--actor", "agent:b", "policy", "evaluate", "access", changeset, "--action", "changeset.read")
        self.assertTrue(inherited["data"]["allowed"])
        self.assertEqual(inherited["data"]["reason"], "inherited_grant")
        self.assertEqual(inherited["data"]["event"], inherited["events"][0])
        self.assertEqual(inherited["data"]["policies"], ["local_policy_v0"])
        self.assertEqual(inherited["data"]["redactions"], [])
        self.assertEqual(inherited["data"]["matched_grants"][0]["inherited_from"], workspace)
        allowed_inherited_changeset = self.tri("--actor", "agent:b", "changeset", "inspect", changeset)
        self.assertEqual(allowed_inherited_changeset["data"]["id"], changeset)

        visibility = self.tri("policy", "visibility", changeset)["data"]
        self.assertEqual(visibility["parents"]["workspace"], workspace)
        self.assertEqual(visibility["inherited_grants"][0]["object"], workspace)
        agent_b_visibility = self.tri("--actor", "agent:b", "policy", "visibility", changeset)["data"]
        self.assertEqual(agent_b_visibility["parents"]["workspace"], workspace)

        blocked_changeset = self.tri("--actor", "agent:c", "changeset", "inspect", changeset, ok=False)
        self.assertEqual(blocked_changeset["diagnostics"][0]["code"], "policy_denied")
        blocked_decision = blocked_changeset["diagnostics"][0]["decision"]
        blocked_policy_event = blocked_changeset["diagnostics"][0]["event"]
        self.assertIn(blocked_decision, blocked_changeset["refs"].get("decisions", []))
        self.assertIn(blocked_policy_event, blocked_changeset["refs"].get("events", []))
        explained_blocked = self.tri("--actor", "agent:c", "policy", "explain", blocked_decision)
        self.assertEqual(explained_blocked["data"]["id"], blocked_decision)
        self.assertEqual(explained_blocked["data"]["decision"]["event"], blocked_policy_event)
        self.assertEqual(explained_blocked["data"]["event"]["kind"], "policy.denied")
        self.assertEqual(explained_blocked["data"]["decision"]["reason_code"], "policy_denied")
        self.assertEqual(explained_blocked["data"]["event"]["id"], blocked_policy_event)
        audit_after_denial = self.tri("changeset", "inspect", changeset)
        self.assertIn(blocked_decision, audit_after_denial["data"]["policy_decisions"])
        blocked_visibility = self.tri("--actor", "agent:c", "policy", "visibility", changeset, ok=False)
        self.assertEqual(blocked_visibility["diagnostics"][0]["code"], "policy_denied")
        denied = self.tri("--actor", "agent:c", "policy", "evaluate", "access", changeset, "--action", "changeset.read", ok=False)
        self.assertEqual(denied["data"]["reason_code"], "policy_denied")
        self.assertEqual(denied["data"]["event"], denied["events"][0])
        explained = self.tri("--actor", "agent:c", "policy", "explain", denied["data"]["id"])
        self.assertEqual(explained["data"]["id"], denied["data"]["id"])
        self.assertEqual(explained["data"]["action"], "changeset.read")
        self.assertEqual(explained["data"]["object"], changeset)
        self.assertEqual(explained["data"]["decision"]["reason_code"], "policy_denied")
        self.assertEqual(explained["data"]["decision"]["event"], denied["data"]["event"])
        self.assertEqual(explained["data"]["event"]["id"], denied["data"]["event"])
        self.assertEqual(explained["data"]["event"]["kind"], "policy.evaluated")
        blocked_explain = self.tri("--actor", "agent:d", "policy", "explain", denied["data"]["id"], ok=False)
        self.assertEqual(blocked_explain["diagnostics"][0]["code"], "policy_denied")

        changeset_grant = self.tri("policy", "grant", changeset, "--principal", "actor:agent:c", "--action", "changeset.read")["data"]
        self.assertEqual(changeset_grant["object"], changeset)
        self.assertEqual(self.tri("--actor", "agent:c", "policy", "grant-inspect", changeset_grant["id"])["data"]["object"], changeset)
        self.assertEqual(self.tri("--actor", "agent:c", "blobs", "inspect", written_file["blob"])["data"]["id"], written_file["blob"])
        self.assertEqual(self.tri("--actor", "agent:c", "trees", "inspect", written_tree)["data"]["id"], written_tree)
        allowed_changeset = self.tri("--actor", "agent:c", "changeset", "inspect", changeset)
        self.assertEqual(allowed_changeset["data"]["id"], changeset)
        allowed_visibility = self.tri("--actor", "agent:c", "policy", "visibility", changeset)
        self.assertEqual(allowed_visibility["data"]["object"], changeset)
        blocked_review_create = self.tri("--actor", "agent:c", "review", "create", changeset, ok=False)
        self.assertEqual(blocked_review_create["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_review_create["diagnostics"][0]["required_grant"], "changeset.write")
        review = self.tri("review", "create", changeset)["data"]["id"]
        inherited_review = self.tri("--actor", "agent:b", "policy", "evaluate", "access", review, "--action", "review.read")
        self.assertTrue(inherited_review["data"]["allowed"])
        self.assertEqual(inherited_review["data"]["reason"], "inherited_grant")
        self.assertEqual(inherited_review["data"]["matched_grants"][0]["inherited_from"], workspace)
        review_visibility = self.tri("--actor", "agent:b", "policy", "visibility", review)["data"]
        self.assertEqual(review_visibility["parents"]["changeset"], changeset)
        self.assertEqual(review_visibility["parents"]["workspace"], workspace)
        self.assertEqual(review_visibility["inherited_grants"][0]["object"], workspace)
        inherited_review_inspect = self.tri("--actor", "agent:b", "review", "inspect", review)
        self.assertEqual(inherited_review_inspect["data"]["id"], review)
        self.tri("review", "attach-policy-finding", review, "--body", "review-only search note")

        blocked_review_read = self.tri("--actor", "agent:d", "review", "inspect", review, ok=False)
        self.assertEqual(blocked_review_read["diagnostics"][0]["code"], "policy_denied")
        review_grant = self.tri("policy", "grant", review, "--principal", "actor:agent:d", "--action", "review.read")["data"]
        self.assertEqual(review_grant["object"], review)
        direct_review_read = self.tri("--actor", "agent:d", "review", "inspect", review)
        self.assertEqual(direct_review_read["data"]["id"], review)
        self.assertIsNone(direct_review_read["data"]["changeset"])
        self.assertTrue(direct_review_read["data"]["redacted_changeset"])
        self.assertTrue(direct_review_read["data"]["changeset_summary"]["redacted"])
        self.assertTrue(direct_review_read["data"]["visibility_impact"]["redacted"])
        self.assertEqual(direct_review_read["data"]["commands"], [])
        self.assertEqual(direct_review_read["data"]["checks"], [])
        self.assertEqual(direct_review_read["data"]["artifacts"], [])
        self.assertEqual(direct_review_read["data"]["attached_policy_findings"][0]["metadata"], {"redacted": True})
        self.assertEqual(direct_review_read["data"]["actions"][0]["metadata"], {"redacted": True})
        redacted_action = self.tri("--actor", "agent:d", "review", "action-inspect", direct_review_read["data"]["actions"][0]["id"])
        self.assert_schema_required_keys("tri.review_action.inspect", redacted_action["data"])
        self.assertEqual(redacted_action["data"]["review"], review)
        self.assertEqual(redacted_action["data"]["metadata"], {"redacted": True})
        self.assertNotIn(changeset, json.dumps(direct_review_read))
        self.assertNotIn(changeset, direct_review_read["refs"].get("changesets", []))
        self.assertNotIn(changeset, json.dumps(redacted_action))
        self.assertNotIn(changeset, redacted_action["refs"].get("changesets", []))
        direct_review_visibility = self.tri("--actor", "agent:d", "policy", "visibility", review)["data"]
        self.assertEqual(direct_review_visibility["parents"]["changeset_redacted"], True)
        self.assertEqual(direct_review_visibility["parents"]["workspace_redacted"], True)
        self.assertEqual(direct_review_visibility["workspace"], None)
        self.assertEqual(direct_review_visibility["inherited_grants"], [])
        self.assertNotIn(changeset, json.dumps(direct_review_visibility))
        review_search = self.tri("--actor", "agent:d", "search", "app", "review-only search note", "--scope", "reviews")
        self.assertEqual(len(review_search["data"]["matches"]), 1)
        self.assertEqual(review_search["data"]["matches"][0]["provenance"]["review"], review)
        self.assertEqual(review_search["data"]["matches"][0]["provenance"]["changeset_redacted"], True)
        self.assertGreaterEqual(review_search["data"]["redacted_skipped"], 1)
        self.assertNotIn(changeset, json.dumps(review_search))
        metadata_search = self.tri("--actor", "agent:d", "search", "app", "missing_review_approval", "--scope", "reviews")
        self.assertEqual(metadata_search["data"]["matches"], [])
        self.assertGreaterEqual(metadata_search["data"]["redacted_skipped"], 1)
        still_blocked_changeset = self.tri("--actor", "agent:d", "changeset", "inspect", changeset, ok=False)
        self.assertEqual(still_blocked_changeset["diagnostics"][0]["code"], "policy_denied")

        blocked_comment = self.tri("--actor", "agent:c", "review", "comment", review, "--body", "read-only comment", ok=False)
        self.assertEqual(blocked_comment["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_comment["diagnostics"][0]["required_grant"], "changeset.write")

        revoked = self.tri("policy", "revoke", changeset_grant["id"])["data"]
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(self.tri("--actor", "agent:c", "policy", "grant-inspect", changeset_grant["id"])["data"]["status"], "revoked")
        blocked_after_revoke = self.tri("--actor", "agent:c", "changeset", "inspect", changeset, ok=False)
        self.assertEqual(blocked_after_revoke["diagnostics"][0]["code"], "policy_denied")

    def test_public_workspace_redacts_private_changesets_from_parent_views(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target, "--visibility", "public")["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "private draft\n")
        workspace_check = self.tri("run", workspace, "--", "python3", "-c", "print('workspace-visible')")["data"]["id"]
        changeset = self.tri("changeset", "create", workspace, "--title", "Private draft", "--visibility", "private")["data"]["id"]
        revalidated = self.tri("changeset", "revalidate", changeset)
        private_check = revalidated["data"]["checks"][0]["id"]

        public_workspace = self.tri("--actor", "agent:b", "workspace", "inspect", workspace)
        self.assertEqual(public_workspace["data"]["id"], workspace)
        self.assertEqual(public_workspace["data"]["changesets"], [])
        self.assertEqual(public_workspace["data"]["redacted_changesets"], 1)
        self.assertEqual(public_workspace["data"]["checks"], [workspace_check])
        self.assertEqual(public_workspace["data"]["redacted_checks"], 1)
        self.assertNotIn(changeset, json.dumps(public_workspace))
        self.assertNotIn(private_check, json.dumps(public_workspace))
        self.assertNotIn(changeset, public_workspace["refs"].get("changesets", []))
        self.assertNotIn(private_check, public_workspace["refs"].get("checks", []))

        public_list = self.tri("--actor", "agent:b", "workspace", "list", "app")
        self.assertEqual(public_list["data"]["workspaces"][0]["id"], workspace)
        self.assertEqual(public_list["data"]["workspaces"][0]["changesets"], [])
        self.assertEqual(public_list["data"]["workspaces"][0]["redacted_changesets"], 1)
        self.assertEqual(public_list["data"]["workspaces"][0]["checks"], [workspace_check])
        self.assertEqual(public_list["data"]["workspaces"][0]["redacted_checks"], 1)
        self.assertNotIn(changeset, json.dumps(public_list))
        self.assertNotIn(private_check, json.dumps(public_list))

        public_checks = self.tri("--actor", "agent:b", "checks", "list", workspace)
        self.assertEqual([check["id"] for check in public_checks["data"]["checks"]], [workspace_check])
        self.assertEqual(public_checks["data"]["redacted"], 1)
        self.assertNotIn(changeset, json.dumps(public_checks))
        self.assertNotIn(private_check, json.dumps(public_checks))
        blocked_check = self.tri("--actor", "agent:b", "checks", "inspect", private_check, ok=False)
        self.assertEqual(blocked_check["diagnostics"][0]["code"], "policy_denied")

        blocked_changeset = self.tri("--actor", "agent:b", "changeset", "inspect", changeset, ok=False)
        self.assertEqual(blocked_changeset["diagnostics"][0]["code"], "policy_denied")

        self.tri("policy", "grant", changeset, "--principal", "actor:agent:b", "--action", "changeset.read")
        visible_workspace = self.tri("--actor", "agent:b", "workspace", "inspect", workspace)
        self.assertEqual(visible_workspace["data"]["changesets"], [changeset])
        self.assertEqual(visible_workspace["data"]["checks"], [workspace_check, private_check])
        self.assertEqual(visible_workspace["data"]["redacted_changesets"], 0)
        self.assertEqual(visible_workspace["data"]["redacted_checks"], 0)

    def test_changeset_readers_can_inspect_attached_check_evidence_without_workspace_access(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target, "--task", "public evidence task")["data"]["id"]
        write = self.tri("--actor", "agent:a", "files", "write", workspace, "src/app.txt", "--text", "review-evidence\n")
        write_event = next(
            event
            for event in write["events"]
            if self.tri("--actor", "agent:a", "events", "inspect", event)["data"]["kind"] == "workspace.write_file"
        )
        run = self.tri("--actor", "agent:a", "run", workspace, "--", "python3", "-c", "print('review-evidence')")
        check = run["data"]
        check_event = next(
            event
            for event in run["events"]
            if self.tri("--actor", "agent:a", "events", "inspect", event)["data"]["kind"] == "check.completed"
        )
        changeset = self.tri("--actor", "agent:a", "changeset", "create", workspace, "--title", "Public evidence", "--visibility", "public")["data"]["id"]

        blocked_workspace = self.tri("--actor", "agent:b", "workspace", "inspect", workspace, ok=False)
        self.assertEqual(blocked_workspace["diagnostics"][0]["code"], "policy_denied")
        inspected_operation = self.tri("--actor", "agent:b", "operations", "inspect", write["data"]["operation"])
        self.assert_schema_required_keys("tri.operation.inspect", inspected_operation["data"])
        self.assertIsNone(inspected_operation["data"]["workspace"])
        self.assertTrue(inspected_operation["data"]["workspace_redacted"])
        self.assertEqual(inspected_operation["data"]["changesets"], [changeset])
        self.assertEqual(inspected_operation["data"]["access"]["reason"], "linked_changeset")
        self.assertTrue(inspected_operation["data"]["policy"]["object_redacted"])
        self.assertNotIn(workspace, json.dumps(inspected_operation))
        self.assertNotIn(workspace, inspected_operation["refs"].get("workspaces", []))
        changeset_operations = self.tri("--actor", "agent:b", "operations", "list", changeset)
        self.assertEqual([item["id"] for item in changeset_operations["data"]["operations"]], [write["data"]["operation"]])
        blocked_operations = self.tri("--actor", "agent:b", "operations", "list", workspace, ok=False)
        self.assertEqual(blocked_operations["diagnostics"][0]["code"], "policy_denied")
        public_changeset = self.tri("--actor", "agent:b", "changeset", "inspect", changeset)
        self.assertEqual(public_changeset["data"]["checks"], [check["id"]])
        self.assertIsNone(public_changeset["data"]["workspace"])
        self.assertTrue(public_changeset["data"]["redacted_workspace"])
        self.assertIsNone(public_changeset["data"]["operation_details"][0]["workspace"])
        self.assertTrue(public_changeset["data"]["operation_details"][0]["workspace_redacted"])
        self.assertTrue(public_changeset["data"]["operation_details"][0]["policy"]["object_redacted"])
        self.assertNotIn(workspace, json.dumps(public_changeset))
        self.assertNotIn(workspace, public_changeset["refs"].get("workspaces", []))

        inspected_check = self.tri("--actor", "agent:b", "checks", "inspect", check["id"])
        self.assertIsNone(inspected_check["data"]["workspace"])
        self.assertTrue(inspected_check["data"]["redacted_workspace"])
        self.assertEqual(inspected_check["data"]["linked_changesets"], [changeset])
        self.assertEqual(inspected_check["data"]["access"]["reason"], "linked_changeset")
        self.assertNotIn(workspace, json.dumps(inspected_check))
        self.assertNotIn(workspace, inspected_check["refs"].get("workspaces", []))
        changeset_checks = self.tri("--actor", "agent:b", "checks", "list", changeset)
        self.assert_schema_required_keys("tri.check.list", changeset_checks["data"])
        self.assertEqual(changeset_checks["data"]["object_type"], "changeset")
        self.assertIsNone(changeset_checks["data"]["workspace"])
        self.assertTrue(changeset_checks["data"]["redacted_workspace"])
        self.assertEqual([item["id"] for item in changeset_checks["data"]["checks"]], [check["id"]])
        self.assertIsNone(changeset_checks["data"]["checks"][0]["workspace"])
        self.assertTrue(changeset_checks["data"]["checks"][0]["redacted_workspace"])
        self.assertNotIn(workspace, json.dumps(changeset_checks))

        public_write_event = self.tri("--actor", "agent:b", "events", "inspect", write_event)
        self.assertIsNone(public_write_event["data"]["workspace"])
        self.assertTrue(public_write_event["data"]["redacted_workspace"])
        self.assertEqual(public_write_event["data"]["object_refs"]["paths"], ["src/app.txt"])
        self.assertNotIn(workspace, json.dumps(public_write_event))
        self.assertNotIn(workspace, public_write_event["refs"].get("workspaces", []))

        public_check_event = self.tri("--actor", "agent:b", "events", "inspect", check_event)
        self.assertIsNone(public_check_event["data"]["workspace"])
        self.assertTrue(public_check_event["data"]["redacted_workspace"])
        self.assertEqual(public_check_event["data"]["object_refs"]["check"], check["id"])
        self.assertNotIn(workspace, json.dumps(public_check_event))
        self.assertNotIn(workspace, public_check_event["refs"].get("workspaces", []))

        streamed = self.tri("--actor", "agent:b", "checks", "stream", check["id"], "--log", "stdout")
        self.assertEqual(streamed["data"]["text"], "review-evidence\n")
        artifacts = self.tri("--actor", "agent:b", "checks", "artifacts", check["id"])
        self.assertEqual({item["id"] for item in artifacts["data"]["artifacts"]}, set(check["artifacts"]))
        stdout_artifact = self.tri("--actor", "agent:b", "artifacts", "read", check["logs"]["stdout"])
        self.assertEqual(stdout_artifact["data"]["text"], "review-evidence\n")
        self.assertIsNone(stdout_artifact["data"]["path"])
        self.assertTrue(stdout_artifact["data"]["storage"]["path_redacted"])
        self.assertEqual({origin["policy_object"] for origin in stdout_artifact["data"]["origins"]}, {changeset})
        self.assertGreaterEqual(stdout_artifact["data"]["access"]["redacted_origins"], 1)
        self.assertNotIn(workspace, json.dumps(stdout_artifact))

        review = self.tri("--actor", "agent:a", "review", "create", changeset)["data"]["id"]
        public_review = self.tri("--actor", "agent:b", "review", "inspect", review)
        self.assertIsNone(public_review["data"]["workspace"])
        self.assertTrue(public_review["data"]["redacted_workspace"])
        self.assertEqual(public_review["data"]["task"]["title"], "public evidence task")
        self.assertIsNone(public_review["data"]["task_run"]["workspace"])
        self.assertTrue(public_review["data"]["task_run"]["redacted_workspace"])
        self.assertIsNone(public_review["data"]["actor_identity"]["workspace_owner"])
        self.assertNotIn(workspace, json.dumps(public_review))
        self.assertNotIn(str(self.root / "tri-home"), json.dumps(public_review))
        self.assertNotIn(workspace, public_review["refs"].get("workspaces", []))
        self.assertNotIn("lease_values_redacted_before_log_storage", public_review["refs"].get("leases", []))
        self.assertTrue(all(item["artifact"]["path"] is None for item in public_review["data"]["artifacts"]))

        self.tri("--actor", "agent:a", "review", "approve", review)
        export_id = self.tri("--actor", "agent:a", "export", "git-patch", changeset)["data"]["export"]
        public_export = self.tri("--actor", "agent:b", "export", "inspect", export_id)
        self.assertIsNone(public_export["data"]["workspace"])
        self.assertTrue(public_export["data"]["redacted_workspace"])
        self.assertNotIn(workspace, json.dumps(public_export))
        self.assertNotIn(str(self.root / "tri-home"), json.dumps(public_export))
        self.assertNotIn(workspace, public_export["refs"].get("workspaces", []))

        search = self.tri("--actor", "agent:b", "search", "app", "review-evidence", "--scope", "changesets", "--scope", "checks")
        changeset_match = next(match for match in search["data"]["matches"] if match["object"] == changeset and match["scope"] == "changesets")
        check_match = next(match for match in search["data"]["matches"] if match["object"] == check["id"] and match["scope"] == "checks")
        self.assertEqual(changeset_match["access"]["action"], "changeset.read")
        self.assertEqual(changeset_match["access"]["reason"], "public_visibility")
        self.assertEqual(check_match["access"]["action"], "check.read")
        self.assertEqual(check_match["access"]["reason"], "linked_changeset")
        self.assertEqual(check_match["access"]["policy_object"], changeset)
        self.assertEqual(check_match["access"]["policy_action"], "changeset.read")
        self.assertNotIn(workspace, json.dumps(search))
        self.assertNotIn(workspace, search["refs"].get("workspaces", []))
        self.assertTrue(any(match["provenance"].get("workspace_redacted") for match in search["data"]["matches"]))

    def test_event_log_is_policy_gated_and_redacts_hidden_paths(self) -> None:
        imported = self.import_project()
        workspace = self.tri(
            "--actor",
            "agent:a",
            "workspace",
            "create",
            "app",
            "--from",
            imported["data"]["target"]["name"],
            "--hide-path",
            "private/**",
        )["data"]["id"]
        hidden_write = self.tri("--actor", "agent:a", "files", "write", workspace, "private/token.txt", "--text", "TOKEN=secret\n")
        hidden_event = next(
            event
            for event in hidden_write["events"]
            if self.tri("--actor", "agent:a", "events", "inspect", event)["data"]["kind"] == "workspace.write_file"
        )

        secret_file = self.root / "token.txt"
        secret_file.write_text("lease-secret")
        secret = self.tri("secrets", "put", "app", "API_TOKEN", "--value-file", str(secret_file))["data"]["id"]
        self.tri("policy", "grant", secret, "--principal", "actor:agent:a", "--action", "secret.use")
        self.tri("--actor", "agent:a", "secrets", "request-lease", workspace, "API_TOKEN", "--env", "API_TOKEN")

        blocked_events = self.tri("--actor", "agent:b", "events", "list", "--workspace", workspace)
        self.assertEqual(blocked_events["data"]["events"], [])
        self.assertGreaterEqual(blocked_events["data"]["redacted"], 3)
        denials_after_list = self.tri("events", "list", "--workspace", workspace, "--kind", "policy.denied")
        self.assertEqual(denials_after_list["data"]["events"], [])
        blocked_event = self.tri("--actor", "agent:b", "events", "inspect", hidden_event, ok=False)
        self.assertEqual(blocked_event["diagnostics"][0]["code"], "policy_denied")
        self.assertGreaterEqual(blocked_event["diagnostics"][0]["policy_candidate_count"], 1)
        self.assertEqual(blocked_event["diagnostics"][0]["redacted_candidates"], blocked_event["diagnostics"][0]["policy_candidate_count"])
        self.assertIn("workspace.read", blocked_event["diagnostics"][0]["required_grants"])
        self.assertNotIn(workspace, blocked_event["refs"].get("workspaces", []))
        blocked_event_decision = blocked_event["diagnostics"][0]["decision"]
        blocked_event_policy_event = blocked_event["diagnostics"][0]["event"]
        self.assertEqual(blocked_event["diagnostics"][0]["explain_command"], ["tri", "policy", "explain", blocked_event_decision])
        self.assertIn(blocked_event_decision, blocked_event["refs"].get("decisions", []))
        self.assertIn(blocked_event_policy_event, blocked_event["refs"].get("events", []))
        explained_event = self.tri("--actor", "agent:b", "policy", "explain", blocked_event_decision)
        self.assertEqual(explained_event["data"]["event"]["kind"], "policy.denied")
        self.assertEqual(explained_event["data"]["decision"]["action"], "event.read")
        self.assertEqual(explained_event["data"]["decision"]["reason_code"], "policy_denied")
        denials_after_inspect = self.tri("events", "list", "--workspace", workspace, "--kind", "policy.denied")
        self.assertEqual([event["id"] for event in denials_after_inspect["data"]["events"]], [blocked_event_policy_event])

        blocked_audit = self.tri("--actor", "agent:b", "secrets", "audit", "--workspace", workspace)
        self.assertEqual(blocked_audit["data"]["events"], [])
        self.assertGreaterEqual(blocked_audit["data"]["redacted"], 1)

        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        allowed_events = self.tri("--actor", "agent:b", "events", "list", "--workspace", workspace)
        self.assertIn(hidden_event, {event["id"] for event in allowed_events["data"]["events"]})
        allowed_event = self.tri("--actor", "agent:b", "events", "inspect", hidden_event)
        paths = allowed_event["data"]["object_refs"]["paths"]
        self.assertIsNone(paths[0]["path"])
        self.assertEqual(paths[0]["redaction"], "hidden_path")
        self.assertNotIn("private/token.txt", json.dumps(allowed_event))

        allowed_audit = self.tri("--actor", "agent:b", "secrets", "audit", "--workspace", workspace)
        self.assertIn("secret.lease_granted", {event["kind"] for event in allowed_audit["data"]["events"]})
        self.assertEqual(allowed_audit["data"]["redacted"], 0)

    def test_secret_metadata_use_and_lease_revoke_are_policy_bound(self) -> None:
        imported = self.import_project()
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        secret_file = self.root / "token.txt"
        secret_file.write_text("scoped-secret")
        secret = self.tri("secrets", "put", "app", "API_TOKEN", "--value-file", str(secret_file))["data"]["id"]

        listed = self.tri("--actor", "agent:a", "secrets", "list", "app")
        self.assertEqual(listed["data"]["secrets"], [])
        self.assertEqual(listed["data"]["redacted"], 1)
        blocked_inspect = self.tri("--actor", "agent:a", "secrets", "inspect", secret, ok=False)
        self.assertEqual(blocked_inspect["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_inspect["diagnostics"][0]["required_grant"], "secret.read")
        blocked_lease = self.tri("--actor", "agent:a", "secrets", "request-lease", workspace, "API_TOKEN", "--env", "API_TOKEN", ok=False)
        self.assertEqual(blocked_lease["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_lease["diagnostics"][0]["required_grant"], "secret.use")

        self.tri("policy", "grant", secret, "--principal", "actor:agent:a", "--action", "secret.use")
        lease = self.tri("--actor", "agent:a", "secrets", "request-lease", workspace, "API_TOKEN", "--env", "API_TOKEN")["data"]["id"]
        still_hidden = self.tri("--actor", "agent:a", "secrets", "list", "app")
        self.assertEqual(still_hidden["data"]["secrets"], [])
        self.assertEqual(still_hidden["data"]["redacted"], 1)

        self.tri("policy", "grant", secret, "--principal", "actor:agent:a", "--action", "secret.read")
        visible = self.tri("--actor", "agent:a", "secrets", "list", "app")
        self.assertEqual(visible["data"]["secrets"][0]["id"], secret)
        self.assertEqual(visible["data"]["redacted"], 0)
        inspected = self.tri("--actor", "agent:a", "secrets", "inspect", "API_TOKEN", "--project", "app")
        self.assert_schema_required_keys("tri.secrets.inspect", inspected["data"])
        self.assertEqual(inspected["data"]["id"], secret)
        self.assertEqual(inspected["data"]["name"], "API_TOKEN")
        self.assertEqual(inspected["data"]["provider"], "local_file")
        self.assertEqual(inspected["data"]["created_by"], "actor_local")
        self.assertIsNone(inspected["data"]["revoked_at"])
        self.assertNotIn("scoped-secret", json.dumps(inspected))

        blocked_revoke = self.tri("--actor", "agent:b", "secrets", "revoke", lease, ok=False)
        self.assertEqual(blocked_revoke["diagnostics"][0]["code"], "policy_denied")
        revoked = self.tri("--actor", "agent:a", "secrets", "revoke", lease)
        self.assert_schema_required_keys("tri.secret_lease.inspect", revoked["data"])
        self.assertEqual(revoked["data"]["status"], "revoked")
        self.assertEqual(revoked["data"]["actor"], "agent:a")
        self.assertIsNotNone(revoked["data"]["revoked_at"])

    def test_secret_leases_are_bound_to_requesting_actor(self) -> None:
        imported = self.import_project()
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        secret_file = self.root / "token.txt"
        secret_file.write_text("bound-secret")
        secret = self.tri("secrets", "put", "app", "API_TOKEN", "--value-file", str(secret_file))["data"]["id"]
        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.write")
        self.tri("policy", "grant", secret, "--principal", "actor:agent:a", "--action", "secret.use")
        lease_a = self.tri("--actor", "agent:a", "secrets", "request-lease", workspace, "API_TOKEN", "--env", "API_TOKEN")["data"]["id"]

        check_a = self.tri(
            "--actor",
            "agent:a",
            "run",
            workspace,
            "--with-lease",
            lease_a,
            "--",
            "python3",
            "-c",
            "import os; assert os.environ['API_TOKEN'] == 'bound-secret'",
        )
        self.assertEqual(check_a["data"]["status"], "passed")

        blocked_run = self.tri(
            "--actor",
            "agent:b",
            "run",
            workspace,
            "--with-lease",
            lease_a,
            "--",
            "python3",
            "-c",
            "pass",
            ok=False,
        )
        self.assertEqual(blocked_run["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_run["diagnostics"][0]["required_grant"], "lease_actor")

        blocked_rerun = self.tri("--actor", "agent:b", "checks", "rerun", check_a["data"]["id"], ok=False)
        self.assertEqual(blocked_rerun["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_rerun["diagnostics"][0]["required_grant"], "lease_actor")

        self.tri("policy", "grant", secret, "--principal", "actor:agent:b", "--action", "secret.use")
        still_bound = self.tri(
            "--actor",
            "agent:b",
            "run",
            workspace,
            "--with-lease",
            lease_a,
            "--",
            "python3",
            "-c",
            "pass",
            ok=False,
        )
        self.assertEqual(still_bound["diagnostics"][0]["required_grant"], "lease_actor")

        lease_b = self.tri("--actor", "agent:b", "secrets", "request-lease", workspace, "API_TOKEN", "--env", "API_TOKEN")["data"]["id"]
        check_b = self.tri(
            "--actor",
            "agent:b",
            "run",
            workspace,
            "--with-lease",
            lease_b,
            "--",
            "python3",
            "-c",
            "import os; assert os.environ['API_TOKEN'] == 'bound-secret'",
        )
        self.assertEqual(check_b["data"]["status"], "passed")
        self.assertEqual(check_b["data"]["secret_leases"], [lease_b])

    def test_stale_workspace_write_fails_with_diagnostic(self) -> None:
        imported = self.import_project()
        command_names = {item["name"] for item in self.tri("commands")["data"]["commands"]}
        self.assertIn("files-delete", command_names)
        self.assertIn("files-move", command_names)
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        original_head = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "two\n")

        stale = self.tri(
            "files",
            "write",
            workspace,
            "src/other.txt",
            "--text",
            "later\n",
            "--expect-head",
            original_head,
            ok=False,
        )
        self.assertEqual(stale["diagnostics"][0]["code"], "stale_workspace_head")
        stale_delete = self.tri("files", "delete", workspace, "README.md", "--expect-head", original_head, ok=False)
        self.assertEqual(stale_delete["diagnostics"][0]["code"], "stale_workspace_head")
        stale_move = self.tri("files", "move", workspace, "README.md", "docs/readme.md", "--expect-head", original_head, ok=False)
        self.assertEqual(stale_move["diagnostics"][0]["code"], "stale_workspace_head")
        stale_snapshot = self.tri("workspace", "snapshot", workspace, "--expect-head", original_head, ok=False)
        self.assertEqual(stale_snapshot["diagnostics"][0]["code"], "stale_workspace_head")
        self.assertEqual(stale_snapshot["diagnostics"][0]["expected"], original_head)

    def test_run_file_changes_fail_stale_if_workspace_head_moves(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        before = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]
        release = self.root / "release-run"
        proc = subprocess.Popen(
            [
                "python3",
                "-m",
                "tri",
                "run",
                workspace,
                "--",
                "python3",
                "-c",
                # Wait for the test to finish its concurrent write instead of racing a fixed sleep.
                f"import time; from pathlib import Path; deadline = time.time() + 30\nwhile not Path({str(release)!r}).exists() and time.time() < deadline: time.sleep(0.02)\nPath('src/app.txt').write_text('run edit\\n')",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                self.fail(f"tri run exited before check.started\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            events = self.tri("events", "list", "--workspace", workspace, "--kind", "check.started")["data"]["events"]
            if events:
                break
            time.sleep(0.05)
        else:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=5)
            self.fail(f"tri run did not create check.started\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")

        concurrent = self.tri("files", "write", workspace, "README.md", "--text", "concurrent\n")
        release.write_text("go")
        stdout, stderr = proc.communicate(timeout=30)
        self.assertNotEqual(proc.returncode, 0, stdout)
        result = json.loads(stdout)
        self.assertFalse(result["ok"])
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["code"], "stale_workspace_head")
        self.assertEqual(diagnostic["expected"], before)
        self.assertEqual(diagnostic["actual"], concurrent["data"]["snapshot"])
        self.assertEqual(diagnostic["paths"], ["src/app.txt"])
        self.assertEqual(result["data"]["status"], "stale")
        self.assertEqual(result["data"]["snapshot_before"], before)
        self.assertEqual(result["data"]["snapshot_after"], before)
        self.assertTrue(result["data"]["materialized_directory"]["mutated"])
        self.assertEqual(result["data"]["reproducibility"], "not_imported_stale_workspace_head")
        self.assertEqual(
            [self.tri("events", "inspect", event)["data"]["kind"] for event in result["events"]],
            ["task_run.checking", "policy.evaluated", "check.started", "check.completed"],
        )
        self.assertEqual(self.tri("files", "read", workspace, "src/app.txt")["data"]["text"], "one\n")
        self.assertEqual(self.tri("files", "read", workspace, "README.md")["data"]["text"], "concurrent\n")

    def test_workspace_checkpoint_fails_if_workspace_head_moves_at_commit(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        before = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]
        store = self.open_store()
        try:
            self.install_head_race(store, workspace)
            with self.assertRaises(TriError) as caught:
                cmd_workspace_snapshot(
                    store,
                    argparse.Namespace(workspace=workspace, actor="actor_local"),
                    ["tri", "workspace", "snapshot", workspace],
                    time.time(),
                )
            self.assertEqual(caught.exception.diagnostic["code"], "stale_workspace_head")
            self.assertEqual(caught.exception.diagnostic["expected"], before)
        finally:
            store.close()
        self.assertEqual(self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"], before)

    def test_files_stat_and_search_are_agent_readable(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]

        stat = self.tri("files", "stat", workspace, "src/app.txt")
        self.assertEqual(stat["data"]["path"], "src/app.txt")
        self.assertEqual(stat["data"]["type"], "file")
        self.assertEqual(stat["data"]["size"], 4)
        self.assertTrue(stat["data"]["visible"])

        search = self.tri("files", "search", workspace, "one")
        self.assertEqual(search["data"]["matches"][0]["path"], "src/app.txt")
        self.assertEqual(search["data"]["matches"][0]["line"], 1)
        self.assertTrue(search["data"]["matches"][0]["access"]["allowed"])
        self.assertEqual(search["data"]["matches"][0]["access"]["reason"], "authorized_snapshot_context")

        self.tri("files", "write", workspace, ".env", "--text", "TOKEN=secret\n")
        redacted = self.tri("files", "search", workspace, "TOKEN")
        self.assertEqual(redacted["data"]["matches"], [])
        self.assertEqual(redacted["data"]["redacted_skipped"], 1)

        snapshot = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]
        snapshot_stat = self.tri("files", "stat", snapshot, "README.md")
        self.assertEqual(snapshot_stat["data"]["path"], "README.md")

        moved = self.tri("files", "move", workspace, "README.md", "docs/readme.md")
        self.assertEqual(moved["data"]["paths"], ["README.md", "docs/readme.md"])
        self.assertEqual(self.tri("files", "read", workspace, "docs/readme.md")["data"]["text"], "hello\n")

        deleted = self.tri("files", "delete", workspace, "docs/readme.md")
        self.assertEqual(deleted["data"]["paths"], ["docs/readme.md"])
        missing = self.tri("files", "stat", workspace, "docs/readme.md", ok=False)
        self.assertEqual(missing["diagnostics"][0]["code"], "path_not_found")

    def test_historical_workspace_snapshots_are_policy_gated(self) -> None:
        (self.repo / "private").mkdir()
        (self.repo / "private" / "seed.txt").write_text("public project seed\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "add public private path"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        imported = self.import_project()
        base_snapshot = imported["data"]["snapshot"]["id"]
        target = imported["data"]["target"]["name"]
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target, "--hide-path", "private/**")["data"]["id"]
        historical_snapshot = self.tri("--actor", "agent:a", "files", "write", workspace, "private/old.txt", "--text", "hidden old\n")["data"]["snapshot"]
        head_snapshot = self.tri("--actor", "agent:a", "files", "write", workspace, "src/app.txt", "--text", "new head\n")["data"]["snapshot"]

        public_base = self.tri("--actor", "agent:b", "snapshots", "inspect", base_snapshot)
        self.assertEqual(public_base["data"]["access"]["reason"], "public_source")
        self.assertEqual(public_base["data"]["redacted"]["workspaces"], 1)
        public_base_files = self.tri("--actor", "agent:b", "files", "list", base_snapshot)["data"]
        self.assertIn("private/seed.txt", {item["path"] for item in public_base_files["files"]})
        self.assertEqual(public_base_files["policy_denied_skipped"], 0)

        blocked_snapshot = self.tri("--actor", "agent:b", "snapshots", "inspect", historical_snapshot, ok=False)
        self.assertEqual(blocked_snapshot["diagnostics"][0]["code"], "policy_denied")
        self.assertIn("workspace.read", blocked_snapshot["diagnostics"][0]["required_grants"])
        self.assertNotIn(workspace, json.dumps(blocked_snapshot["diagnostics"]))
        self.assertNotIn(workspace, blocked_snapshot["refs"].get("workspaces", []))
        agent_b_snapshots = self.tri("--actor", "agent:b", "snapshots", "list", "app")
        self.assertEqual(agent_b_snapshots["data"]["redacted"], 2)
        self.assertEqual([item["id"] for item in agent_b_snapshots["data"]["snapshots"]], [base_snapshot])

        blocked = self.tri("--actor", "agent:b", "files", "stat", historical_snapshot, "src/app.txt", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "policy_denied")
        self.assertGreaterEqual(blocked["diagnostics"][0]["candidate_count"], 1)
        self.assertEqual(blocked["diagnostics"][0]["redacted_candidates"], blocked["diagnostics"][0]["candidate_count"])
        self.assertIn("workspace.read", blocked["diagnostics"][0]["required_grants"])
        self.assertNotIn(workspace, json.dumps(blocked["diagnostics"]))
        self.assertNotIn(workspace, blocked["refs"].get("workspaces", []))

        blocked_diff = self.tri("--actor", "agent:b", "diff", base_snapshot, head_snapshot, ok=False)
        self.assertEqual(blocked_diff["diagnostics"][0]["code"], "policy_denied")
        self.assertIn("workspace.read", blocked_diff["diagnostics"][0]["required_grants"])
        self.assertNotIn(workspace, json.dumps(blocked_diff["diagnostics"]))
        self.assertNotIn(workspace, blocked_diff["refs"].get("workspaces", []))

        owner_diff = self.tri("--actor", "agent:a", "diff", base_snapshot, head_snapshot)
        self.assertEqual(owner_diff["data"]["summary"]["modified"], 1)
        self.assertEqual(owner_diff["data"]["summary"]["policy_denied"], 1)
        self.assertEqual([item["path"] for item in owner_diff["data"]["files"] if item["visible"]], ["src/app.txt"])
        self.assertNotIn("private/old.txt", json.dumps(owner_diff))
        hidden_file = [item for item in owner_diff["data"]["files"] if not item["visible"]][0]
        self.assertIsNone(hidden_file["path"])
        self.assertEqual(hidden_file["redaction"], "hidden_path")

        owner_list = self.tri("--actor", "agent:a", "files", "list", historical_snapshot)
        self.assertNotIn("private/old.txt", {item["path"] for item in owner_list["data"]["files"]})
        self.assertEqual(owner_list["data"]["policy_denied_skipped"], 2)

        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        granted_snapshot = self.tri("--actor", "agent:b", "snapshots", "inspect", historical_snapshot)
        self.assert_schema_required_keys("tri.snapshot.inspect", granted_snapshot["data"])
        self.assertEqual(granted_snapshot["data"]["id"], historical_snapshot)
        self.assertEqual(granted_snapshot["data"]["access"]["reason"], "origin_access")
        self.assertEqual(granted_snapshot["data"]["workspaces"], [workspace])
        self.assertEqual(granted_snapshot["data"]["redacted"]["workspaces"], 0)
        self.assertIn(workspace, granted_snapshot["refs"].get("workspaces", []))
        granted_snapshots = self.tri("--actor", "agent:b", "snapshots", "list", "app")
        self.assertEqual(granted_snapshots["data"]["redacted"], 0)
        self.assertEqual([item["id"] for item in granted_snapshots["data"]["snapshots"]], [base_snapshot, historical_snapshot, head_snapshot])
        allowed = self.tri("--actor", "agent:b", "files", "stat", historical_snapshot, "src/app.txt")
        self.assertTrue(allowed["data"]["visible"])
        granted_list = self.tri("--actor", "agent:b", "files", "list", historical_snapshot)
        self.assertNotIn("private/old.txt", {item["path"] for item in granted_list["data"]["files"]})
        self.assertEqual(granted_list["data"]["policy_denied_skipped"], 2)
        granted_diff = self.tri("--actor", "agent:b", "diff", base_snapshot, head_snapshot)
        self.assertEqual(granted_diff["data"]["summary"], owner_diff["data"]["summary"])
        self.assertEqual(granted_diff["data"]["files"], owner_diff["data"]["files"])

    def test_package_boundaries_are_inspectable_and_expand_hidden_paths(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        blocked = self.tri("--actor", "agent:a", "packages", "declare", "app", "internal", "--path", "internal/**", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked["diagnostics"][0]["action"], "package.declare")

        package = self.tri(
            "packages",
            "declare",
            "app",
            "frontend",
            "--path",
            "src/**",
            "--visibility",
            "public",
            "--owner",
            "role:maintainer",
        )["data"]
        self.assertEqual(package["name"], "frontend")
        self.assertEqual(package["path_globs"], ["src/**"])
        self.assertEqual(package["owners"], ["role:maintainer"])
        self.assertIn(package["id"], self.tri("packages", "list", "app")["refs"].get("packages", []))
        inspected = self.tri("packages", "inspect", "app", "frontend")
        self.assertEqual(inspected["data"]["id"], package["id"])
        inspected_by_id = self.tri("packages", "inspect", package["id"])
        self.assertEqual(inspected_by_id["data"]["name"], "frontend")
        generic = self.tri("objects", "inspect", package["id"])
        self.assertEqual(generic["data"]["object_type"], "package")
        self.assertEqual(generic["data"]["resolved_schema"], "tri.package.inspect")

        internal = self.tri("packages", "declare", "app", "internal", "--path", "internal/**", "--visibility", "private")["data"]
        project = self.tri("projects", "inspect", "app")["data"]
        self.assertEqual(project["counts"]["package_boundaries"], 2)
        self.assertEqual(project["redacted_package_boundaries"], 0)
        self.assertEqual([item["name"] for item in project["package_boundaries"]], ["frontend", "internal"])

        agent_packages = self.tri("--actor", "agent:a", "packages", "list", "app")
        self.assertEqual([item["name"] for item in agent_packages["data"]["packages"]], ["frontend"])
        self.assertEqual(agent_packages["data"]["redacted"], 1)
        self.assertNotIn(internal["id"], agent_packages["refs"].get("packages", []))
        agent_project = self.tri("--actor", "agent:a", "projects", "inspect", "app")["data"]
        self.assertEqual([item["name"] for item in agent_project["package_boundaries"]], ["frontend"])
        self.assertEqual(agent_project["redacted_package_boundaries"], 1)
        blocked_package = self.tri("--actor", "agent:a", "packages", "inspect", "app", "internal", ok=False)
        self.assertEqual(blocked_package["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_package["diagnostics"][0]["action"], "package.read")
        blocked_hide_package = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target, "--hide-package", "internal", ok=False)
        self.assertEqual(blocked_hide_package["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_hide_package["diagnostics"][0]["action"], "package.read")
        package_search = self.tri("--actor", "agent:a", "search", "app", "internal", "--scope", "packages")
        self.assertEqual(package_search["data"]["matches"], [])
        self.assertEqual(package_search["data"]["policy_denied_skipped"], 1)

        agent_workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target, "--visibility", "public")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", agent_workspace, "src/agent.txt", "--text", "agent public package change\n")
        self.tri("--actor", "agent:a", "files", "write", agent_workspace, "internal/agent.txt", "--text", "agent private package change\n")
        agent_listing = self.tri("--actor", "agent:a", "files", "list", agent_workspace)["data"]
        self.assertIn("src/agent.txt", {item["path"] for item in agent_listing["files"]})
        self.assertNotIn("internal/agent.txt", {item["path"] for item in agent_listing["files"]})
        self.assertEqual(agent_listing["policy_denied_skipped"], 1)
        maintainer_listing = self.tri("files", "list", agent_workspace)["data"]
        self.assertIn("internal/agent.txt", {item["path"] for item in maintainer_listing["files"]})
        agent_diff = self.tri("--actor", "agent:a", "diff", agent_workspace)
        self.assertEqual(agent_diff["data"]["summary"]["policy_denied"], 1)
        self.assertNotIn("internal/agent.txt", json.dumps(agent_diff))
        agent_changeset = self.tri("--actor", "agent:a", "changeset", "create", agent_workspace, "--title", "Agent package policy", "--visibility", "public")["data"]
        self.assertEqual(agent_changeset["touched_paths"], ["src/agent.txt"])
        self.assertEqual(agent_changeset["redacted_touched_paths"], 1)
        maintainer_changeset = self.tri("changeset", "inspect", agent_changeset["id"])["data"]
        self.assertIn("internal/agent.txt", maintainer_changeset["touched_paths"])
        blocked_export = self.tri("--actor", "agent:a", "policy", "evaluate", "export", agent_changeset["id"], ok=False)
        self.assertIn("visibility_export_blocked", blocked_export["data"]["denials"])

        workspace = self.tri("workspace", "create", "app", "--from", target, "--hide-package", "internal")["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "frontend package change\n")
        self.tri("files", "write", workspace, "internal/data.txt", "--text", "internal package change\n")
        listing = self.tri("files", "list", workspace)["data"]
        self.assertIn("src/app.txt", {item["path"] for item in listing["files"]})
        self.assertNotIn("internal/data.txt", {item["path"] for item in listing["files"]})
        self.assertEqual(listing["policy_denied_skipped"], 1)
        blocked_read = self.tri("files", "read", workspace, "internal/data.txt", ok=False)
        self.assertEqual(blocked_read["diagnostics"][0]["code"], "policy_denied")

        changeset = self.tri("changeset", "create", workspace, "--title", "Package boundary change")["data"]
        self.assertEqual(changeset["touched_paths"], ["src/app.txt"])
        self.assertEqual(changeset["redacted_touched_paths"], 1)
        package_splits = [item for item in changeset["suggested_split_points"] if item["kind"] == "package"]
        self.assertEqual(package_splits[0]["package"], package["id"])
        self.assertEqual(package_splits[0]["matched_paths"], ["src/app.txt"])
        self.assertNotIn(internal["id"], json.dumps(changeset))

        search = self.tri("search", "app", "frontend", "--scope", "packages")
        self.assertEqual(search["data"]["matches"][0]["scope"], "packages")
        self.assertEqual(search["data"]["matches"][0]["object"], package["id"])

    def test_project_search_spans_authorized_work_and_metadata(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        task = self.tri(
            "task",
            "create",
            "app",
            "--title",
            "project search task",
            "--body",
            "task project needle context",
        )["data"]["id"]
        workspace = self.tri(
            "workspace",
            "create",
            "app",
            "--from",
            target,
            "--task",
            task,
            "--hide-path",
            "private/**",
        )["data"]["id"]

        self.tri("files", "write", workspace, "src/old.txt", "--text", "old project needle\n")
        self.tri("files", "write", workspace, "src/old.txt", "--text", "gone\n")
        self.tri("files", "write", workspace, "src/app.txt", "--text", "project needle in source\n")
        self.tri("run", workspace, "--", "python3", "-c", "print('check project needle')")
        changeset = self.tri("changeset", "create", workspace, "--title", "project needle changeset")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "comment", review, "--body", "review project needle")
        self.tri("review", "approve", review)
        self.tri("release", "prepare", "app", "--include", changeset, "--package", "project needle package")
        self.tri("files", "write", workspace, "private/notes.txt", "--text", "hidden project needle\n")

        search = self.tri("search", "app", "project needle", "--include-history")
        scopes = {match["scope"] for match in search["data"]["matches"]}
        self.assertTrue({"workspaces", "changesets", "tasks", "reviews", "checks", "releases"}.issubset(scopes))
        self.assertTrue(all(match["access"]["allowed"] for match in search["data"]["matches"]))
        self.assertTrue(all(match["access"]["reason_code"] == "allowed" for match in search["data"]["matches"]))
        self.assertTrue(any(match["path"] == "src/old.txt" for match in search["data"]["matches"]))
        self.assertGreaterEqual(search["data"]["redacted_skipped"], 1)
        self.assertNotIn("private/notes.txt", json.dumps(search))
        self.assertNotIn("hidden project needle", json.dumps(search))

        restricted = self.tri(
            "--actor",
            "agent:b",
            "search",
            "app",
            "project needle",
            "--scope",
            "workspaces",
            "--scope",
            "changesets",
            "--scope",
            "reviews",
            "--scope",
            "checks",
        )
        self.assertEqual(restricted["data"]["matches"], [])
        self.assertGreaterEqual(restricted["data"]["policy_denied_skipped"], 4)

        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        allowed = self.tri(
            "--actor",
            "agent:b",
            "search",
            "app",
            "project needle",
            "--scope",
            "workspaces",
            "--scope",
            "changesets",
            "--scope",
            "reviews",
            "--scope",
            "checks",
        )
        allowed_scopes = {match["scope"] for match in allowed["data"]["matches"]}
        self.assertTrue({"workspaces", "changesets", "reviews", "checks"}.issubset(allowed_scopes))
        self.assertTrue(all(match["access"]["allowed"] for match in allowed["data"]["matches"]))
        self.assertNotIn("private/notes.txt", json.dumps(allowed))

    def test_hidden_path_policy_redacts_review_surfaces_and_blocks_export(self) -> None:
        imported = self.import_project()
        task = self.tri("task", "create", "app", "--title", "Hidden path work")["data"]
        workspace = self.tri(
            "workspace",
            "create",
            "app",
            "--from",
            imported["data"]["target"]["name"],
            "--task",
            task["id"],
            "--hide-path",
            "private/**",
        )["data"]["id"]
        inspected_workspace = self.tri("workspace", "inspect", workspace)
        self.assertEqual(inspected_workspace["data"]["hidden_paths"], ["private/**"])

        self.tri("files", "write", workspace, "src/app.txt", "--text", "public edit\n")
        self.tri("files", "write", workspace, "private/notes.txt", "--text", "private context\n")

        listed = self.tri("files", "list", workspace)
        self.assertNotIn("private/notes.txt", {item["path"] for item in listed["data"]["files"]})
        self.assertEqual(listed["data"]["policy_denied_skipped"], 1)

        stat = self.tri("files", "stat", workspace, "private/notes.txt")
        self.assertFalse(stat["data"]["visible"])
        self.assertIsNone(stat["data"]["blob"])
        self.assertEqual(stat["data"]["redaction"], "hidden_path")

        read = self.tri("files", "read", workspace, "private/notes.txt", ok=False)
        self.assertEqual(read["diagnostics"][0]["code"], "policy_denied")

        search = self.tri("files", "search", workspace, "private context")
        self.assertEqual(search["data"]["matches"], [])
        self.assertEqual(search["data"]["policy_denied_skipped"], 1)

        head_snapshot = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]
        snapshot_list = self.tri("files", "list", head_snapshot)
        self.assertNotIn("private/notes.txt", {item["path"] for item in snapshot_list["data"]["files"]})
        self.assertEqual(snapshot_list["data"]["policy_denied_skipped"], 1)

        diff = self.tri("diff", workspace)
        self.assertNotIn("private/notes.txt", json.dumps(diff))
        self.assertEqual(diff["data"]["summary"]["policy_denied"], 1)
        hidden_file = [item for item in diff["data"]["files"] if not item["visible"]][0]
        self.assertIsNone(hidden_file["path"])
        self.assertEqual(hidden_file["redaction"], "hidden_path")

        check = self.tri(
            "run",
            workspace,
            "--",
            "python3",
            "-c",
            "from pathlib import Path; assert Path('src/app.txt').read_text() == 'public edit\\n'; p = 'private/' + 'run-notes.txt'; Path(p).write_text('private run context\\n')",
        )
        self.assertEqual(len(check["data"]["file_changes"]), 1)
        self.assertEqual(check["data"]["outputs"]["file_changes"], check["data"]["file_changes"])
        self.assertEqual(check["data"]["redacted_file_changes"], 1)
        self.assertNotIn("private/run-notes.txt", json.dumps(check))
        redacted_operation = self.tri("operations", "inspect", check["data"]["file_changes"][0])
        self.assertEqual(redacted_operation["data"]["paths"], [])
        self.assertEqual(redacted_operation["data"]["redacted_paths"], 1)
        task_run = self.tri("task", "run-inspect", inspected_workspace["data"]["task_run"])
        self.assertEqual(task_run["data"]["checks"][0]["redacted_file_changes"], 1)
        self.assertNotIn("private/run-notes.txt", json.dumps(task_run))
        changeset = self.tri("changeset", "create", workspace, "--title", "Public plus private")
        self.assertEqual(changeset["data"]["touched_paths"], ["src/app.txt"])
        self.assertEqual(changeset["data"]["redacted_touched_paths"], 2)

        review = self.tri("review", "create", changeset["data"]["id"])["data"]["id"]
        inspected_review = self.tri("review", "inspect", review)
        self.assertNotIn("private/notes.txt", json.dumps(inspected_review))
        self.assertNotIn("private/run-notes.txt", json.dumps(inspected_review))
        self.assertEqual(inspected_review["data"]["checks"][0]["redacted_file_changes"], 1)
        self.assertEqual(inspected_review["data"]["export_preview"]["policy_denied"], 2)
        self.tri("review", "approve", review)

        blocked = self.tri("export", "git-patch", changeset["data"]["id"], ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "visibility_export_blocked")
        self.assertNotIn("private/notes.txt", json.dumps(blocked))

    def test_workspace_list_snapshot_discard_and_archive(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]

        listed = self.tri("workspace", "list", "app")
        self.assertEqual([item["id"] for item in listed["data"]["workspaces"]], [workspace])

        before = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]
        checkpoint = self.tri("workspace", "snapshot", workspace)
        self.assertNotEqual(checkpoint["data"]["snapshot"], before)
        inspected = self.tri("workspace", "inspect", workspace)
        self.assertEqual(inspected["data"]["head_snapshot"], checkpoint["data"]["snapshot"])
        self.assertEqual(inspected["data"]["last_operation"], checkpoint["data"]["operation"])

        paused = self.tri("workspace", "pause", workspace)
        self.assertEqual(paused["data"]["previous_status"], "active")
        self.assertEqual(paused["data"]["status"], "paused")
        self.assertEqual(paused["data"]["transition"], "active->paused")
        read_paused = self.tri("files", "read", workspace, "src/app.txt")
        self.assertEqual(read_paused["data"]["text"], "one\n")
        blocked_paused_write = self.tri("files", "write", workspace, "src/app.txt", "--text", "blocked while paused\n", ok=False)
        self.assertEqual(blocked_paused_write["diagnostics"][0]["code"], "policy_denied")
        blocked_paused_changeset = self.tri("changeset", "create", workspace, "--title", "Blocked while paused", ok=False)
        self.assertEqual(blocked_paused_changeset["diagnostics"][0]["code"], "policy_denied")
        resumed = self.tri("workspace", "resume", workspace)
        self.assertEqual(resumed["data"]["previous_status"], "paused")
        self.assertEqual(resumed["data"]["status"], "active")
        self.assertEqual(resumed["data"]["transition"], "paused->active")

        self.tri("files", "write", workspace, "src/app.txt", "--text", "discarded export\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'discarded export\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Discarded export")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "approve", review)
        paused_again = self.tri("workspace", "pause", workspace)
        self.assertEqual(paused_again["data"]["status"], "paused")
        discarded = self.tri("workspace", "discard", workspace)
        self.assertEqual(discarded["data"]["previous_status"], "paused")
        self.assertEqual(discarded["data"]["transition"], "paused->discarded")
        self.assertEqual(discarded["data"]["status"], "discarded")
        blocked_resume = self.tri("workspace", "resume", workspace, ok=False)
        self.assertEqual(blocked_resume["diagnostics"][0]["code"], "workspace_lifecycle_blocked")
        blocked_export = self.tri("export", "git-patch", changeset, ok=False)
        self.assertEqual(blocked_export["diagnostics"][0]["code"], "workspace_lifecycle_blocked")
        self.assertEqual(blocked_export["data"]["record"]["policy_decision"]["workspace_lifecycle"]["status"], "discarded")
        blocked = self.tri("files", "write", workspace, "src/app.txt", "--text", "blocked\n", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "policy_denied")

        archived_workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        archived = self.tri("workspace", "archive", archived_workspace)
        self.assertEqual(archived["data"]["status"], "archived")
        mount_path = self.root / "archived-mount"
        mounted = self.tri("workspace", "mount", archived_workspace, str(mount_path))
        self.assertTrue(mounted["data"]["read_only"])
        self.assertEqual(mounted["data"]["materialization"]["status"], "read_only")
        self.assertTrue((mount_path / "src" / "app.txt").exists())
        blocked_sync = self.tri("workspace", "sync", archived_workspace, "--lease", mounted["data"]["lease"], ok=False)
        self.assertEqual(blocked_sync["diagnostics"][0]["code"], "policy_denied")
        blocked_run = self.tri("run", archived_workspace, "--", "python3", "-c", "print('no')", ok=False)
        self.assertEqual(blocked_run["diagnostics"][0]["code"], "policy_denied")

        promoted_workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", promoted_workspace, "src/app.txt", "--text", "promoted\n")
        self.tri("run", promoted_workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'promoted\\n'")
        promoted_changeset = self.tri("changeset", "create", promoted_workspace, "--title", "Promote then archive")["data"]["id"]
        promoted_review = self.tri("review", "create", promoted_changeset)["data"]["id"]
        self.tri("review", "approve", promoted_review)
        self.tri("changeset", "merge", promoted_changeset, "--target", imported["data"]["target"]["name"])
        self.assertEqual(self.tri("workspace", "inspect", promoted_workspace)["data"]["status"], "promoted")
        blocked_promoted_write = self.tri("files", "write", promoted_workspace, "src/app.txt", "--text", "blocked promoted\n", ok=False)
        self.assertEqual(blocked_promoted_write["diagnostics"][0]["code"], "policy_denied")
        archived_promoted = self.tri("workspace", "archive", promoted_workspace)
        self.assertEqual(archived_promoted["data"]["previous_status"], "promoted")
        self.assertEqual(archived_promoted["data"]["transition"], "promoted->archived")

    def test_agent_discovery_events_and_artifact_read(self) -> None:
        commands = self.tri("commands")
        self.assertGreaterEqual(len(commands["data"]["commands"]), 20)
        self.assertTrue(any(command["name"] == "workspace-sync" for command in commands["data"]["commands"]))

        schema = self.tri("schema", "tri.workspace.inspect")
        self.assertIn("required", schema["data"]["schema"])

        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        check = self.tri("run", workspace, "--", "python3", "-c", "print('agent-log')")
        stdout_artifact = check["data"]["logs"]["stdout"]
        config = self.tri("config", "materialize", workspace, "--profile", "test")
        config_artifact = config["data"]["artifact"]

        artifact = self.tri("artifacts", "read", stdout_artifact)
        self.assert_schema_required_keys("tri.artifacts.read", artifact["data"])
        self.assertEqual(artifact["data"]["text"], "agent-log\n")
        self.assertIsNone(artifact["data"]["path"])
        self.assertTrue(artifact["data"]["fingerprint"].startswith("sha256:"))
        self.assertEqual(artifact["data"]["storage"]["content_command"], ["artifacts", "read", stdout_artifact])
        self.assertEqual(artifact["data"]["origins"][0]["type"], "check")
        self.assertTrue(artifact["data"]["access"]["allowed"])

        blocked = self.tri("--actor", "agent:b", "artifacts", "read", stdout_artifact, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked["diagnostics"][0]["origin_count"], 2)
        self.assertEqual(blocked["diagnostics"][0]["redacted_origins"], 2)
        blocked_artifact_decision = blocked["diagnostics"][0]["decision"]
        blocked_artifact_event = blocked["diagnostics"][0]["event"]
        self.assertEqual(blocked["diagnostics"][0]["explain_command"], ["tri", "policy", "explain", blocked_artifact_decision])
        self.assertIn(blocked_artifact_decision, blocked["refs"].get("decisions", []))
        self.assertIn(blocked_artifact_event, blocked["refs"].get("events", []))
        explained_artifact = self.tri("--actor", "agent:b", "policy", "explain", blocked_artifact_decision)
        self.assertEqual(explained_artifact["data"]["event"]["kind"], "policy.denied")
        self.assertEqual(explained_artifact["data"]["decision"]["action"], "artifact.read")
        self.assertEqual(explained_artifact["data"]["decision"]["reason_code"], "policy_denied")
        blocked_config = self.tri("--actor", "agent:b", "artifacts", "read", config_artifact, ok=False)
        self.assertEqual(blocked_config["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_config["diagnostics"][0]["origin_count"], 1)
        self.assertEqual(blocked_config["diagnostics"][0]["redacted_origins"], 1)
        blocked_check = self.tri("--actor", "agent:b", "checks", "inspect", check["data"]["id"], ok=False)
        self.assertEqual(blocked_check["diagnostics"][0]["code"], "policy_denied")
        blocked_check_decision = blocked_check["diagnostics"][0]["decision"]
        blocked_check_event = blocked_check["diagnostics"][0]["event"]
        self.assertEqual(blocked_check["diagnostics"][0]["explain_command"], ["tri", "policy", "explain", blocked_check_decision])
        self.assertIn(blocked_check_decision, blocked_check["refs"].get("decisions", []))
        self.assertIn(blocked_check_event, blocked_check["refs"].get("events", []))
        explained_check = self.tri("--actor", "agent:b", "policy", "explain", blocked_check_decision)
        self.assertEqual(explained_check["data"]["event"]["kind"], "policy.denied")
        self.assertEqual(explained_check["data"]["decision"]["action"], "check.read")
        self.assertEqual(explained_check["data"]["decision"]["reason_code"], "policy_denied")
        blocked_artifact_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", stdout_artifact, ok=False)
        self.assertEqual(blocked_artifact_policy["data"]["action"], "artifact.read")
        self.assertEqual(blocked_artifact_policy["data"]["reason_code"], "policy_denied")
        self.assertEqual(blocked_artifact_policy["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(workspace, json.dumps(blocked_artifact_policy["data"]))
        self.assertNotIn(workspace, blocked_artifact_policy["refs"].get("workspaces", []))
        blocked_check_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", check["data"]["id"], ok=False)
        self.assertEqual(blocked_check_policy["data"]["action"], "check.read")
        self.assertEqual(blocked_check_policy["data"]["reason_code"], "policy_denied")
        self.assertEqual(blocked_check_policy["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(workspace, json.dumps(blocked_check_policy["data"]))
        self.assertNotIn(workspace, blocked_check_policy["refs"].get("workspaces", []))
        check_event = check["events"][0]
        blocked_event_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", check_event, ok=False)
        self.assertEqual(blocked_event_policy["data"]["action"], "event.read")
        self.assertEqual(blocked_event_policy["data"]["reason_code"], "policy_denied")
        self.assertEqual(blocked_event_policy["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(workspace, json.dumps(blocked_event_policy["data"]))
        self.assertNotIn(workspace, blocked_event_policy["refs"].get("workspaces", []))
        blocked_artifact_visibility = self.tri("--actor", "agent:b", "policy", "visibility", stdout_artifact, ok=False)
        self.assertEqual(blocked_artifact_visibility["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(workspace, json.dumps(blocked_artifact_visibility["diagnostics"]))
        blocked_check_visibility = self.tri("--actor", "agent:b", "policy", "visibility", check["data"]["id"], ok=False)
        self.assertEqual(blocked_check_visibility["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(workspace, json.dumps(blocked_check_visibility["diagnostics"]))
        blocked_event_visibility = self.tri("--actor", "agent:b", "policy", "visibility", check_event, ok=False)
        self.assertEqual(blocked_event_visibility["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(workspace, json.dumps(blocked_event_visibility["diagnostics"]))
        listed = self.tri("--actor", "agent:b", "artifacts", "list", "--kind", "stdout")
        self.assertNotIn(stdout_artifact, {item["id"] for item in listed["data"]["artifacts"]})
        self.assertEqual(listed["data"]["redacted"], 1)

        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        allowed_artifact_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", stdout_artifact)
        self.assertTrue(allowed_artifact_policy["data"]["allowed"])
        self.assertEqual(allowed_artifact_policy["data"]["action"], "artifact.read")
        allowed_check_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", check["data"]["id"])
        self.assertTrue(allowed_check_policy["data"]["allowed"])
        self.assertEqual(allowed_check_policy["data"]["action"], "check.read")
        allowed_event_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", check_event)
        self.assertTrue(allowed_event_policy["data"]["allowed"])
        self.assertEqual(allowed_event_policy["data"]["action"], "event.read")
        allowed_artifact_visibility = self.tri("--actor", "agent:b", "policy", "visibility", stdout_artifact)
        self.assert_schema_required_keys("tri.policy.visibility", allowed_artifact_visibility["data"])
        self.assertEqual(allowed_artifact_visibility["data"]["object_type"], "artifact")
        self.assertTrue(allowed_artifact_visibility["data"]["access"]["allowed"])
        self.assertIn(workspace, allowed_artifact_visibility["data"]["parents"]["workspaces"])
        allowed_check_visibility = self.tri("--actor", "agent:b", "policy", "visibility", check["data"]["id"])
        self.assert_schema_required_keys("tri.policy.visibility", allowed_check_visibility["data"])
        self.assertEqual(allowed_check_visibility["data"]["object_type"], "check")
        self.assertEqual(allowed_check_visibility["data"]["parents"]["workspace"], workspace)
        allowed_event_visibility = self.tri("--actor", "agent:b", "policy", "visibility", check_event)
        self.assert_schema_required_keys("tri.policy.visibility", allowed_event_visibility["data"])
        self.assertEqual(allowed_event_visibility["data"]["object_type"], "event")
        self.assertTrue(allowed_event_visibility["data"]["access"]["allowed"])
        allowed_artifact = self.tri("--actor", "agent:b", "artifacts", "read", stdout_artifact)
        self.assertEqual(allowed_artifact["data"]["text"], "agent-log\n")
        self.assertIsNone(allowed_artifact["data"]["path"])
        self.assertEqual(allowed_artifact["data"]["fingerprint"], artifact["data"]["fingerprint"])
        allowed_config = self.tri("--actor", "agent:b", "artifacts", "read", config_artifact)
        self.assertIn("TRI_PROFILE=test", allowed_config["data"]["text"])
        self.assertIsNone(allowed_config["data"]["path"])

        private_workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", private_workspace, "src/secret.txt", "--text", "private\n")
        private_changeset = self.tri("changeset", "create", private_workspace, "--title", "Private artifact origin", "--visibility", "private")["data"]["id"]
        store = self.open_store()
        try:
            ws = store.get_workspace(private_workspace)
            with store.conn:
                store.create_event(
                    kind="artifact.private_origin_linked",
                    actor="actor_local",
                    project_id=ws["project_id"],
                    workspace_id=private_workspace,
                    object_refs={"artifact": stdout_artifact, "changeset": private_changeset},
                )
        finally:
            store.close()

        mixed_origin = self.tri("--actor", "agent:b", "artifacts", "read", stdout_artifact)
        self.assertEqual(mixed_origin["data"]["text"], "agent-log\n")
        self.assertEqual(mixed_origin["data"]["access"]["origin_count"], 3)
        self.assertEqual(mixed_origin["data"]["access"]["redacted_origins"], 1)
        self.assertEqual(len(mixed_origin["data"]["origins"]), 2)
        self.assertEqual({origin["policy_object"] for origin in mixed_origin["data"]["origins"]}, {workspace})
        self.assertNotIn(private_changeset, json.dumps(mixed_origin))
        self.assertNotIn(private_changeset, mixed_origin["refs"].get("changesets", []))
        mixed_visibility = self.tri("--actor", "agent:b", "policy", "visibility", stdout_artifact)
        self.assertEqual(mixed_visibility["data"]["parents"]["redacted_origins"], 1)
        self.assertNotIn(private_changeset, json.dumps(mixed_visibility["data"]))
        self.assertNotIn(private_changeset, mixed_visibility["refs"].get("changesets", []))

        mixed_list = self.tri("--actor", "agent:b", "artifacts", "list", "--kind", "stdout")
        listed_stdout = next(item for item in mixed_list["data"]["artifacts"] if item["id"] == stdout_artifact)
        self.assert_schema_required_keys("tri.artifacts.inspect", listed_stdout)
        self.assertEqual(listed_stdout["fingerprint"], artifact["data"]["fingerprint"])
        self.assertEqual(listed_stdout["access"]["origin_count"], 3)
        self.assertEqual(listed_stdout["access"]["redacted_origins"], 1)
        self.assertEqual(len(listed_stdout["origins"]), 2)
        self.assertEqual({origin["policy_object"] for origin in listed_stdout["origins"]}, {workspace})
        self.assertNotIn(private_changeset, json.dumps(listed_stdout))

        events = self.tri("events", "list", "--workspace", workspace)
        self.assertTrue(events["data"]["events"])
        event = self.tri("events", "inspect", events["data"]["events"][0]["id"])
        self.assertIn("kind", event["data"])

        doctor = self.tri("doctor")
        self.assertGreaterEqual(doctor["data"]["counts"]["events"], 1)

    def test_secret_lease_runs_check_without_leaking_value(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        secret_file = self.root / "token.txt"
        secret_file.write_text("s3cr3t-token")

        secret = self.tri("secrets", "put", "app", "API_TOKEN", "--value-file", str(secret_file))
        self.assertEqual(secret["data"]["name"], "API_TOKEN")
        self.assertNotIn("s3cr3t-token", json.dumps(secret))
        command_names = {item["name"] for item in self.tri("commands")["data"]["commands"]}
        self.assertIn("secrets-redact-logs", command_names)
        self.assertIn("secrets-inspect", command_names)

        listed = self.tri("secrets", "list", "app")
        self.assertEqual(listed["data"]["secrets"][0]["id"], secret["data"]["id"])
        self.assertEqual(listed["data"]["redacted"], 0)
        inspected = self.tri("secrets", "inspect", secret["data"]["id"])
        self.assertEqual(inspected["data"]["fingerprint"], secret["data"]["fingerprint"])

        lease = self.tri("secrets", "request-lease", workspace, "API_TOKEN", "--env", "API_TOKEN", "--ttl-seconds", "60")
        lease_id = lease["data"]["id"]
        self.assert_schema_required_keys("tri.secret_lease.inspect", lease["data"])
        self.assertEqual(lease["data"]["env"], "API_TOKEN")
        self.assertEqual(lease["data"]["actor"], "actor_local")
        self.assertIsNotNone(lease["data"]["expires_at"])
        self.assertIsNone(lease["data"]["revoked_at"])
        generic_lease = self.tri("leases", "inspect", lease_id)["data"]
        self.assert_schema_required_keys("tri.lease.inspect", generic_lease)
        self.assertEqual(generic_lease["kind"], "secret")
        self.assertEqual(generic_lease["schema"], "tri.secret_lease.inspect")
        self.assert_schema_required_keys("tri.secret_lease.inspect", generic_lease["lease"])
        self.assertEqual(generic_lease["lease"]["id"], lease_id)
        blocked_lease = self.tri("--actor", "agent:b", "leases", "inspect", lease_id, ok=False)
        self.assertEqual(blocked_lease["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_lease["diagnostics"][0]["required_grant"], "lease_actor or workspace.write")
        self.assertNotIn(secret["data"]["id"], json.dumps(blocked_lease["diagnostics"]))

        check = self.tri(
            "run",
            workspace,
            "--with-lease",
            lease_id,
            "--",
            "python3",
            "-c",
            "import os; assert os.environ['API_TOKEN'] == 's3cr3t-token'; print(os.environ['API_TOKEN'])",
        )
        self.assertEqual(check["data"]["status"], "passed")
        self.assertEqual(check["data"]["secret_leases"], [lease_id])
        self.assertEqual(check["data"]["environment_policy"]["secret_leases"], [lease_id])
        self.assertEqual(check["data"]["environment_policy"]["secret_env"], ["API_TOKEN"])
        check_started_event = next(
            event
            for event in check["events"]
            if self.tri("events", "inspect", event)["data"]["kind"] == "check.started"
        )
        self.tri("changeset", "create", workspace, "--title", "Secret check evidence", "--visibility", "public")
        public_secret_event = self.tri("--actor", "agent:b", "events", "inspect", check_started_event)
        self.assertIsNone(public_secret_event["data"]["workspace"])
        self.assertTrue(public_secret_event["data"]["redacted_workspace"])
        self.assertTrue(public_secret_event["data"]["object_refs"]["secret_leases"][0]["redacted"])
        self.assertNotIn(lease_id, json.dumps(public_secret_event))

        stdout = self.tri("artifacts", "read", check["data"]["logs"]["stdout"])
        self.assertNotIn("s3cr3t-token", stdout["data"]["text"])
        self.assertIn(f"[tri-secret-redacted:{lease_id}]", stdout["data"]["text"])
        already_redacted = self.tri("secrets", "redact-logs", check["data"]["id"])
        self.assertEqual(already_redacted["data"]["replacement_count"], 0)
        self.assertEqual(already_redacted["data"]["leases"], [lease_id])

        leaked = self.tri("run", workspace, "--", "python3", "-c", "print('s3cr3t-token')")
        repaired = self.tri("secrets", "redact-logs", leaked["data"]["id"], "--lease", lease_id)
        self.assertEqual(repaired["data"]["replacement_count"], 1)
        self.assertEqual(repaired["data"]["redacted_artifacts"][0]["artifact"], leaked["data"]["logs"]["stdout"])
        repaired_stdout = self.tri("artifacts", "read", leaked["data"]["logs"]["stdout"])
        self.assertNotIn("s3cr3t-token", repaired_stdout["data"]["text"])
        self.assertIn(f"[tri-secret-redacted:{lease_id}]", repaired_stdout["data"]["text"])

        files = self.tri("files", "list", workspace)["data"]["files"]
        self.assertNotIn(".env", {item["path"] for item in files})

        audit = self.tri("secrets", "audit", "--workspace", workspace)
        self.assertEqual([event["kind"] for event in audit["data"]["events"]], ["secret.logs_redacted", "secret.logs_redacted", "secret.lease_granted"])

        revoked = self.tri("secrets", "revoke", lease_id)
        self.assertEqual(revoked["data"]["status"], "revoked")
        blocked = self.tri("run", workspace, "--with-lease", lease_id, "--", "python3", "-c", "print('blocked')", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "policy_denied")

    def test_materialized_workspace_sync_imports_filesystem_edits(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        mount = self.root / "mounted"
        mounted = self.tri("workspace", "mount", workspace, str(mount), "--ttl-seconds", "60", "--ignore-path", "tmp/**")["data"]
        lease = mounted["lease"]
        self.assertEqual(mounted["materialization"]["workspace"], workspace)
        self.assert_schema_required_keys("tri.workspace.materialization_lease", mounted["materialization"])
        self.assertEqual(mounted["materialization"]["ignored_paths"], ["tmp/**"])
        self.assertEqual(mounted["materialization"]["status"], "active")

        inspected_lease = self.tri("workspace", "lease-inspect", lease)["data"]
        self.assert_schema_required_keys("tri.workspace.materialization_lease", inspected_lease)
        self.assertEqual(inspected_lease["path"], str(mount.resolve()))
        self.assertEqual(inspected_lease["mounted_snapshot"], mounted["snapshot"])
        self.assertEqual(inspected_lease["last_sync_snapshot"], mounted["snapshot"])
        generic_lease = self.tri("leases", "inspect", lease)["data"]
        self.assert_schema_required_keys("tri.lease.inspect", generic_lease)
        self.assertEqual(generic_lease["kind"], "materialization")
        self.assertEqual(generic_lease["schema"], "tri.workspace.materialization_lease")
        self.assertEqual(generic_lease["lease"]["id"], lease)
        self.assertEqual(generic_lease["lease"]["workspace"], workspace)
        listed_leases = self.tri("workspace", "leases", workspace)["data"]["leases"]
        self.assertEqual([item["id"] for item in listed_leases], [lease])
        inspected_workspace = self.tri("workspace", "inspect", workspace)["data"]
        self.assert_schema_required_keys("tri.workspace.inspect", inspected_workspace)
        self.assertEqual(inspected_workspace["materializations"][0]["id"], lease)

        (mount / "src" / "app.txt").write_text("mounted edit\n")
        (mount / "tmp").mkdir()
        (mount / "tmp" / "ignored.txt").write_text("ignored\n")

        sync = self.tri("workspace", "sync", workspace, "--lease", lease)
        self.assertEqual(sync["data"]["paths"], ["src/app.txt"])
        self.assertEqual(sync["data"]["lease"], lease)

        read = self.tri("files", "read", workspace, "src/app.txt")
        self.assertEqual(read["data"]["text"], "mounted edit\n")
        files = {item["path"] for item in self.tri("files", "list", workspace)["data"]["files"]}
        self.assertNotIn("tmp/ignored.txt", files)

        updated_lease = self.tri("workspace", "lease-inspect", lease)["data"]
        self.assert_schema_required_keys("tri.workspace.materialization_lease", updated_lease)
        self.assertEqual(updated_lease["last_sync_snapshot"], sync["data"]["snapshot"])
        revoked = self.tri("workspace", "lease-revoke", lease)["data"]
        self.assert_schema_required_keys("tri.workspace.materialization_lease", revoked)
        self.assertEqual(revoked["status"], "revoked")
        blocked = self.tri("workspace", "sync", workspace, "--lease", lease, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "stale_materialization")
        self.assertEqual(blocked["diagnostics"][0]["status"], "revoked")

    def test_implicit_materialization_sync_uses_actor_lease(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.write")

        mount_a = self.root / "mounted-agent-a"
        lease_a = self.tri("--actor", "agent:a", "workspace", "mount", workspace, str(mount_a))["data"]["lease"]
        mount_b = self.root / "mounted-agent-b"
        lease_b = self.tri("--actor", "agent:b", "workspace", "mount", workspace, str(mount_b))["data"]["lease"]

        (mount_a / "src" / "app.txt").write_text("agent a mount edit\n")
        sync_a = self.tri("--actor", "agent:a", "workspace", "sync", workspace)
        self.assertEqual(sync_a["data"]["lease"], lease_a)
        self.assertEqual(sync_a["data"]["paths"], ["src/app.txt"])
        self.assertEqual(self.tri("--actor", "agent:a", "files", "read", workspace, "src/app.txt")["data"]["text"], "agent a mount edit\n")

        ambiguous = self.tri("workspace", "sync", workspace, ok=False)
        self.assertEqual(ambiguous["diagnostics"][0]["code"], "stale_materialization")
        self.assertIn(lease_a, ambiguous["diagnostics"][0]["leases"])
        self.assertIn(lease_b, ambiguous["diagnostics"][0]["leases"])

    @unittest.skipIf(sys.platform == "win32", "symlink behavior differs on Windows")
    def test_materialized_workspace_rejects_symlink_mount_and_sync(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]

        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep me\n")
        symlink_mount = self.root / "mount-link"
        symlink_mount.symlink_to(outside, target_is_directory=True)

        blocked_mount = self.tri("workspace", "mount", workspace, str(symlink_mount), ok=False)
        self.assertEqual(blocked_mount["diagnostics"][0]["code"], "path_escaped_mount")
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_text(), "keep me\n")

        mount = self.root / "mounted-safe"
        lease = self.tri("workspace", "mount", workspace, str(mount))["data"]["lease"]
        (mount / "src" / "outside-link.txt").symlink_to(sentinel)
        blocked_sync = self.tri("workspace", "sync", workspace, "--lease", lease, ok=False)
        self.assertEqual(blocked_sync["diagnostics"][0]["code"], "path_escaped_mount")
        self.assertEqual(self.tri("files", "read", workspace, "src/app.txt")["data"]["text"], "one\n")

    def test_materialized_workspace_sync_rejects_stale_mount(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        mount = self.root / "stale-mounted"
        self.tri("workspace", "mount", workspace, str(mount))
        mounted = self.tri("workspace", "inspect", workspace)["data"]["materializations"][0]

        self.tri("files", "write", workspace, "README.md", "--text", "tri-side edit\n")
        head = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]
        (mount / "src" / "app.txt").write_text("mounted edit\n")

        stale = self.tri("workspace", "sync", workspace, ok=False)
        diagnostic = stale["diagnostics"][0]
        self.assertEqual(diagnostic["code"], "stale_materialization")
        self.assertEqual(diagnostic["expected"], mounted["last_sync_snapshot"])
        self.assertEqual(diagnostic["actual"], head)
        self.assertTrue(diagnostic["retryable"])
        self.assertEqual(self.tri("files", "read", workspace, "src/app.txt")["data"]["text"], "one\n")

    def test_workspace_update_merges_target_change_or_reports_conflict(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", workspace, "README.md", "--text", "workspace edit\n")

        (self.repo / "src" / "app.txt").write_text("target edit\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "target edit"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.tri("targets", "refresh", "app", "main")

        update = self.tri("workspace", "update", workspace, "--from", "main")
        self.assertIn("src/app.txt", update["data"]["changed_paths"])
        self.assertEqual(self.tri("files", "read", workspace, "README.md")["data"]["text"], "workspace edit\n")
        self.assertEqual(self.tri("files", "read", workspace, "src/app.txt")["data"]["text"], "target edit\n")

    def test_workspace_update_fails_if_workspace_head_moves_at_commit(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", workspace, "README.md", "--text", "workspace edit\n")
        before_update = self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"]

        (self.repo / "src" / "app.txt").write_text("target edit\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "target edit"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.tri("targets", "refresh", "app", "main")
        stale_precondition = self.tri("workspace", "update", workspace, "--from", "main", "--expect-head", imported["data"]["snapshot"]["id"], ok=False)
        self.assertEqual(stale_precondition["diagnostics"][0]["code"], "stale_workspace_head")
        self.assertEqual(stale_precondition["diagnostics"][0]["actual"], before_update)

        store = self.open_store()
        try:
            self.install_head_race(store, workspace)
            with self.assertRaises(TriError) as caught:
                cmd_workspace_update(
                    store,
                    argparse.Namespace(workspace=workspace, from_ref="main", actor="actor_local"),
                    ["tri", "workspace", "update", workspace, "--from", "main"],
                    time.time(),
                )
            self.assertEqual(caught.exception.diagnostic["code"], "stale_workspace_head")
            self.assertEqual(caught.exception.diagnostic["expected"], before_update)
        finally:
            store.close()
        self.assertEqual(self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"], before_update)
        self.assertEqual(self.tri("files", "read", workspace, "src/app.txt")["data"]["text"], "one\n")

    def test_workspace_update_reports_conflict_for_same_path_edits(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "workspace edit\n")

        (self.repo / "src" / "app.txt").write_text("target edit\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "target edit"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.tri("targets", "refresh", "app", "main")

        conflict = self.tri("workspace", "update", workspace, "--from", "main", ok=False)
        self.assertEqual(conflict["diagnostics"][0]["code"], "conflict_required")
        conflict_id = conflict["data"]["conflict"]
        self.assertEqual(conflict["diagnostics"][0]["conflict"], conflict_id)
        self.assertEqual(conflict["diagnostics"][0]["conflicts"][0]["path"], "src/app.txt")

        listed = self.tri("conflicts", "list", "--workspace", workspace)
        self.assertEqual(listed["data"]["conflicts"][0]["id"], conflict_id)
        self.assertEqual(listed["data"]["redacted"], 0)
        inspected = self.tri("conflicts", "inspect", conflict_id)
        self.assertEqual(inspected["data"]["status"], "open")
        conflict_entry = inspected["data"]["conflicts"][0]
        self.assertEqual(conflict_entry["workspace_blob"], conflict["data"]["conflicts"][0]["workspace_blob"])
        self.assertEqual(conflict_entry["kind"], "file_text_conflict")
        self.assertIn("conflicting_objects", conflict_entry)
        self.assertEqual(conflict_entry["conflicting_objects"]["workspace"]["blob"], conflict_entry["workspace_blob"])
        self.assertIn("actor_local", conflict_entry["involved_actors"])
        self.assertIn("human:tri_example_com", conflict_entry["involved_actors"])
        self.assertEqual(conflict_entry["involved_operations"][0]["kind"], "write_file")
        self.assertEqual(conflict_entry["involved_operations"][0]["paths"], ["src/app.txt"])
        self.assertEqual(
            {item["strategy"] for item in conflict_entry["proposed_resolutions"]},
            {"ours", "theirs", "base"},
        )
        self.assertEqual(
            conflict_entry["proposed_resolutions"][1]["command"],
            ["tri", "conflicts", "resolve", conflict_id, "--strategy", "theirs"],
        )
        self.assertEqual(
            {item["action"] for item in conflict_entry["required_policy_checks"]},
            {"workspace.read", "workspace.write"},
        )
        proc, port = self.start_server()
        try:
            api_workspace_conflicts = self.api_get(port, f"/v0/workspaces/{workspace}/conflicts?status=open")
            self.assertEqual(api_workspace_conflicts["data"]["conflicts"][0]["id"], conflict_id)
            api_conflicts = self.api_get(port, f"/v0/conflicts?workspace={workspace}&status=open")
            self.assertEqual(api_conflicts["data"]["conflicts"][0]["id"], conflict_id)
            api_conflict = self.api_get(port, f"/v0/conflicts/{conflict_id}")
            self.assertEqual(api_conflict["data"]["id"], conflict_id)
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)

        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'workspace edit\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Conflicted source")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "approve", review)
        blocked_policy = self.tri("policy", "evaluate", "export", changeset, ok=False)
        self.assertEqual(blocked_policy["diagnostics"][0]["code"], "unresolved_conflict")
        self.assertIn("policy_no_unresolved_conflicts", blocked_policy["data"]["policies"])
        self.assertEqual(blocked_policy["data"]["reason_code"], "unresolved_conflict")
        self.assertEqual(blocked_policy["data"]["unresolved_conflicts"][0]["id"], conflict_id)
        blocked_export = self.tri("export", "git-patch", changeset, "--override-reason", "still blocked", ok=False)
        self.assertEqual(blocked_export["diagnostics"][0]["code"], "unresolved_conflict")

        hidden_list = self.tri("--actor", "agent:b", "conflicts", "list", "--workspace", workspace)
        self.assertEqual(hidden_list["data"]["conflicts"], [])
        self.assertEqual(hidden_list["data"]["redacted"], 1)
        hidden_inspect = self.tri("--actor", "agent:b", "conflicts", "inspect", conflict_id, ok=False)
        self.assertEqual(hidden_inspect["diagnostics"][0]["code"], "policy_denied")

        self.tri("policy", "grant", workspace, "--principal", "actor:agent:b", "--action", "workspace.read")
        visible_list = self.tri("--actor", "agent:b", "conflicts", "list", "--workspace", workspace)
        self.assertEqual(visible_list["data"]["conflicts"][0]["id"], conflict_id)
        visible_inspect = self.tri("--actor", "agent:b", "conflicts", "inspect", conflict_id)
        self.assertEqual(visible_inspect["data"]["id"], conflict_id)

        resolved = self.tri("conflicts", "resolve", conflict_id, "--strategy", "theirs")
        self.assertEqual(resolved["data"]["strategy"], "theirs")
        self.assertEqual(self.tri("files", "read", workspace, "src/app.txt")["data"]["text"], "target edit\n")
        self.assertEqual(self.tri("conflicts", "inspect", conflict_id)["data"]["status"], "resolved")
        allowed_after_resolution = self.tri("policy", "evaluate", "export", changeset)
        self.assertTrue(allowed_after_resolution["data"]["allowed"])
        self.assertEqual(allowed_after_resolution["data"]["unresolved_conflicts"], [])

    def test_workspace_conflicts_redact_hidden_paths(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target, "--hide-path", "private/**")["data"]["id"]
        self.tri("files", "write", workspace, "private/token.txt", "--text", "workspace secret\n")

        (self.repo / "private").mkdir()
        (self.repo / "private" / "token.txt").write_text("target secret\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "target secret"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.tri("targets", "refresh", "app", target)

        conflict = self.tri("workspace", "update", workspace, "--from", target, ok=False)
        self.assertEqual(conflict["diagnostics"][0]["code"], "conflict_required")
        self.assertNotIn("private/token.txt", json.dumps(conflict))
        conflict_id = conflict["data"]["conflict"]
        entry = conflict["data"]["conflicts"][0]
        self.assertEqual(entry["kind"], "path_visibility_conflict")
        self.assertTrue(entry["redacted"])
        self.assertEqual(entry["redaction"], "hidden_path")
        self.assertIsNone(entry["path"]["path"])
        self.assertNotIn("workspace_blob", entry)
        self.assertNotIn("conflicting_objects", entry)
        self.assertEqual(entry["proposed_resolutions"][1]["command"], ["tri", "conflicts", "resolve", conflict_id, "--strategy", "theirs"])

        inspected = self.tri("conflicts", "inspect", conflict_id)
        self.assertNotIn("private/token.txt", json.dumps(inspected))
        self.assertTrue(inspected["data"]["conflicts"][0]["redacted"])
        unresolved = self.tri("policy", "evaluate", "export", self.tri("changeset", "create", workspace, "--title", "Hidden conflict source")["data"]["id"], ok=False)
        self.assertNotIn("private/token.txt", json.dumps(unresolved))
        self.assertTrue(unresolved["data"]["unresolved_conflicts"][0]["conflicts"][0]["redacted"])

        resolved = self.tri("conflicts", "resolve", conflict_id, "--strategy", "theirs")
        self.assertEqual(resolved["data"]["strategy"], "theirs")
        self.assertEqual(self.tri("conflicts", "inspect", conflict_id)["data"]["status"], "resolved")

    def test_workspace_conflicts_classify_secret_like_paths(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", workspace, ".env", "--text", "TOKEN=workspace\n")

        (self.repo / ".env").write_text("TOKEN=target\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "target env"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.tri("targets", "refresh", "app", target)

        conflict = self.tri("workspace", "update", workspace, "--from", target, ok=False)
        self.assertEqual(conflict["diagnostics"][0]["code"], "conflict_required")
        self.assertNotIn(".env", json.dumps(conflict))
        entry = conflict["data"]["conflicts"][0]
        self.assertEqual(entry["kind"], "secret_export_conflict")
        self.assertTrue(entry["redacted"])
        self.assertEqual(entry["redaction"], "secret_like_path")
        self.assertNotIn("workspace_blob", entry)
        conflict_id = conflict["data"]["conflict"]
        inspected = self.tri("conflicts", "inspect", conflict_id)
        self.assertNotIn(".env", json.dumps(inspected))
        self.assertEqual(inspected["data"]["conflicts"][0]["kind"], "secret_export_conflict")

    def test_conflict_resolve_fails_if_workspace_head_moves_at_commit(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "workspace edit\n")

        (self.repo / "src" / "app.txt").write_text("target edit\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "target edit"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.tri("targets", "refresh", "app", "main")

        conflict = self.tri("workspace", "update", workspace, "--from", "main", ok=False)
        conflict_id = conflict["data"]["conflict"]
        conflict_head = self.tri("conflicts", "inspect", conflict_id)["data"]["head_snapshot"]

        store = self.open_store()
        try:
            self.install_head_race(store, workspace)
            with self.assertRaises(TriError) as caught:
                cmd_conflicts_resolve(
                    store,
                    argparse.Namespace(conflict=conflict_id, strategy="theirs", actor="actor_local"),
                    ["tri", "conflicts", "resolve", conflict_id, "--strategy", "theirs"],
                    time.time(),
                )
            self.assertEqual(caught.exception.diagnostic["code"], "stale_workspace_head")
            self.assertEqual(caught.exception.diagnostic["expected"], conflict_head)
        finally:
            store.close()
        self.assertEqual(self.tri("workspace", "inspect", workspace)["data"]["head_snapshot"], conflict_head)
        self.assertEqual(self.tri("conflicts", "inspect", conflict_id)["data"]["status"], "open")

    def test_task_attempts_changeset_compare_and_review_inspect(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        task = self.tri("task", "create", "app", "--title", "Three agent attempts")["data"]["id"]
        attempts_spec = [
            ("agent:a", "src/app.txt", "attempt a\n"),
            ("agent:b", "README.md", "attempt b\n"),
            ("agent:c", "docs/agent-c.txt", "attempt c\n"),
        ]
        workspaces = []
        changesets = []
        for actor, path, text in attempts_spec:
            workspace = self.tri("--actor", actor, "workspace", "create", "app", "--from", target, "--task", task)["data"]["id"]
            inspected = self.tri("--actor", actor, "workspace", "inspect", workspace)["data"]
            self.assertEqual(inspected["base_snapshot"], imported["data"]["snapshot"]["id"])
            self.tri("--actor", actor, "files", "write", workspace, path, "--text", text)
            self.tri("--actor", actor, "run", workspace, "--", "python3", "-c", f"from pathlib import Path; assert Path({path!r}).read_text() == {text!r}")
            changeset = self.tri("--actor", actor, "changeset", "create", workspace, "--title", f"Attempt {actor}")["data"]["id"]
            workspaces.append(workspace)
            changesets.append(changeset)

        attempts = self.tri("task", "attempts", task)
        self.assertEqual({item["id"] for item in attempts["data"]["workspaces"]}, set(workspaces))
        self.assertEqual({item["id"] for item in attempts["data"]["changesets"]}, set(changesets))
        self.assertEqual(len(attempts["data"]["runs"]), 3)

        comparison = self.tri("changeset", "compare", *changesets)
        self.assertEqual([item["changeset"] for item in comparison["data"]["changesets"]], changesets)
        self.assertEqual(comparison["data"]["redacted"], 0)

        agent_comparison = self.tri("--actor", "agent:a", "changeset", "compare", *changesets)
        self.assertEqual([item["changeset"] for item in agent_comparison["data"]["changesets"]], [changesets[0]])
        self.assertEqual(agent_comparison["data"]["redacted"], 2)
        self.assertNotIn(changesets[1], json.dumps(agent_comparison["data"]))
        self.assertNotIn(changesets[1], agent_comparison["refs"].get("changesets", []))
        self.assertNotIn(changesets[2], json.dumps(agent_comparison["data"]))
        self.assertNotIn(changesets[2], agent_comparison["refs"].get("changesets", []))
        self.assertNotIn("Attempt agent:b", json.dumps(agent_comparison))
        self.assertNotIn("README.md", json.dumps(agent_comparison["data"]))

        self.tri("policy", "grant", changesets[1], "--principal", "actor:agent:a", "--action", "changeset.read")
        granted_comparison = self.tri("--actor", "agent:a", "changeset", "compare", *changesets)
        self.assertEqual([item["changeset"] for item in granted_comparison["data"]["changesets"]], changesets[:2])
        self.assertEqual(granted_comparison["data"]["redacted"], 1)
        self.assertNotIn(changesets[2], granted_comparison["refs"].get("changesets", []))

        review = self.tri("review", "create", changesets[0], "--reviewers", "human:reviewer")["data"]["id"]
        inspected = self.tri("review", "inspect", review)
        self.assertEqual(inspected["data"]["changeset"], changesets[0])
        self.assertEqual(inspected["data"]["reviewers"], ["human:reviewer"])

    def test_public_tasks_allow_agent_attempts_without_lifecycle_control(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        task = self.tri("task", "create", "app", "--title", "Agent attempts")["data"]

        run_decision = self.tri("--actor", "agent:a", "policy", "evaluate", "access", task["id"], "--action", "task.run")
        self.assertTrue(run_decision["data"]["allowed"])
        self.assertEqual(run_decision["data"]["reason"], "public_task_run")

        workspace_response = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", target, "--task", task["id"])
        workspace = workspace_response["data"]["id"]
        workspace_event = workspace_response["events"][0]
        run_id = self.tri("--actor", "agent:a", "workspace", "inspect", workspace)["data"]["task_run"]
        agent_a_task = self.tri("--actor", "agent:a", "task", "inspect", task["id"])["data"]
        self.assert_schema_required_keys("tri.task.inspect", agent_a_task)
        self.assertEqual(agent_a_task["workspaces"], [workspace])
        self.assertEqual(agent_a_task["runs"], [run_id])

        agent_b_task = self.tri("--actor", "agent:b", "task", "inspect", task["id"])["data"]
        self.assert_schema_required_keys("tri.task.inspect", agent_b_task)
        self.assertEqual(agent_b_task["workspaces"], [])
        self.assertEqual(agent_b_task["runs"], [])
        self.assertEqual(agent_b_task["changesets"], [])
        self.assertGreaterEqual(agent_b_task["redacted"], 2)
        self.assertNotIn(workspace, json.dumps(agent_b_task))

        agent_b_attempts = self.tri("--actor", "agent:b", "task", "attempts", task["id"])["data"]
        self.assert_schema_required_keys("tri.task.inspect", agent_b_attempts["task"])
        self.assertEqual(agent_b_attempts["workspaces"], [])
        self.assertEqual(agent_b_attempts["runs"], [])
        self.assertNotIn(workspace, json.dumps(agent_b_attempts))
        self.assertNotIn(run_id, json.dumps(agent_b_attempts))
        agent_b_run = self.tri("--actor", "agent:b", "task", "run-inspect", run_id)["data"]
        self.assert_schema_required_keys("tri.task_run.inspect", agent_b_run)
        self.assertEqual(agent_b_run["id"], run_id)
        self.assertIsNone(agent_b_run["workspace"])
        self.assertTrue(agent_b_run["redacted_workspace"])
        agent_b_events = self.tri("--actor", "agent:b", "events", "list", "--workspace", workspace)
        self.assertEqual(agent_b_events["data"]["events"], [])
        self.assertGreaterEqual(agent_b_events["data"]["redacted"], 1)
        blocked_workspace_event = self.tri("--actor", "agent:b", "events", "inspect", workspace_event, ok=False)
        self.assertEqual(blocked_workspace_event["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(workspace, blocked_workspace_event["refs"].get("workspaces", []))
        agent_b_run_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", run_id)
        self.assertTrue(agent_b_run_policy["data"]["allowed"])
        self.assertEqual(agent_b_run_policy["data"]["action"], "task_run.read")
        self.assertEqual(agent_b_run_policy["data"]["reason"], "task_access")
        self.assertIsNone(agent_b_run_policy["data"]["workspace"])
        self.assertTrue(agent_b_run_policy["data"]["redacted_workspace"])
        self.assertNotIn(workspace, json.dumps(agent_b_run_policy["data"]))
        self.assertNotIn(workspace, agent_b_run_policy["refs"].get("workspaces", []))
        agent_b_run_visibility = self.tri("--actor", "agent:b", "policy", "visibility", run_id)
        self.assertEqual(agent_b_run_visibility["data"]["object"], run_id)
        self.assertEqual(agent_b_run_visibility["data"]["object_type"], "task_run")
        self.assertIsNone(agent_b_run_visibility["data"]["workspace"])
        self.assertTrue(agent_b_run_visibility["data"]["parents"]["workspace_redacted"])
        self.assertEqual(agent_b_run_visibility["data"]["task"], task["id"])
        self.assertEqual(agent_b_run_visibility["data"]["access"]["reason"], "task_access")
        self.assertNotIn(workspace, json.dumps(agent_b_run_visibility["data"]))
        self.assertNotIn(workspace, agent_b_run_visibility["refs"].get("workspaces", []))
        private_workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        private_run = self.tri("workspace", "inspect", private_workspace)["data"]["task_run"]
        private_run_policy = self.tri("--actor", "agent:b", "policy", "evaluate", "access", private_run, ok=False)
        self.assertFalse(private_run_policy["data"]["allowed"])
        self.assertEqual(private_run_policy["data"]["action"], "task_run.read")
        self.assertEqual(private_run_policy["data"]["reason_code"], "policy_denied")
        self.assertEqual(private_run_policy["diagnostics"][0]["code"], "policy_denied")
        self.assertIsNone(private_run_policy["data"]["workspace"])
        self.assertTrue(private_run_policy["data"]["redacted_workspace"])
        self.assertNotIn(private_workspace, json.dumps(private_run_policy["data"]))
        self.assertNotIn(private_workspace, private_run_policy["refs"].get("workspaces", []))
        private_run_visibility = self.tri("--actor", "agent:b", "policy", "visibility", private_run, ok=False)
        self.assertEqual(private_run_visibility["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(private_run_visibility["diagnostics"][0]["action"], "task_run.read")
        self.assertNotIn(private_workspace, json.dumps(private_run_visibility))
        self.assertNotIn(private_workspace, private_run_visibility["refs"].get("workspaces", []))
        blocked_private_run = self.tri("--actor", "agent:b", "task", "run-inspect", private_run, ok=False)
        blocked_private_run_diagnostic = blocked_private_run["diagnostics"][0]
        self.assertEqual(blocked_private_run_diagnostic["code"], "policy_denied")
        self.assertEqual(blocked_private_run_diagnostic["action"], "task_run.read")
        self.assertEqual(blocked_private_run_diagnostic["required_grant"], "workspace.read")
        self.assertEqual(blocked_private_run_diagnostic["explain_command"], ["tri", "policy", "explain", blocked_private_run_diagnostic["decision"]])
        self.assertIn(blocked_private_run_diagnostic["decision"], blocked_private_run["refs"].get("decisions", []))
        self.assertIn(blocked_private_run_diagnostic["event"], blocked_private_run["refs"].get("events", []))
        self.assertNotIn(private_workspace, blocked_private_run["refs"].get("workspaces", []))
        explained_private_run = self.tri("--actor", "agent:b", "policy", "explain", blocked_private_run_diagnostic["decision"])
        self.assertEqual(explained_private_run["data"]["event"]["kind"], "policy.denied")
        self.assertEqual(explained_private_run["data"]["decision"]["action"], "task_run.read")
        self.assertEqual(explained_private_run["data"]["decision"]["reason_code"], "policy_denied")
        self.assertEqual(explained_private_run["data"]["decision"]["required_grant"], "workspace.read")
        self.assertNotIn(private_workspace, json.dumps(explained_private_run))
        blocked_block = self.tri("--actor", "agent:a", "task", "block", task["id"], "--reason", "not my lifecycle", ok=False)
        self.assertEqual(blocked_block["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_block["diagnostics"][0]["required_grant"], "task.write")

        blocked_complete = self.tri("--actor", "agent:b", "task", "complete-run", run_id, "--recommendation", "steal run", ok=False)
        self.assertEqual(blocked_complete["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_complete["diagnostics"][0]["required_grant"], "task.write")

        completed = self.tri("--actor", "agent:a", "task", "complete-run", run_id, "--recommendation", "Use agent A.")
        self.assertEqual(completed["data"]["status"], "completed")
        self.assertEqual(self.tri("task", "inspect", task["id"])["data"]["status"], "reviewing")

        blocked_accept = self.tri("--actor", "agent:a", "task", "accept", task["id"], "--no-change", "no patch needed", ok=False)
        self.assertEqual(blocked_accept["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_accept["diagnostics"][0]["required_grant"], "task.write")

        accepted = self.tri("task", "accept", task["id"], "--no-change", "no patch needed")["data"]
        self.assertEqual(accepted["status"], "accepted")

    def test_task_scoped_actor_can_only_access_its_task_work(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        task = self.tri("task", "create", "app", "--title", "Scoped task")["data"]
        other_task = self.tri("task", "create", "app", "--title", "Other task")["data"]
        actor = f"task:{task['id']}"

        run_decision = self.tri("--actor", actor, "policy", "evaluate", "access", task["id"], "--action", "task.run")
        self.assertTrue(run_decision["data"]["allowed"])
        self.assertEqual(run_decision["data"]["reason"], "task_scope")
        self.assertIn(f"task:{task['id']}", run_decision["data"]["actor_selectors"])

        other_run_decision = self.tri("--actor", actor, "policy", "evaluate", "access", other_task["id"], "--action", "task.run", ok=False)
        self.assertFalse(other_run_decision["data"]["allowed"])
        self.assertEqual(other_run_decision["data"]["reason_code"], "policy_denied")

        blocked_task_create = self.tri("--actor", actor, "task", "create", "app", "--title", "Spawn unrelated task", ok=False)
        self.assertEqual(blocked_task_create["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_task_create["diagnostics"][0]["required_grant"], "non_task_scoped_actor")

        blocked_import = self.tri("--actor", actor, "import", str(self.repo), "--name", "spawned", ok=False)
        self.assertEqual(blocked_import["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_import["diagnostics"][0]["required_grant"], "non_task_scoped_actor")

        blocked_unscoped = self.tri("--actor", actor, "workspace", "create", "app", "--from", target, ok=False)
        self.assertEqual(blocked_unscoped["diagnostics"][0]["code"], "policy_denied")
        blocked_other_task = self.tri("--actor", actor, "workspace", "create", "app", "--from", target, "--task", other_task["id"], ok=False)
        self.assertEqual(blocked_other_task["diagnostics"][0]["code"], "policy_denied")

        workspace = self.tri("--actor", actor, "workspace", "create", "app", "--from", target, "--task", task["id"])["data"]["id"]
        self.tri("--actor", actor, "files", "write", workspace, "src/app.txt", "--text", "task scoped edit\n")
        changeset = self.tri("--actor", actor, "changeset", "create", workspace, "--title", "Task scoped change")["data"]["id"]
        self.assertEqual(self.tri("--actor", actor, "changeset", "inspect", changeset)["data"]["id"], changeset)

        other_workspace = self.tri("workspace", "create", "app", "--from", target, "--task", other_task["id"], "--visibility", "public")["data"]["id"]
        self.tri("files", "write", other_workspace, "README.md", "--text", "public but wrong task\n")
        other_changeset = self.tri("changeset", "create", other_workspace, "--title", "Other public change", "--visibility", "public")["data"]["id"]
        blocked_workspace_read = self.tri("--actor", actor, "workspace", "inspect", other_workspace, ok=False)
        self.assertEqual(blocked_workspace_read["diagnostics"][0]["code"], "policy_denied")
        blocked_changeset_read = self.tri("--actor", actor, "changeset", "inspect", other_changeset, ok=False)
        self.assertEqual(blocked_changeset_read["diagnostics"][0]["code"], "policy_denied")

    def test_changeset_split_previews_path_and_operation_ranges(self) -> None:
        imported = self.import_project()
        workspace = self.tri(
            "workspace",
            "create",
            "app",
            "--from",
            imported["data"]["target"]["name"],
            "--hide-path",
            "private/**",
        )["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "split src\n")
        self.tri("files", "write", workspace, "README.md", "--text", "split readme\n")
        self.tri("files", "write", workspace, "private/notes.txt", "--text", "hidden split\n")
        changeset = self.tri("changeset", "create", workspace, "--title", "Split me")["data"]
        operations = changeset["operations"]

        path_split = self.tri("changeset", "split", changeset["id"], "--paths", "src/**")
        self.assertEqual(path_split["data"]["selector"], "paths")
        self.assertEqual(path_split["data"]["patterns"], ["src/**"])
        self.assertEqual(path_split["data"]["paths"], ["src/app.txt"])
        self.assertEqual([op["id"] for op in path_split["data"]["operations"]], [operations[0]])

        range_split = self.tri("changeset", "split", changeset["id"], "--operations", "2..3")
        self.assertEqual(range_split["data"]["selector"], "operations")
        self.assertEqual(range_split["data"]["operation_selector"], "2..3")
        self.assertEqual([op["index"] for op in range_split["data"]["operations"]], [2, 3])
        self.assertEqual([op["id"] for op in range_split["data"]["operations"]], operations[1:3])
        self.assertEqual(range_split["data"]["paths"], ["README.md"])
        self.assertEqual(range_split["data"]["redacted_paths"], 1)

        id_split = self.tri("changeset", "split", changeset["id"], "--operations", operations[0])
        self.assertEqual(id_split["data"]["paths"], ["src/app.txt"])

        applied = self.tri("changeset", "split", changeset["id"], "--paths", "src/**", "--apply", "--title", "Split src only")
        split_id = applied["data"]["split_changeset"]
        self.assertEqual(applied["data"]["mode"], "applied")
        self.assertEqual(applied["data"]["created"]["title"], "Split src only")
        self.assertEqual(applied["data"]["created"]["operations"], [operations[0]])
        self.assertEqual(applied["data"]["created"]["touched_paths"], ["src/app.txt"])
        self.assertEqual(applied["data"]["created"]["lifecycle"]["split_from"], changeset["id"])
        self.assertEqual(applied["data"]["created"]["lifecycle"]["source_operations"], [operations[0]])
        split_diff = self.tri("diff", split_id, "--mode", "machine")
        self.assertEqual(split_diff["data"]["summary"]["modified"], 1)
        self.assertEqual([item["path"] for item in split_diff["data"]["files"] if item["visible"]], ["src/app.txt"])
        split_event = self.tri("events", "inspect", applied["events"][0])
        self.assertEqual(split_event["data"]["kind"], "changeset.split_created")
        self.assertEqual(split_event["data"]["object_refs"]["changeset"], split_id)

        invalid = self.tri("changeset", "split", changeset["id"], "--operations", "0", ok=False)
        self.assertEqual(invalid["diagnostics"][0]["code"], "invalid_command")

    def test_task_specs_required_checks_and_final_recommendation(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        task = self.tri(
            "task",
            "create",
            "app",
            "--title",
            "Task spec",
            "--body",
            "Make a small edit.",
            "--acceptance",
            "src/app.txt changes",
            "--required-check",
            "python3 -c pass",
        )["data"]
        self.assertEqual(task["acceptance_criteria"], ["src/app.txt changes"])
        self.assertEqual(task["required_checks"][0]["command"], ["python3", "-c", "pass"])

        inspected_task = self.tri("task", "inspect", task["id"])
        self.assertEqual(inspected_task["data"]["body"], "Make a small edit.")

        workspace = self.tri("workspace", "create", "app", "--from", target, "--task", task["id"])["data"]["id"]
        started_attempts = self.tri("task", "attempts", task["id"])
        self.assertEqual(started_attempts["data"]["runs"][0]["status"], "started")
        running_task = self.tri("task", "inspect", task["id"])["data"]
        self.assertEqual(running_task["status"], "running")
        self.assertEqual(running_task["lifecycle"]["transition"], "open->running")
        blocked_task = self.tri("task", "block", task["id"], "--reason", "Waiting for fixture")["data"]
        self.assertEqual(blocked_task["status"], "blocked")
        self.assertEqual(blocked_task["lifecycle"]["reason"], "Waiting for fixture")
        resumed_task = self.tri("task", "resume", task["id"], "--reason", "Fixture ready")["data"]
        self.assertEqual(resumed_task["status"], "running")
        self.assertEqual(resumed_task["transition"], "blocked->running")
        run_id = self.tri("workspace", "inspect", workspace)["data"]["task_run"]
        inspected_run = self.tri("task", "run-inspect", run_id)
        self.assert_schema_required_keys("tri.task_run.inspect", inspected_run["data"])
        self.assertEqual(inspected_run["data"]["id"], run_id)
        self.assertEqual(inspected_run["data"]["status"], "started")
        self.assertEqual(inspected_run["data"]["workspace"], workspace)
        self.assertEqual(inspected_run["data"]["commands"], [])
        self.assertEqual(inspected_run["data"]["checks"], [])
        self.assertEqual(inspected_run["data"]["artifacts"], [])
        self.assertEqual(inspected_run["data"]["changesets"], [])
        self.assertEqual(inspected_run["data"]["redacted"]["changesets"], 0)
        self.assertEqual(inspected_run["data"]["cost"]["external_cost"], "0 USD")
        self.tri("files", "write", workspace, "src/app.txt", "--text", "task edit\n")
        mutating_attempts = self.tri("task", "attempts", task["id"])
        self.assertEqual(mutating_attempts["data"]["runs"][0]["status"], "mutating")
        self.tri("run", workspace, "--", "python3", "-c", "print('not the required check')")
        checking_attempts = self.tri("task", "attempts", task["id"])
        self.assertEqual(checking_attempts["data"]["runs"][0]["status"], "checking")
        blocked_changeset = self.tri("changeset", "create", workspace, "--title", "Missing task check")["data"]["id"]
        summarizing_attempts = self.tri("task", "attempts", task["id"])
        self.assertEqual(summarizing_attempts["data"]["runs"][0]["status"], "summarizing")
        blocked_review_response = self.tri("review", "create", blocked_changeset)
        blocked_review = blocked_review_response["data"]["id"]
        self.assertEqual(blocked_review_response["data"]["status"], "waiting_for_checks")
        self.assertEqual(self.tri("review", "inspect", blocked_review)["data"]["status"], "waiting_for_checks")
        blocked = self.tri("review", "approve", blocked_review, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "missing_required_check")
        self.assertEqual(blocked["diagnostics"][0]["required_checks"][0]["command"], ["python3", "-c", "pass"])

        self.tri("run", workspace, "--assumption", "fixture is deterministic", "--", "python3", "-c", "pass")
        changeset = self.tri("changeset", "create", workspace, "--title", "Task checked")["data"]
        self.assertEqual(changeset["task_run"], run_id)
        review_response = self.tri("review", "create", changeset["id"])
        review = review_response["data"]["id"]
        self.assertEqual(review_response["data"]["status"], "open")
        self.tri("review", "approve", review)
        decision = self.tri("policy", "evaluate", "export", changeset["id"])
        self.assertTrue(decision["data"]["allowed"])
        self.assertEqual(decision["data"]["checks"]["total"], 2)
        self.assertEqual(len(decision["data"]["checks"]["latest_required"]), 1)

        completed = self.tri("task", "complete-run", run_id, "--recommendation", "Use the checked changeset.")
        self.assert_schema_required_keys("tri.task_run.inspect", completed["data"])
        self.assertEqual(completed["data"]["status"], "completed")
        self.assertFalse(completed["data"]["redacted_workspace"])
        self.assertEqual(completed["data"]["final_recommendation"], "Use the checked changeset.")
        self.assertEqual(len(completed["data"]["checks"]), 2)
        self.assertEqual(len(completed["data"]["commands"]), 2)
        self.assertEqual(completed["data"]["commands"][-1]["assumptions"], ["fixture is deterministic"])
        self.assertEqual(completed["data"]["redacted"]["checks"], 0)
        self.assertEqual(completed["data"]["changesets"], [blocked_changeset, changeset["id"]])
        self.assertEqual(completed["data"]["redacted"]["changesets"], 0)
        self.assertEqual(completed["data"]["cost"]["external_cost"], "0 USD")
        self.assertGreater(completed["data"]["cost"]["input_bytes"], 0)
        self.assertGreaterEqual(len(completed["data"]["artifacts"]), 4)
        inspected_completed = self.tri("task", "run-inspect", run_id)
        self.assertEqual(inspected_completed["data"]["status"], "completed")
        self.assertEqual(inspected_completed["data"]["final_recommendation"], "Use the checked changeset.")
        self.assertEqual(inspected_completed["data"]["commands"], completed["data"]["commands"])
        self.assertEqual(inspected_completed["data"]["changesets"], [blocked_changeset, changeset["id"]])
        late_write = self.tri("files", "write", workspace, "src/app.txt", "--text", "late edit\n", ok=False)
        self.assertEqual(late_write["diagnostics"][0]["code"], "task_lifecycle_blocked")
        self.assertEqual(late_write["diagnostics"][0]["status"], "completed")
        second_complete = self.tri("task", "complete-run", run_id, "--recommendation", "Overwrite it.", ok=False)
        self.assertEqual(second_complete["diagnostics"][0]["code"], "task_lifecycle_blocked")
        attempts = self.tri("task", "attempts", task["id"])
        self.assertEqual(attempts["data"]["task"]["status"], "reviewing")
        self.assertEqual(attempts["data"]["runs"][0]["final_recommendation"], "Use the checked changeset.")
        self.assertIn("fixture is deterministic", attempts["data"]["runs"][0]["assumptions"])

        other_task = self.tri("task", "create", "app", "--title", "Other task")["data"]
        other_workspace = self.tri("workspace", "create", "app", "--from", target, "--task", other_task["id"])["data"]["id"]
        self.tri("files", "write", other_workspace, "README.md", "--text", "other task edit\n")
        other_changeset = self.tri("changeset", "create", other_workspace, "--title", "Other task change")["data"]["id"]
        wrong_accept = self.tri("task", "accept", task["id"], "--changeset", other_changeset, ok=False)
        self.assertEqual(wrong_accept["diagnostics"][0]["code"], "invalid_command")
        self.assertEqual(wrong_accept["diagnostics"][0]["task"], task["id"])

        accepted = self.tri("task", "accept", task["id"], "--changeset", changeset["id"])["data"]
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["lifecycle"]["accepted_changesets"], [changeset["id"]])
        closed = self.tri("task", "close", task["id"], "--reason", "Done")["data"]
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["transition"], "accepted->closed")

        rejected_task = self.tri("task", "create", "app", "--title", "Reject me")["data"]
        rejected_workspace = self.tri("workspace", "create", "app", "--from", target, "--task", rejected_task["id"])["data"]["id"]
        rejected_run = self.tri("workspace", "inspect", rejected_workspace)["data"]["task_run"]
        self.tri("task", "complete-run", rejected_run, "--recommendation", "Do not use.")
        rejected = self.tri("task", "reject", rejected_task["id"], "--reason", "Wrong goal")["data"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["lifecycle"]["reason"], "Wrong goal")
        closed_rejected = self.tri("task", "close", rejected_task["id"], "--reason", "Archived rejection")["data"]
        self.assertEqual(closed_rejected["transition"], "rejected->closed")

    def test_named_check_sets_expand_into_task_required_checks(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]
        blocked_target = self.tri("--actor", "agent:a", "targets", "refresh", "app", target, ok=False)
        self.assertEqual(blocked_target["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_target["diagnostics"][0]["action"], "target.refresh")

        blocked_check_set = self.tri(
            "--actor",
            "agent:a",
            "checks",
            "set-create",
            "app",
            "agent-smoke",
            "--required-check",
            "python3 -c pass",
            ok=False,
        )
        self.assertEqual(blocked_check_set["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(blocked_check_set["diagnostics"][0]["action"], "check_set.create")

        self.tri("targets", "refresh", "app", target)
        check_set = self.tri(
            "checks",
            "set-create",
            "app",
            "smoke",
            "--required-check",
            "python3 -c pass",
        )["data"]
        self.assertEqual(check_set["name"], "smoke")
        self.assertEqual(check_set["required_checks"][0]["command"], ["python3", "-c", "pass"])

        listed = self.tri("checks", "set-list", "app")
        self.assertEqual([item["name"] for item in listed["data"]["check_sets"]], ["smoke"])
        inspected = self.tri("checks", "set-inspect", "app", "smoke")
        self.assertEqual(inspected["data"]["id"], check_set["id"])
        inspected_by_id = self.tri("checks", "set-inspect", check_set["id"])
        self.assertEqual(inspected_by_id["data"]["name"], "smoke")

        task = self.tri("task", "create", "app", "--title", "Use smoke set", "--check-set", "smoke")["data"]
        self.assertEqual(task["required_checks"][0]["command"], ["python3", "-c", "pass"])
        self.assertEqual(task["required_checks"][0]["source"]["name"], "smoke")

        workspace = self.tri("workspace", "create", "app", "--from", target, "--task", task["id"])["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "check set edit\n")
        self.tri("run", workspace, "--", "python3", "-c", "print('not smoke')")
        blocked_changeset = self.tri("changeset", "create", workspace, "--title", "Missing smoke")["data"]["id"]
        blocked_review = self.tri("review", "create", blocked_changeset)["data"]["id"]
        blocked = self.tri("review", "approve", blocked_review, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "missing_required_check")
        self.assertEqual(blocked["diagnostics"][0]["required_checks"][0]["source"]["name"], "smoke")

        self.tri("run", workspace, "--", "python3", "-c", "pass")
        changeset = self.tri("changeset", "create", workspace, "--title", "Smoke checked")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]
        approved = self.tri("review", "approve", review)
        self.assertEqual(approved["data"]["status"], "approved")

    def test_check_rerun_compare_and_artifact_listing(self) -> None:
        imported = self.import_project()
        workspace = self.tri("workspace", "create", "app", "--from", imported["data"]["target"]["name"])["data"]["id"]
        check = self.tri("run", workspace, "--", "python3", "-c", "print('repeatable')")
        check_id = check["data"]["id"]
        inspected = self.tri("checks", "inspect", check_id)
        self.assertEqual(inspected["data"]["id"], check_id)
        self.assertEqual(inspected["data"]["actor"], "actor_local")
        self.assertEqual(inspected["data"]["cost"]["external_cost"], "0 USD")
        self.assert_schema_required_keys("tri.check.inspect", inspected["data"])

        check_artifacts = self.tri("checks", "artifacts", check_id)
        self.assertEqual(len(check_artifacts["data"]["artifacts"]), 2)

        stdout_artifact = check["data"]["logs"]["stdout"]
        artifacts = self.tri("artifacts", "list", "--kind", "stdout")
        self.assertIn(stdout_artifact, {artifact["id"] for artifact in artifacts["data"]["artifacts"]})

        first_chunk = self.tri("checks", "stream", check_id, "--log", "stdout", "--offset", "0", "--limit", "6")["data"]
        self.assertEqual(first_chunk["artifact"], stdout_artifact)
        self.assertEqual(first_chunk["text"], "repeat")
        self.assertEqual(first_chunk["next_offset"], 6)
        self.assertFalse(first_chunk["eof"])

        second_chunk = self.tri("checks", "stream", check_id, "--log", "stdout", "--offset", str(first_chunk["next_offset"]), "--limit", "1024")["data"]
        self.assertEqual(second_chunk["text"], "able\n")
        self.assertTrue(second_chunk["eof"])

        stderr_chunk = self.tri("checks", "stream", check_id, "--log", "stderr")["data"]
        self.assertEqual(stderr_chunk["text"], "")
        self.assertTrue(stderr_chunk["eof"])

        rerun = self.tri("checks", "rerun", check_id)
        self.assertNotEqual(rerun["data"]["id"], check_id)
        self.assertEqual(rerun["data"]["rerun_of"], check_id)
        self.assertEqual(rerun["data"]["actor"], "actor_local")
        self.assertEqual(rerun["data"]["cost"]["external_cost"], "0 USD")
        self.assert_schema_required_keys("tri.check.inspect", rerun["data"])

        comparison = self.tri("checks", "compare", check_id, rerun["data"]["id"])
        self.assertTrue(comparison["data"]["same_command"])
        self.assertTrue(comparison["data"]["all_passed"])
        self.assertEqual(comparison["data"]["status_changed_from_first"], [])

    def test_review_approval_respects_assigned_reviewers(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "reviewer flow\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'reviewer flow\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Reviewer assignment")["data"]["id"]
        review = self.tri("review", "create", changeset, "--reviewers", "human:alice", "--reviewers", "human:bob")["data"]["id"]

        for actor in ("human:alice", "human:bob", "human:mallory"):
            self.tri("policy", "grant", changeset, "--principal", f"actor:{actor}", "--action", "changeset.write")

        blocked = self.tri("--actor", "human:mallory", "review", "approve", review, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "reviewer_not_assigned")

        alice = self.tri("--actor", "human:alice", "review", "approve", review)
        self.assertEqual(alice["data"]["status"], "waiting_for_reviewers")
        self.assertEqual(alice["data"]["pending_reviewers"], ["human:bob"])
        pending = self.tri("review", "inspect", review)
        self.assertEqual(pending["data"]["status"], "waiting_for_reviewers")
        self.assertEqual(pending["data"]["approvals"], ["human:alice"])
        self.assertEqual(pending["data"]["pending_reviewers"], ["human:bob"])
        self.assertEqual(self.tri("changeset", "inspect", changeset)["data"]["status"], "reviewing")

        bob = self.tri("--actor", "human:bob", "review", "approve", review)
        self.assertEqual(bob["data"]["status"], "approved")
        approved = self.tri("review", "inspect", review)
        self.assertEqual(approved["data"]["approvals"], ["human:alice", "human:bob"])
        self.assertEqual(approved["data"]["approved_reviewers"], ["human:alice", "human:bob"])
        self.assertEqual(approved["data"]["pending_reviewers"], [])
        self.assertEqual(self.tri("changeset", "inspect", changeset)["data"]["status"], "approved")

    def test_review_threaded_comments_can_be_replied_to_and_resolved(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "thread me\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'thread me\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Thread comments")["data"]["id"]
        review = self.tri("review", "create", changeset)["data"]["id"]

        root = self.tri("review", "comment", review, "--path", "src/app.txt", "--line", "1", "--body", "Please explain this change.")["data"]["action"]
        reply = self.tri("review", "comment", review, "--reply-to", root["id"], "--body", "Added a simpler test fixture.")["data"]["action"]

        threaded = self.tri("review", "inspect", review)["data"]["comment_threads"]
        self.assertEqual(len(threaded), 1)
        self.assertEqual(threaded[0]["id"], root["id"])
        self.assertEqual(threaded[0]["status"], "open")
        self.assertEqual(threaded[0]["path"], "src/app.txt")
        self.assertEqual(threaded[0]["line"], 1)
        self.assertEqual([comment["id"] for comment in threaded[0]["comments"]], [root["id"], reply["id"]])
        self.assertIsNone(threaded[0]["comments"][0]["reply_to"])
        self.assertEqual(threaded[0]["comments"][1]["reply_to"], root["id"])

        resolved = self.tri("review", "resolve-thread", review, "--thread", root["id"], "--body", "Resolved after explanation.")
        self.assertEqual(resolved["data"]["thread"], root["id"])
        after = self.tri("review", "inspect", review)["data"]["comment_threads"][0]
        self.assertEqual(after["status"], "resolved")
        self.assertEqual(after["resolved_by"], "actor_local")

    def test_review_actions_and_agent_pass_workspace(self) -> None:
        imported = self.import_project()
        target = imported["data"]["target"]["name"]

        workspace = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", workspace, "src/app.txt", "--text", "review me\n")
        self.tri("run", workspace, "--", "python3", "-c", "from pathlib import Path; assert Path('src/app.txt').read_text() == 'review me\\n'")
        changeset = self.tri("changeset", "create", workspace, "--title", "Review actions")["data"]["id"]
        review = self.tri("review", "create", changeset, "--reviewers", "human:reviewer")["data"]["id"]

        comment = self.tri("review", "comment", review, "--body", "Looks small.")
        self.assertEqual(comment["data"]["action"]["kind"], "commented")
        inspected_comment = self.tri("review", "action-inspect", comment["data"]["action"]["id"])
        self.assert_schema_required_keys("tri.review_action.inspect", inspected_comment["data"])
        self.assertEqual(inspected_comment["data"]["id"], comment["data"]["action"]["id"])
        self.assertEqual(inspected_comment["data"]["body"], "Looks small.")

        risk = self.tri("review", "mark-risk", review, "--risk", "medium", "--body", "Touches runtime path.")
        self.assertEqual(risk["data"]["action"]["metadata"]["risk"], "medium")

        check_summary = self.tri("review", "attach-check-summary", review, "--body", "Checks refreshed for review.")
        self.assertEqual(check_summary["data"]["action"]["kind"], "check_summary_attached")
        self.assertEqual(check_summary["data"]["check_summary"]["total"], 1)
        self.assertEqual(check_summary["data"]["check_summary"]["passed"], 1)
        self.assertTrue(check_summary["data"]["check_summary"]["required_satisfied"])

        policy_finding = self.tri("review", "attach-policy-finding", review, "--body", "Policy snapshot before approval.")
        self.assertEqual(policy_finding["data"]["action"]["kind"], "policy_finding_attached")
        self.assertEqual(policy_finding["data"]["policy_finding"]["decision"]["reason_code"], "missing_review_approval")
        attached = self.tri("review", "inspect", review)["data"]
        self.assertEqual(attached["attached_check_summaries"][0]["metadata"]["check_summary"]["passed"], 1)
        self.assertEqual(attached["attached_policy_findings"][0]["metadata"]["decision"]["reason_code"], "missing_review_approval")

        requested = self.tri("review", "request-changes", review, "--body", "Add a test.")
        self.assertEqual(requested["data"]["status"], "changes_requested")
        self.assertEqual(self.tri("changeset", "inspect", changeset)["data"]["status"], "changes_requested")

        blocked_approval = self.tri("review", "approve", review, "--body", "Too early.", ok=False)
        self.assertEqual(blocked_approval["diagnostics"][0]["code"], "changeset_lifecycle_blocked")
        ready = self.tri("changeset", "ready", changeset, "--reason", "Added the requested test.")
        self.assertEqual(ready["data"]["previous_status"], "changes_requested")
        self.assertEqual(ready["data"]["status"], "ready")
        self.assertEqual(ready["data"]["transition"], "changes_requested->ready")
        self.assertEqual(ready["data"]["lifecycle"]["reason"], "Added the requested test.")
        self.assertEqual(self.tri("review", "inspect", review)["data"]["status"], "open")

        approved = self.tri("review", "approve", review, "--body", "Accepting for MVP.")
        self.assertEqual(approved["data"]["status"], "approved")
        inspected = self.tri("review", "inspect", review)
        self.assertEqual(inspected["data"]["status"], "approved")
        self.assertEqual(inspected["data"]["risk_summary"], "medium")
        self.assertEqual(
            [action["kind"] for action in inspected["data"]["actions"]],
            ["commented", "risk_marked", "check_summary_attached", "policy_finding_attached", "changes_requested", "approved"],
        )

        close_ws = self.tri("workspace", "create", "app", "--from", target)["data"]["id"]
        self.tri("files", "write", close_ws, "README.md", "--text", "close review\n")
        close_changeset = self.tri("changeset", "create", close_ws, "--title", "Close review")["data"]["id"]
        close_review = self.tri("review", "create", close_changeset)["data"]["id"]
        closed = self.tri("review", "close", close_review, "--body", "No longer needed.")
        self.assertEqual(closed["data"]["status"], "closed")
        self.assertEqual(closed["data"]["previous_status"], "waiting_for_checks")
        close_inspected = self.tri("review", "inspect", close_review)
        self.assertEqual(close_inspected["data"]["status"], "closed")
        self.assertEqual(self.tri("changeset", "inspect", close_changeset)["data"]["status"], "reviewing")
        blocked_comment = self.tri("review", "comment", close_review, "--body", "late comment", ok=False)
        self.assertEqual(blocked_comment["diagnostics"][0]["code"], "review_closed")
        blocked_approve = self.tri("review", "approve", close_review, ok=False)
        self.assertEqual(blocked_approve["diagnostics"][0]["code"], "review_closed")

        followup = self.tri(
            "review",
            "request-agent-pass",
            review,
            "--task",
            "Minimize the patch",
            "--assignee",
            "agent:b",
        )
        followup_workspace = followup["data"]["workspace"]
        followup_read = self.tri("files", "read", followup_workspace, "src/app.txt")
        self.assertEqual(followup_read["data"]["text"], "review me\n")

        attempts = self.tri("task", "attempts", followup["data"]["task"])
        self.assertEqual(attempts["data"]["runs"][0]["actor"], "agent:b")
        self.assertEqual(attempts["data"]["workspaces"][0]["id"], followup_workspace)


if __name__ == "__main__":
    unittest.main()
