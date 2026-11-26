
import json
import tempfile
import unittest
from pathlib import Path

from experiments.report import (
    format_ci,
    generate_all,
    load_aggregate,
)


def summary(median, low=None, high=None, n=10):
        return {
        "n": 0 if median is None else n,
        "median": median,
        "ci_low": None if median is None else (
            median if low is None else low
        ),
        "ci_high": None if median is None else (
            median if high is None else high
        ),
    }


def synthetic_aggregate():
    configurations = {}

    for configuration, offset in (
        ("no_qos", 0),
        ("static_qos", -5),
    ):
        phases = {}

        for phase in ("baseline", "bulk_ramp", "bulk_steady"):
            phases[phase] = {
                "call": {
                    "forward_delay_ms_p95": summary(30 + offset),
                    "forward_delay_ms_p99": summary(45 + offset),
                    "loss_pct": summary(0.1),
                },
                "game": {
                    "forward_delay_ms_p95": summary(25 + offset),
                    "forward_delay_ms_p99": summary(40 + offset),
                    "loss_pct": summary(0.1),
                },
                "iso_download_mbps": summary(
                    None if phase == "baseline" else 75 + offset
                ) if phase != "baseline" else {
                    "n": 0,
                    "median": None,
                    "ci_low": None,
                    "ci_high": None,
                },
                "tv_jain_fairness_index": summary(0.99),
                "bulk_guarantee": {
                    "measured_runs": 10,
                    "runs_below_guarantee": 0,
                },
                "pooled_udp_supplementary": {
                    device: {
                        metric: {
                            "samples": 1000,
                            "p50": 10.0,
                            "p95": 15.0,
                            "p99": 20.0,
                        }
                        for metric in (
                            "forward_delay_ms",
                            "reverse_delay_ms",
                            "rtt_ms",
                        )
                    }
                    for device in ("call", "game")
                },
            }

        configurations[configuration] = {
            "run_counts": {
                "valid": 10,
                "invalid": 1,
                "failed": 0,
            },
            "phases": phases,
            "controller_convergence_s": "n/a",
            "host_cpu_max_pct": summary(35),
            "qdiscs": {
                target: {
                    "max_backlog_bytes": summary(1024),
                }
                for target in (
                    "modem_m_lan",
                    "modem_m_wan",
                    "router_r_wan",
                    "router_ifb0",
                )
            },
        }

    configurations["adaptive"] = {
        "status": "n/a",
        "reason": "not_implemented",
    }

    return {
        "experiment_id": "synthetic-test",
        "scenario": "iso-download",
        "expected_repetitions_per_configuration": 10,
        "discovered_runs": 22,
        "configurations": configurations,
    }


class ReportTests(unittest.TestCase):
    def test_format_ci_handles_missing_values(self):
        self.assertEqual(format_ci(None), "n/a")

        result = format_ci(summary(12.5, 10.0, 15.0))
        self.assertIn("12.500", result)
        self.assertIn("95% CI", result)
        self.assertIn("n=10", result)

    def test_generate_all_creates_report_and_plots(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment_dir = Path(directory)
            data = synthetic_aggregate()

            (experiment_dir / "aggregate.json").write_text(
                json.dumps(data)
            )

            generate_all(experiment_dir)

            expected = (
                "latency-comparison.png",
                "throughput-comparison.png",
                "system-health.png",
                "report.md",
            )

            for filename in expected:
                path = experiment_dir / filename
                self.assertTrue(path.is_file(), filename)
                self.assertGreater(path.stat().st_size, 0)

            report = (experiment_dir / "report.md").read_text()

            self.assertIn("adaptive", report)
            self.assertIn("not_implemented", report)
            self.assertIn("Pooled UDP percentiles", report)
            self.assertIn("Invalid runs: 1", report)
            self.assertIn("n/a", report)

    def test_load_aggregate_rejects_invalid_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aggregate.json"
            path.write_text(json.dumps({"scenario": "invalid"}))

            with self.assertRaises(ValueError):
                load_aggregate(path)


if __name__ == "__main__":
    unittest.main()
