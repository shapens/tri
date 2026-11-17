import argparse
import tempfile
import unittest
from pathlib import Path

from benchmarks.agentic import git_batch_runner_source, load_workflow_scenario, markdown_summary, mutation_specs, parse_targets, summarize_steps
from benchmarks.task_runner import DEFAULT_TASKS_ROOT, discover_tasks, select_tasks, validate_task_dir, markdown_summary as task_markdown_summary


class AgenticBenchmarkSummaryTests(unittest.TestCase):
    def test_summarize_steps_counts_embedded_calls_and_payload_bytes(self) -> None:
        totals = summarize_steps(
            [
                {"kind": "command", "wall_ms": 1.0, "stdin_bytes": 2, "stdout_bytes": 3, "stderr_bytes": 4},
                {"kind": "http", "wall_ms": 2.0, "stdin_bytes": 5, "stdout_bytes": 6, "stderr_bytes": 7},
                {"kind": "embedded", "wall_ms": 3.0, "response_bytes": 11},
                {"kind": "action", "wall_ms": 4.0},
            ],
            target_wall_ms=12.3456,
            store_bytes=99,
        )

        self.assertEqual(totals["commands"], 1)
        self.assertEqual(totals["external_commands"], 1)
        self.assertEqual(totals["requests"], 1)
        self.assertEqual(totals["http_requests"], 1)
        self.assertEqual(totals["embedded_calls"], 1)
        self.assertEqual(totals["transport_bytes"], 27)
        self.assertEqual(totals["response_bytes"], 11)
        self.assertEqual(totals["agent_io_bytes"], 38)
        self.assertEqual(totals["store_bytes"], 99)
        self.assertEqual(totals["setup_wall_ms"], 0)
        self.assertEqual(totals["workflow_wall_ms"], 10.0)
        self.assertEqual(totals["export_wall_ms"], 0)

    def test_summarize_steps_exposes_phase_wall_times(self) -> None:
        totals = summarize_steps(
            [
                {"name": "fixture.create", "kind": "action", "wall_ms": 2.0},
                {"name": "git.worktree.add.0", "kind": "command", "wall_ms": 3.0},
                {"name": "git.format_patch", "kind": "command", "wall_ms": 4.0},
                {"name": "task-verifier.hidden", "kind": "action", "wall_ms": 5.0},
            ],
            target_wall_ms=20.0,
        )

        self.assertEqual(totals["setup_wall_ms"], 2.0)
        self.assertEqual(totals["workflow_wall_ms"], 3.0)
        self.assertEqual(totals["export_wall_ms"], 4.0)
        self.assertEqual(totals["verify_wall_ms"], 5.0)

    def test_markdown_summary_exposes_embedded_calls_and_agent_io(self) -> None:
        results = {
            "created_at": "2026-05-31T00:00:00Z",
            "workdir": "/tmp/tri-bench",
            "config": {"attempts": 1, "files": 1, "file_size": 64},
            "targets": [
                {
                    "target": "tri",
                    "ok": True,
                    "skipped": False,
                    "details": {"transport": "embedded_batch"},
                    "totals": summarize_steps(
                        [{"name": "tri.batch", "kind": "embedded", "wall_ms": 1.0, "response_bytes": 123}],
                        target_wall_ms=1.0,
                        store_bytes=456,
                    ),
                }
            ],
        }

        summary = markdown_summary(results)

        self.assertIn("Embedded calls", summary)
        self.assertIn("Agent I/O bytes", summary)
        self.assertIn("Mutations per attempt", summary)
        self.assertIn("| tri | ok | 1.000 | 0.000 | 1.000 | 0.000 | 0 | 0 | 1 | 123 | 456 | embedded_batch |", summary)

    def test_markdown_summary_includes_verifier_outcomes(self) -> None:
        results = {
            "created_at": "2026-05-31T00:00:00Z",
            "workdir": "/tmp/tri-bench",
            "config": {"attempts": 1, "mutations_per_attempt": 1, "files": 1, "file_size": 64},
            "targets": [],
            "verification": {
                "tasks": [
                    {
                        "task_id": "tri-hidden-path-policy",
                        "ok": True,
                        "timed_out": False,
                        "reward": 1.0,
                        "wall_ms": 10.0,
                        "metadata": {"category": "policy_workflow"},
                    }
                ]
            },
        }

        summary = markdown_summary(results)

        self.assertIn("Tri Task Verifiers", summary)
        self.assertIn("| tri-hidden-path-policy | ok | 1.000 | 10.000 | policy_workflow |", summary)

    def test_parse_targets_accepts_tri_transport_variants(self) -> None:
        self.assertEqual(parse_targets("tri,tri-cli,tri-http,git-batch"), ["tri", "tri-cli", "tri-http", "git-batch"])

    def test_workflow_scenarios_are_loadable(self) -> None:
        scenario = load_workflow_scenario("small")

        self.assertEqual(scenario["id"], "small")
        self.assertEqual(scenario["attempts"], 1)
        self.assertEqual(scenario["mutations_per_attempt"], 1)
        self.assertEqual(scenario["targets"], ["tri", "git"])
        stress = load_workflow_scenario("stress")
        self.assertEqual(stress["mutations_per_attempt"], 8)
        self.assertIn("git-batch", stress["targets"])
        self.assertTrue(stress["verify_tasks"])

    def test_mutation_specs_keep_legacy_single_mutation_path(self) -> None:
        self.assertEqual(mutation_specs(2, 1), [{"path": "src/agent_2.txt", "text": "agent 2 attempt\n"}])
        self.assertEqual(
            mutation_specs(2, 2),
            [
                {"path": "src/agent_2_0.txt", "text": "agent 2 mutation 0\n"},
                {"path": "src/agent_2_1.txt", "text": "agent 2 mutation 1\n"},
            ],
        )

    def test_git_batch_runner_source_is_valid_python(self) -> None:
        args = argparse.Namespace(attempts=1, files=1, file_size=64, mutations_per_attempt=1)
        source = git_batch_runner_source(args)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "git-batch-runner.py"
            path.write_text(source)
            compile(source, str(path), "exec")

    def test_task_packages_are_discoverable_and_valid(self) -> None:
        tasks = discover_tasks(DEFAULT_TASKS_ROOT)
        ids = [validate_task_dir(path)["metadata"]["task_id"] for path in tasks]

        self.assertIn("tri-workspace-isolation", ids)
        self.assertIn("tri-hidden-path-policy", ids)
        self.assertIn("tri-conflict-resolution", ids)
        self.assertIn("tri-export-policy-gates", ids)
        self.assertIn("tri-secret-lease-redaction", ids)

    def test_task_selection_and_summary(self) -> None:
        tasks = discover_tasks(DEFAULT_TASKS_ROOT)
        selected = select_tasks(tasks, ["tri-conflict-resolution"])

        self.assertEqual([path.name for path in selected], ["tri-conflict-resolution"])
        summary = task_markdown_summary(
            {
                "created_at": "2026-05-31T00:00:00Z",
                "config": {"tasks_root": str(DEFAULT_TASKS_ROOT)},
                "tasks": [
                    {
                        "task_id": "tri-conflict-resolution",
                        "ok": True,
                        "timed_out": False,
                        "reward": 1.0,
                        "wall_ms": 12.0,
                        "metadata": {"category": "source_control_workflow"},
                    }
                ],
            }
        )

        self.assertIn("Tri Task Benchmark", summary)
        self.assertIn("| tri-conflict-resolution | ok | 1.000 | 12.000 | source_control_workflow |", summary)


if __name__ == "__main__":
    unittest.main()
