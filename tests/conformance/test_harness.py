"""The validator must reject what the contracts forbid, or every suite passes vacuously."""

from __future__ import annotations

import unittest

from .harness import Contracts, server_argv


def full_envelope(**overrides):
    envelope = {
        "ok": True, "schema": "tri.files.read", "schema_version": "0.1.0", "command": ["tri", "files", "read"], "actor": "agent:a",
        "data": {"snapshot": "snap_1", "path": "a", "blob": "blob_1", "encoding": "utf-8", "text": "x"},
        "events": [], "diagnostics": [], "warnings": [], "cost": {"wall_ms": 1}, "refs": {}, "links": {},
    }
    envelope.update(overrides)
    return envelope


class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts = Contracts()

    def test_accepts_a_valid_envelope(self) -> None:
        self.assertEqual(self.contracts.envelope_errors(full_envelope(), expect_schema="tri.files.read"), [])

    def test_rejects_contract_violations(self) -> None:
        data = full_envelope()["data"]
        del data["text"]
        cases = {
            "missing data field": full_envelope(data=data),
            "wrong type": full_envelope(ok="yes"),
            "bad schema version": full_envelope(schema_version="v1"),
            "missing envelope field": {k: v for k, v in full_envelope().items() if k != "refs"},
            "unknown diagnostic code": full_envelope(ok=False, schema="tri.error", data={}, diagnostics=[{"code": "made_up", "severity": "error", "message": "m", "retryable": False}]),
            "error without diagnostics": full_envelope(ok=False, schema="tri.error", data={}),
            "unknown schema": full_envelope(schema="tri.not.a.schema"),
        }
        for label, envelope in cases.items():
            with self.subTest(label):
                self.assertNotEqual(self.contracts.envelope_errors(envelope), [], label)
        self.assertNotEqual(self.contracts.envelope_errors(full_envelope(), expect_schema="tri.diff"), [])

    def test_any_of(self) -> None:
        schema = self.contracts.load("schemas/tri.schema.json")
        self.assertEqual(self.contracts.validate({"schema_version": "0.1.0", "schemas": {}}, schema), [])
        self.assertNotEqual(self.contracts.validate({"schema_version": "0.1.0"}, schema), [])

    def test_command_schema_lookup(self) -> None:
        self.assertEqual(self.contracts.command_schema(["files", "read", "ws", "a"]), "tri.files.read")
        self.assertEqual(self.contracts.command_schema(["--actor", "agent:a", "workspace", "create", "app"]), "tri.workspace.create")
        self.assertIsNone(self.contracts.command_schema(["nope"]))

    def test_server_argv(self) -> None:
        argv = server_argv("127.0.0.1", 9, "t.json", native="/bin/tri-server")
        self.assertEqual(argv[:4], ["/bin/tri-server", "--host", "127.0.0.1", "--port"])
        self.assertIn("--auth-file", argv)
        self.assertEqual(argv[-2:], ["worker", "--stdio"])


if __name__ == "__main__":
    unittest.main()
