import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from experiments.orchestrator import (
    build_run_plan,
    execute_campaign,
)
from experiments.schema import Scenario


SCENARIO_PATH = (
    Path(__file__).resolve().parents[1]
    / "experiments"
    / "scenarios"
    / "iso-download.yaml"
)


class OrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = Scenario.model_validate(
            yaml.safe_load(SCENARIO_PATH.read_text())
        )

    def test_plan_is_reproducible_and_paired(self):
        first = build_run_plan(self.scenario)
        second = build_run_plan(self.scenario)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 20)

        for repetition in range(1, 11):
            block = [
                item for item in first
                if item["repetition"] == repetition
            ]
            self.assertEqual(len(block), 2)
            self.assertEqual(
                {item["configuration"] for item in block},
                {"no_qos", "static_qos"},
            )
            self.assertEqual(len({item["seed"] for item in block}), 1)

    def test_invalid_first_attempt_is_retried_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def executor(**kwargs):
                calls.append(kwargs["run_id"])
                run_dir = root / kwargs["experiment_id"] / kwargs["run_id"]
                run_dir.mkdir(parents=True)
                valid = kwargs["run_id"].endswith("attempt-2")
                (run_dir / "status.json").write_text(
                    json.dumps({
                        "state": "completed",
                        "valid": valid,
                    })
                )

            result = execute_campaign(
                self.scenario,
                SCENARIO_PATH,
                "retry-test",
                root,
                executor,
            )
            experiment_dir = root / "retry-test"

            self.assertTrue(
                (experiment_dir / "aggregate.json").is_file()
            )
            self.assertTrue(
                (experiment_dir / "report.md").is_file()
            )

            for filename in (
                "latency-comparison.png",
                "throughput-comparison.png",
                "system-health.png",
            ):
                self.assertTrue(
                    (experiment_dir / filename).is_file(),
                    filename,
                )
            self.assertEqual(len(calls), 40)
            self.assertEqual(result["successful_runs"], 20)
            self.assertEqual(result["unsuccessful_runs"], 0)

    def test_valid_runs_are_skipped_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            experiment_dir = root / "resume-test"
            experiment_dir.mkdir()

            plan = build_run_plan(self.scenario)

            for item in plan:
                run_dir = experiment_dir / item["run_id"]
                run_dir.mkdir()
                (run_dir / "status.json").write_text(
                    json.dumps({
                        "state": "completed",
                        "valid": True,
                    })
                )

            executor = Mock()
            experiment_dir = root / "resume-test"
            result = execute_campaign(
                self.scenario,
                SCENARIO_PATH,
                "resume-test",
                root,
                executor,
            )

            executor.assert_not_called()
            self.assertEqual(result["successful_runs"], 20)

    def test_failed_attempts_are_not_repeated_more_than_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def executor(**kwargs):
                calls.append(kwargs["run_id"])
                run_dir = root / kwargs["experiment_id"] / kwargs["run_id"]
                run_dir.mkdir(parents=True)
                (run_dir / "status.json").write_text(
                    json.dumps({
                        "state": "failed",
                        "valid": False,
                    })
                )

            result = execute_campaign(
                self.scenario,
                SCENARIO_PATH,
                "failure-test",
                root,
                executor,
            )

            self.assertEqual(len(calls), 40)
            self.assertEqual(result["successful_runs"], 0)
            self.assertEqual(result["unsuccessful_runs"], 20)


if __name__ == "__main__":
    unittest.main()