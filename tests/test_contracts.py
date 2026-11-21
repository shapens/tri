"""The contract files in contracts/v0 are generated from the registries and must not drift."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tri import cli, contracts

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts" / contracts.CONTRACT_VERSION


class ContractFileTests(unittest.TestCase):
    def test_contract_files_match_registries(self) -> None:
        problems = contracts.drift(CONTRACTS)
        self.assertEqual(problems, [], "contracts/v0 drifted from COMMAND_CONTRACTS / SCHEMA_CATALOG; run `python -m tri.contracts --write`")

    def test_every_command_and_schema_has_a_file(self) -> None:
        commands = json.loads((CONTRACTS / "commands.json").read_bytes())
        self.assertEqual([item["name"] for item in commands["commands"]], [item["name"] for item in cli.COMMAND_CONTRACTS])
        for item in commands["commands"]:
            self.assertTrue((CONTRACTS / item["data_schema"]).exists(), item)
        for name in cli.SCHEMA_CATALOG:
            self.assertTrue((CONTRACTS / contracts.schema_file(name)).exists(), name)
        self.assertEqual(commands["api_blocked_commands"], sorted(cli.API_BLOCKED_COMMANDS))

    def test_diagnostic_codes_are_the_catalog_codes(self) -> None:
        diagnostic = json.loads((CONTRACTS / "diagnostic.json").read_bytes())
        self.assertEqual(diagnostic["properties"]["code"]["enum"], sorted(set(cli.SCHEMA_CATALOG["tri.diagnostic"]["codes"])))

    def test_native_and_worker_diagnostic_codes_are_in_the_contract(self) -> None:
        import re

        codes = set(cli.SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        sources = [path for path in (ROOT / "native" / "tri-server").glob("*.go") if not path.name.endswith("_test.go")]
        self.assertTrue(sources)
        emitted = {match for path in sources for match in re.findall(r'NewDiagnostic\("([a-z_]+)"', path.read_text())}
        emitted |= set(re.findall(r'TriError\("([a-z_]+)"', (ROOT / "tri" / "worker.py").read_text()))
        self.assertGreaterEqual(emitted, {"unauthenticated", "server_busy", "request_timeout", "worker_failed"})
        self.assertEqual(sorted(emitted - codes), [])

    def test_drift_is_detected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            contracts.write(target)
            self.assertEqual(contracts.drift(target), [])
            stale = target / "schemas" / "tri.files.read.json"
            document = json.loads(stale.read_bytes())
            document["required"].append("extra")
            stale.write_bytes(json.dumps(document).encode())
            (target / "schemas" / "tri.gone.json").write_bytes(b"{}")
            (target / "api.json").unlink()
            self.assertEqual(
                sorted(contracts.drift(target)),
                ["missing: api.json", "no longer generated: schemas/tri.gone.json", "stale: schemas/tri.files.read.json"],
            )


if __name__ == "__main__":
    unittest.main()
