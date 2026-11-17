from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FanoutBenchmarkTests(unittest.TestCase):
    def test_fanout_scenario_small_runs(self) -> None:
        from benchmarks.fanout import main

        with tempfile.TemporaryDirectory(prefix="tri-fanout-test-") as tmp:
            out = Path(tmp) / "out"
            main(["--attempts", "2", "--source-files", "20", "--dep-files", "30", "--volume", "none", "--targets", "tri,tri-mount,git", "--output", str(out)])
            results = json.loads((out / "results.json").read_text())
            by_target = {item["target"]: item for item in results["results"]}
            self.assertEqual(set(by_target), {"tri", "tri-mount", "git"})
            for target, item in by_target.items():
                self.assertEqual(item["status"], "ok", item)
                self.assertIn(item["measure"], ("volume-used", "st_blocks"))
                self.assertGreater(item["end_allocated_bytes"], 0)
                self.assertGreaterEqual(item["peak_allocated_bytes"], item["end_allocated_bytes"] - 1)
                self.assertEqual(item["checks_passed"], 2)
            self.assertIn(by_target["tri"]["backends"][0], ("clone", "copy", "overlay"))
            self.assertTrue((out / "summary.md").read_text().startswith("# Fan-out disk benchmark"))

    def test_fanout_parallel_and_bound_targets(self) -> None:
        from benchmarks.fanout import main

        with tempfile.TemporaryDirectory(prefix="tri-fanout-test-") as tmp:
            out = Path(tmp) / "out"
            main(["--attempts", "3", "--parallel", "3", "--source-files", "20", "--dep-files", "30", "--volume", "none", "--targets", "tri-copy,tri-mount-link,git", "--output", str(out)])
            results = json.loads((out / "results.json").read_text())
            by_target = {item["target"]: item for item in results["results"]}
            for item in by_target.values():
                self.assertEqual(item["status"], "ok", item)
                self.assertEqual(item["checks_passed"], 3)
            self.assertEqual(by_target["tri-copy"]["backends"], ["copy"])
            self.assertEqual(results["parameters"]["parallel"], 3)


if __name__ == "__main__":
    unittest.main()
