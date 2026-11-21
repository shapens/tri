"""HTTP API conformance: /v0/command, /v0/batch, /v0/health, /v0/ready, GET routes,
auth, and the server-path refusal, validated against contracts/v0.

Runs against `tri serve` by default; set TRI_CONFORMANCE_NATIVE to a tri-server
binary (or TRI_CONFORMANCE_SERVER / TRI_CONFORMANCE_URL) to test another
implementation. See harness.py.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from .harness import Contracts, Server, base_env, implementation_name, make_repo, run_cli

ADMIN = "actor_local"
AGENT = "agent:conformance"
OTHER = "agent:other"


class HTTPConformanceBase(unittest.TestCase):
    """One fixture home and two servers per class: unauthenticated loopback, and
    one with a token file. Under TRI_CONFORMANCE_URL only the given server runs."""

    contracts: Contracts
    open_server: Server | None = None
    auth_server: Server | None = None
    tokens: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts = Contracts()
        cls.tokens = {}
        cls.tmp = Path(tempfile.mkdtemp(prefix="tri-conformance-http-"))
        cls.env = base_env(cls.tmp / "home")
        cls.repo = make_repo(cls.tmp)
        url = os.environ.get("TRI_CONFORMANCE_URL")
        try:
            if url:
                cls.open_server = Server(url)
                token = os.environ.get("TRI_CONFORMANCE_TOKEN")
                cls.admin_token = token
            else:
                cls.open_server = Server.start(cls.env)
                cls.admin_token = None
            imported = cls.open_server.command(["import", str(cls.repo), "--name", "app"], token=cls.admin_token)
            if not (imported.json or {}).get("ok"):
                raise unittest.SkipTest(f"fixture import failed against {implementation_name()}: {imported.text[:400]}")
            if not url:
                token_file = cls.tmp / "tokens.json"
                for actor in (ADMIN, AGENT, OTHER):
                    status, issued, err = run_cli(["auth", "issue", actor, "--file", str(token_file)], cls.env)
                    assert status == 0, err
                    cls.tokens[actor] = issued["data"]["token"]
                cls.token_file = token_file
                cls.auth_server = Server.start(cls.env, str(token_file))
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        for server in (cls.open_server, cls.auth_server):
            if server is not None:
                server.stop()
        cls.open_server = cls.auth_server = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # Helpers -------------------------------------------------------------------

    def assertConforms(self, response, *, status: int = 200, expect_schema: str | None = None, summary: bool = False, ok: bool | None = None) -> dict:
        self.assertEqual(response.status, status, response.text[:600])
        self.assertTrue(response.headers.get("content-type", "").startswith("application/json"), response.headers)
        self.assertEqual(int(response.headers.get("content-length", -1)), len(response.raw))
        errors = self.contracts.envelope_errors(response.json, summary=summary, expect_schema=expect_schema)
        self.assertEqual(errors, [], f"{response.text[:600]}")
        if ok is not None:
            self.assertEqual(response.json["ok"], ok, response.text[:600])
        return response.json

    def run_command(self, argv: list[str], *, token: str | None = None, server: Server | None = None, ok: bool | None = True, **extra) -> dict:
        server = server or self.open_server
        response = server.command(argv, token=token if token is not None else (self.admin_token if server is self.open_server else None), **extra)
        expect = self.contracts.command_schema(argv)
        summary = "--response" in argv and argv[argv.index("--response") + 1] == "summary"
        return self.assertConforms(response, expect_schema=expect, summary=summary, ok=ok)

    def require_auth_server(self) -> Server:
        if self.auth_server is None:
            self.skipTest("token tests need a server this harness starts")
        return self.auth_server


class HTTPConformanceTests(HTTPConformanceBase):
    def test_health_and_ready(self) -> None:
        for path in ("/v0/health", "/health"):
            body = self.assertConforms(self.open_server.get(path), expect_schema="tri.api.health", ok=True)
            self.assertEqual(body["data"]["status"], "ok")
        body = self.assertConforms(self.open_server.get("/v0/ready"), expect_schema="tri.api.ready", ok=True)
        self.assertTrue(body["data"]["ready"])

    def test_discovery_routes_match_the_contracts(self) -> None:
        body = self.assertConforms(self.open_server.get("/v0/commands", token=self.admin_token), expect_schema="tri.commands")
        self.assertEqual([item["name"] for item in body["data"]["commands"]], list(self.contracts.by_name))
        self.assertEqual({item["name"]: item["schema"] for item in body["data"]["commands"]}, {name: item["schema"] for name, item in self.contracts.by_name.items()})
        body = self.assertConforms(self.open_server.get("/v0/schema", token=self.admin_token), expect_schema="tri.schema")
        self.assertEqual(sorted(body["data"]["schemas"]), sorted(path.removeprefix("schemas/").removesuffix(".json") for path in self.contracts.index["files"] if path.startswith("schemas/")))
        self.assertConforms(self.open_server.get("/v0/schema/tri.files.read", token=self.admin_token), expect_schema="tri.schema")
        body = self.assertConforms(self.open_server.get("/v0", token=self.admin_token), expect_schema="tri.api.server")
        self.assertEqual(sorted(body["data"]["endpoints"]), sorted(route["name"] for route in self.contracts.api["routes"] if route["name"] != "ready"))

    def test_agent_loop_through_the_command_route(self) -> None:
        self.run_command(["projects", "list"])
        self.run_command(["targets", "list", "app"])
        ws = self.run_command(["workspace", "create", "app", "--from", "main", "--actor", AGENT])["data"]["id"]
        self.run_command(["files", "write", ws, "src/new.py", "--text", "NEW = 1\n", "--actor", AGENT])
        read = self.run_command(["files", "read", ws, "src/new.py", "--actor", AGENT])
        self.assertEqual(read["data"]["text"], "NEW = 1\n")
        self.run_command(["files", "list", ws, "--actor", AGENT])
        self.run_command(["files", "stat", ws, "src/app.py", "--actor", AGENT])
        self.run_command(["files", "search", ws, "conformance", "--actor", AGENT])
        self.run_command(["diff", ws, "--actor", AGENT])
        self.run_command(["workspace", "inspect", ws, "--actor", AGENT])
        changeset = self.run_command(["changeset", "create", ws, "--title", "conformance change", "--actor", AGENT])["data"]["id"]
        self.run_command(["changeset", "inspect", changeset, "--actor", AGENT])
        self.run_command(["events", "list", "--workspace", ws, "--limit", "5", "--actor", AGENT])
        self.run_command(["--response", "summary", "workspace", "list", "--actor", AGENT])

    def test_get_routes(self) -> None:
        ws = self.run_command(["workspace", "create", "app", "--from", "main"])["data"]["id"]
        snapshot = self.run_command(["targets", "inspect", "app", "main"])["data"]
        snapshot_id = snapshot.get("snapshot") or snapshot.get("snapshot_id")
        if isinstance(snapshot_id, dict):
            snapshot_id = snapshot_id.get("id")
        routes = {
            "/v0/projects": "tri.project.list",
            "/v0/projects/app": None,
            "/v0/projects/app/targets": None,
            "/v0/projects/app/workspaces": None,
            f"/v0/workspaces/{ws}": None,
            f"/v0/workspaces/{ws}/files": "tri.files.list",
            f"/v0/workspaces/{ws}/files/src/app.py": "tri.files.read",
            f"/v0/workspaces/{ws}/search?q=conformance": "tri.files.search",
            f"/v0/workspaces/{ws}/diff?mode=summary": None,
            f"/v0/events?project=app&limit=5": "tri.events.list",
            f"/v0/objects/{ws}": None,
        }
        if isinstance(snapshot_id, str):
            routes[f"/v0/snapshots/{snapshot_id}/files"] = "tri.files.list"
        for path, schema in routes.items():
            with self.subTest(path=path):
                self.assertConforms(self.open_server.get(path, token=self.admin_token), expect_schema=schema, ok=True)

    def test_batch_with_placeholders_and_returns(self) -> None:
        payload = {
            "commands": [
                {"argv": ["workspace", "create", "app", "--from", "main", "--actor", AGENT], "as": "ws"},
                {"argv": ["files", "write", "${ws.data.id}", "batch.txt", "--text", "from a batch\n", "--actor", AGENT]},
                {"argv": ["files", "read", "${ws.data.id}", "batch.txt", "--actor", AGENT], "as": "read"},
            ],
            "return": {"workspace": "${ws.data.id}", "text": "${read.data.text}"},
        }
        body = self.assertConforms(self.open_server.post("/v0/batch", payload, token=self.admin_token), expect_schema="tri.api.batch", ok=True)
        self.assertEqual(body["data"]["executed"], 3)
        self.assertEqual(body["data"]["returns"]["text"], "from a batch\n")
        for result in body["data"]["results"]:
            self.assertTrue(result["ok"], result)
        summary = self.open_server.post("/v0/batch", {"commands": [{"argv": ["projects", "list"]}], "response": "summary"}, token=self.admin_token)
        self.assertConforms(summary, expect_schema="tri.api.batch", ok=True)

    def test_errors_are_contract_envelopes(self) -> None:
        cases = [
            (["workspace", "inspect", "ws_doesnotexist0"], "path_not_found"),
            (["files", "read"], "invalid_command"),
            (["no-such-command"], "invalid_command"),
            (["--actor", "not a valid actor", "projects", "list"], "unknown_actor"),
        ]
        for argv, code in cases:
            with self.subTest(argv=argv):
                body = self.assertConforms(self.open_server.command(argv, token=self.admin_token), ok=False)
                self.assertEqual(body["schema"], "tri.error")
                self.assertIn(code, [item["code"] for item in body["diagnostics"]])
        for raw, code in ((b"not json", "invalid_command"), (b"[1, 2]", "invalid_command"), (b'{"argv": "projects list"}', "invalid_command"), (b"{}", "invalid_command")):
            with self.subTest(body=raw):
                response = self.open_server.request("POST", "/v0/command", raw, headers={"Authorization": f"Bearer {self.admin_token}"} if self.admin_token else None)
                body = self.assertConforms(response, ok=False)
                self.assertEqual([item["code"] for item in body["diagnostics"]], [code])
        missing = self.open_server.get("/v0/no/such/route", token=self.admin_token)
        self.assertEqual(self.assertConforms(missing, status=404, ok=False)["diagnostics"][0]["code"], "path_not_found")
        missing = self.open_server.post("/v0/nothing", {}, token=self.admin_token)
        self.assertEqual(self.assertConforms(missing, status=404, ok=False)["diagnostics"][0]["code"], "path_not_found")

    def test_oversized_body_is_refused_without_reading_it(self) -> None:
        limit = self.contracts.api["limits"]["max_body_bytes"]
        conn = http.client.HTTPConnection(self.open_server.host, self.open_server.port, timeout=30)
        try:
            conn.putrequest("POST", "/v0/command")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(limit + 1))
            if self.admin_token:
                conn.putheader("Authorization", f"Bearer {self.admin_token}")
            conn.endheaders()
            conn.send(b'{"argv": [')  # never the rest: the server must answer from the header
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        from .harness import Response

        body = self.assertConforms(Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw), status=self.contracts.api["limits"]["oversize_body"]["status"], ok=False)
        self.assertEqual(body["diagnostics"][0]["code"], self.contracts.api["limits"]["oversize_body"]["code"])

    def test_api_blocked_commands_are_refused(self) -> None:
        self.assertIn("serve", self.contracts.blocked)
        for name in self.contracts.blocked:
            with self.subTest(command=name):
                body = self.assertConforms(self.open_server.command([name, "--help"], token=self.admin_token), ok=False)
                self.assertEqual(body["diagnostics"][0]["code"], "invalid_command")
                body = self.assertConforms(self.open_server.command(["tri", name], token=self.admin_token), ok=False)
                self.assertEqual(body["diagnostics"][0]["code"], "invalid_command")
                batch = self.open_server.post("/v0/batch", {"commands": [{"argv": [name]}]}, token=self.admin_token)
                body = self.assertConforms(batch, ok=False)
                self.assertEqual(body["diagnostics"][0]["code"], "invalid_command")

    def test_concurrent_requests(self) -> None:
        workspaces = [self.run_command(["workspace", "create", "app", "--from", "main", "--actor", AGENT])["data"]["id"] for _ in range(4)]
        failures: list[str] = []
        lock = threading.Lock()

        def client(index: int) -> None:
            ws = workspaces[index % len(workspaces)]
            for step in range(6):
                if index < len(workspaces):
                    argv = ["files", "write", ws, f"c/{index}-{step}.txt", "--text", f"{index} {step}\n", "--actor", AGENT]
                else:
                    argv = ["files", "read", ws, "src/app.py", "--actor", AGENT]
                response = self.open_server.command(argv, token=self.admin_token)
                errors = self.contracts.envelope_errors(response.json, expect_schema=self.contracts.command_schema(argv))
                if response.status != 200 or errors or not response.json.get("ok"):
                    with lock:
                        failures.append(f"{argv}: {response.status} {errors} {response.text[:300]}")

        threads = [threading.Thread(target=client, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
        self.assertEqual(failures, [])
        listed = self.run_command(["files", "list", workspaces[0], "--actor", AGENT])
        paths = {item["path"] for item in listed["data"]["files"]}
        self.assertTrue({f"c/0-{step}.txt" for step in range(6)} <= paths, sorted(paths))


class HTTPAuthConformanceTests(HTTPConformanceBase):
    def test_tokens_are_required_except_for_health(self) -> None:
        server = self.require_auth_server()
        self.assertConforms(server.get("/v0/health"), expect_schema="tri.api.health", ok=True)
        self.assertIsNone(server.get("/v0/health").json["data"]["home"])
        self.assertConforms(server.get("/v0/ready"), expect_schema="tri.api.ready", ok=True)
        for response in (server.get("/v0/projects"), server.command(["projects", "list"]), server.get("/v0/projects", token="tri_wrong"), server.post("/v0/batch", {"commands": []}, token="tri_wrong")):
            body = self.assertConforms(response, status=401, ok=False)
            self.assertEqual(body["diagnostics"][0]["code"], "unauthenticated")

    def test_actor_is_pinned_to_the_token(self) -> None:
        server = self.require_auth_server()
        token = self.tokens[AGENT]
        body = self.run_command(["workspace", "list"], token=token, server=server)
        self.assertEqual(body["actor"], AGENT)
        body = self.assertConforms(server.get("/v0/projects", token=token, headers={"X-Tri-Actor": OTHER}), status=403, ok=False)
        self.assertEqual(body["diagnostics"][0]["code"], "actor_mismatch")
        body = self.assertConforms(server.get("/v0/projects", token=token, headers={"X-Tri-Actor": AGENT}), ok=True)
        for response in (
            server.command(["workspace", "list"], token=token, actor=OTHER),
            server.command(["--actor", OTHER, "workspace", "list"], token=token),
            server.post("/v0/batch", {"commands": [{"argv": ["workspace", "list"], "actor": OTHER}]}, token=token),
            server.post("/v0/batch", {"actor": OTHER, "commands": [{"argv": ["workspace", "list"]}]}, token=token),
        ):
            body = self.assertConforms(response, ok=False)
            codes = [item["code"] for item in body["diagnostics"]] + [d["code"] for result in body["data"].get("results", []) for d in result.get("diagnostics", [])]
            self.assertIn("actor_mismatch", codes, response.text[:400])

    def test_server_paths_are_refused_for_non_maintainers(self) -> None:
        server = self.require_auth_server()
        ws = self.run_command(["workspace", "create", "app", "--from", "main"], token=self.tokens[AGENT], server=server)["data"]["id"]
        server_file = self.tmp / "server-local.txt"
        server_file.write_bytes(b"lives on the server\n")
        for argv in (["files", "write", ws, "x.txt", "--body", str(server_file)], ["import", str(self.repo), "--name", "stolen"]):
            with self.subTest(argv=argv):
                body = self.assertConforms(server.command(argv, token=self.tokens[AGENT]), ok=False)
                diagnostic = body["diagnostics"][0]
                self.assertEqual((diagnostic["code"], diagnostic.get("reason")), ("policy_denied", "server_path"))
                batch = self.assertConforms(server.post("/v0/batch", {"commands": [{"argv": argv}]}, token=self.tokens[AGENT]), ok=False)
                self.assertEqual(batch["data"]["results"][0]["diagnostics"][0].get("reason"), "server_path")
        # A maintainer may name server paths.
        admin_ws = self.run_command(["workspace", "create", "app", "--from", "main"], token=self.tokens[ADMIN], server=server)["data"]["id"]
        self.run_command(["files", "write", admin_ws, "x.txt", "--body", str(server_file)], token=self.tokens[ADMIN], server=server)

    def test_revocation_applies_on_the_next_request(self) -> None:
        server = self.require_auth_server()
        status, issued, err = run_cli(["auth", "issue", "agent:revoked", "--file", str(self.token_file)], self.env)
        self.assertEqual(status, 0, err)
        token = issued["data"]["token"]
        self.run_command(["projects", "list"], token=token, server=server)
        status, _, err = run_cli(["auth", "revoke", "agent:revoked", "--file", str(self.token_file)], self.env)
        self.assertEqual(status, 0, err)
        body = self.assertConforms(server.command(["projects", "list"], token=token), status=401, ok=False)
        self.assertEqual(body["diagnostics"][0]["code"], "unauthenticated")


if __name__ == "__main__":
    unittest.main()
