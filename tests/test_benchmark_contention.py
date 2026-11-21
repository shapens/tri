from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@unittest.skipIf(os.name == "nt", "the contention benchmark drives git clones and pushes over POSIX paths")
class ContentionBenchmarkTests(unittest.TestCase):
    def test_contention_small_runs_every_target(self) -> None:
        from benchmarks.contention import main

        with tempfile.TemporaryDirectory(prefix="tri-contention-test-") as tmp:
            out = Path(tmp) / "out"
            main(["--agents", "4", "--parallel", "2", "--files", "3", "--lines", "30", "--overlap", "0.25", "--output", str(out)])
            results = json.loads((out / "results.json").read_text())
            by_target = {item["target"]: item for item in results["results"]}
            self.assertEqual(set(by_target), {"tri", "tri-path", "git"})
            for target, item in by_target.items():
                self.assertEqual(item["status"], "ok", item)
                self.assertEqual(item["agent_errors"], 0, item["errors"])
                self.assertEqual(item["merges_attempted"], 4)
                self.assertEqual(item["merges_succeeded"] + item["merges_conflicted"] + item["merges_retry_exhausted"], 4)
                self.assertGreaterEqual(item["merges_succeeded"], 1)
                self.assertTrue(item["correctness"]["ok"], item["correctness"])
                self.assertEqual(item["correctness"]["merged_edits_present"], item["merges_succeeded"])
                self.assertGreater(item["merges_per_hour"], 0)
                self.assertGreaterEqual(item["merge_latency_p95_s"], item["merge_latency_p50_s"])
            # Line merges never turn a disjoint edit into a conflict.
            self.assertEqual(by_target["tri"]["conflicts_outside_overlap"], 0)
            self.assertEqual(results["overlap_agents"], 1)
            self.assertTrue((out / "summary.md").read_text().startswith("# Contention benchmark"))

    def test_plan_keeps_disjoint_regions_apart_and_overlaps_reuse_one(self) -> None:
        from benchmarks.contention import GAP, plan_agents

        args = argparse.Namespace(agents=20, parallel=4, files=5, lines=60, edits_per_agent=2, edit_lines=3, overlap=0.2, seed=3)
        agents = plan_agents(args)
        by_index = {a.index: a for a in agents}
        owned: dict[tuple[int, int], int] = {}
        for agent in agents:
            self.assertEqual(len({r.file for r in agent.regions}), 2)
            for i, region in enumerate(agent.regions):
                if i == 0 and agent.overlaps is not None:
                    self.assertEqual(region, by_index[agent.overlaps].regions[0])
                    continue
                for n in range(region.start - GAP, region.start + region.length + GAP):
                    self.assertNotIn((region.file, n), owned, "disjoint regions must keep a gap")
                for n in region.lines():
                    owned[(region.file, n)] = agent.index
        self.assertEqual(sum(1 for a in agents if a.overlaps is not None), 4)

    def test_verify_flags_a_lost_update(self) -> None:
        from benchmarks.contention import Agent, Outcome, Region, agent_line, base_line, verify

        args = argparse.Namespace(files=1, lines=10)
        agents = [Agent(0, [Region(0, 0, 2)]), Agent(1, [Region(0, 5, 2)])]
        outcomes = [Outcome(0, status="merged"), Outcome(1, status="merged")]
        lines = [base_line(0, n) for n in range(10)]
        lines[0], lines[1] = agent_line(0, 0, 0), agent_line(0, 1, 0)
        intact = "\n".join(lines) + "\n"
        lines[5], lines[6] = agent_line(0, 5, 1), agent_line(0, 6, 1)
        self.assertTrue(verify({0: "\n".join(lines) + "\n"}, agents, outcomes, args)["ok"])
        lost = verify({0: intact}, agents, outcomes, args)
        self.assertFalse(lost["ok"])
        self.assertEqual(lost["merged_edits_missing"], [1])


if __name__ == "__main__":
    unittest.main()
