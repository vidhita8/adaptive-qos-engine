import csv
from unittest import mock
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from experiments.aggregate import (
    aggregate_experiment,
    aggregate_configuration,
    bootstrap_median,
)
from experiments.schema import Scenario


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "experiments/scenarios/iso-download.yaml"


class AggregateTests(unittest.TestCase):
    def test_bootstrap_median_is_deterministic(self):
        values = [10, 20, 30, 40, 50]

        first = bootstrap_median(values, seed=123)
        second = bootstrap_median(values, seed=123)

        self.assertEqual(first, second)
        self.assertEqual(first["n"], 5)
        self.assertEqual(first["median"], 30)
        self.assertLessEqual(first["ci_low"], first["median"])
        self.assertGreaterEqual(first["ci_high"], first["median"])

    def test_single_value_has_degenerate_interval(self):
        result = bootstrap_median([12.5], seed=5)

        self.assertEqual(result["n"], 1)
        self.assertEqual(result["median"], 12.5)
        self.assertEqual(result["ci_low"], 12.5)
        self.assertEqual(result["ci_high"], 12.5)

    def test_empty_values_return_null_statistics(self):
        result = bootstrap_median([], seed=5)

        self.assertEqual(result["n"], 0)
        self.assertIsNone(result["median"])
        self.assertIsNone(result["ci_low"])
        self.assertIsNone(result["ci_high"])

    def test_empty_experiment_keeps_adaptive_as_na(self):
        scenario = Scenario.model_validate(
            yaml.safe_load(SCENARIO_PATH.read_text())
        )

        with tempfile.TemporaryDirectory() as directory:
            experiment_dir = Path(directory) / "synthetic-experiment"
            experiment_dir.mkdir()

            result = aggregate_experiment(experiment_dir, scenario)

            self.assertEqual(result["discovered_runs"], 0)
            self.assertEqual(
                result["configurations"]["adaptive"]["status"],
                "n/a",
            )
            self.assertEqual(
                result["configurations"]["no_qos"]["run_counts"]["valid"],
                0,
            )

    def test_invalid_and_failed_runs_are_counted(self):
        scenario = Scenario.model_validate(
            yaml.safe_load(SCENARIO_PATH.read_text())
        )

        with tempfile.TemporaryDirectory() as directory:
            experiment_dir = Path(directory) / "synthetic-experiment"
            experiment_dir.mkdir()

            cases = (
                ("no_qos-invalid", "no_qos", "completed", False),
                ("no_qos-failed", "no_qos", "failed", False),
            )

            for run_id, configuration, state, valid in cases:
                run_dir = experiment_dir / run_id
                run_dir.mkdir()

                (run_dir / "status.json").write_text(json.dumps({
                    "run_id": run_id,
                    "configuration": configuration,
                    "state": state,
                    "valid": valid,
                }))

            result = aggregate_experiment(experiment_dir, scenario)
            counts = result["configurations"]["no_qos"]["run_counts"]

            self.assertEqual(counts["valid"], 0)
            self.assertEqual(counts["invalid"], 1)
            self.assertEqual(counts["failed"], 1)


if __name__ == "__main__":
    unittest.main()


class PopulatedAggregateTests(unittest.TestCase):
    def test_http_percentiles_bulk_guarantee_and_convergence(self):
        scenario = Scenario.model_validate(
            yaml.safe_load(SCENARIO_PATH.read_text())
        )

        records = []
        for index, below in enumerate((True, False)):
            phases = {
                phase.name: {
                    "iso_download": {
                        "below_static_qos_guarantee": below,
                    }
                }
                for phase in scenario.phases
            }
            records.append({
                "configuration": "static_qos",
                "state": "completed",
                "valid": True,
                "metrics": {"phases": phases},
                "run_dir": Path("/tmp"),
            })

        with mock.patch(
            "experiments.aggregate.valid_metrics",
            return_value=records,
        ), mock.patch(
            "experiments.aggregate.metric_values",
            side_effect=lambda runs, phase, path: (
                [100.0, 200.0] if "download_ms" in path else []
            ),
        ), mock.patch(
            "experiments.aggregate.pooled_udp_percentiles",
            return_value={},
        ):
            result = aggregate_configuration(
                records, scenario, "static_qos", seed=42
            )

        baseline = result["phases"]["baseline"]

        self.assertEqual(
            baseline["http"]["tv1"]["download_ms"]["p95"]["median"],
            150.0,
        )
        self.assertEqual(baseline["bulk_guarantee"]["measured_runs"], 2)
        self.assertEqual(baseline["bulk_guarantee"]["runs_below_guarantee"], 1)
        self.assertEqual(result["controller_convergence_s"], "n/a")
