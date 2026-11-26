import json
import tempfile
import unittest
from pathlib import Path

from experiments.metrics import (
    iperf_phase_metrics,
    monitor_metrics,
    udp_flow_metrics,
)


class MetricsTests(unittest.TestCase):
    def test_udp_loss_and_percentiles(self):
        rows = [
            {"lost": "0", "forward_delay_ms": "10", "reverse_delay_ms": "11",
             "rtt_ms": "21", "jitter_ms": "1", "reordered": "0"},
            {"lost": "1", "forward_delay_ms": "", "reverse_delay_ms": "",
             "rtt_ms": "", "jitter_ms": "", "reordered": ""},
            {"lost": "0", "forward_delay_ms": "20", "reverse_delay_ms": "12",
             "rtt_ms": "32", "jitter_ms": "2", "reordered": "1"},
        ]

        result = udp_flow_metrics(rows)

        self.assertEqual(result["samples_sent"], 3)
        self.assertEqual(result["samples_received"], 2)
        self.assertEqual(result["lost_packets"], 1)
        self.assertAlmostEqual(result["loss_pct"], 100 / 3, places=5)
        self.assertEqual(result["reordered_packets"], 1)
        self.assertEqual(result["forward_delay_ms"]["p50"], 15)

    def test_iperf_phase_offset(self):
        intervals = [
            {"start_s": 0, "end_s": 1, "bits_per_second": 8_000_000},
            {"start_s": 1, "end_s": 2, "bits_per_second": 16_000_000},
            {"start_s": 2, "end_s": 3, "bits_per_second": 24_000_000},
        ]

        result = iperf_phase_metrics(intervals, 30, 30, 32)

        self.assertEqual(result["intervals"], 2)
        self.assertEqual(result["throughput_mbps"], 12)

    def test_monitor_cpu_streak_breaks_after_missing_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.jsonl"

            with path.open("w") as output:
                for second in (0, 1, 2, 5, 6, 7, 8):
                    output.write(json.dumps({
                        "monotonic_ns": (
                            1_000_000_000 + second * 1_000_000_000
                        ),
                        "host": {"cpu_pct": 95},
                        "qdiscs": {},
                    }) + "\n")

            result = monitor_metrics(path, 1_000_000_000, 10, 90)

            # The gap from t=2 to t=5 must break the CPU streak.
            self.assertEqual(
                result["host_cpu_pct"][
                    "max_consecutive_samples_above_limit"
                ],
                4,
            )

    def test_monitor_coverage_reports_missing_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.jsonl"

            with path.open("w") as output:
                for second in range(8):
                    output.write(json.dumps({
                        "monotonic_ns": (
                            1_000_000_000 + second * 1_000_000_000
                        ),
                        "host": {
                            "cpu_pct": 20,
                            "memory": {"used_kb": 1000},
                        },
                        "qdiscs": {
                            name: {
                                "returncode": 0,
                                "data": [{"stats2": {}}],
                            }
                            for name in (
                                "modem_m_lan",
                                "modem_m_wan",
                                "router_r_wan",
                                "router_ifb0",
                            )
                        },
                    }) + "\n")

            result = monitor_metrics(path, 1_000_000_000, 10, 90)

            self.assertEqual(result["samples"], 8)
            self.assertEqual(
                result["coverage"]["sample_coverage_pct"],
                80,
            )
            self.assertEqual(
                result["qdiscs"]["modem_m_lan"]["coverage_pct"],
                80,
            )

    def test_monitor_backlog_is_not_transmitted_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.jsonl"

            with path.open("w") as output:
                for index in range(5):
                    record = {
                        "monotonic_ns": 1_000_000_000 + index * 1_000_000_000,
                        "host": {
                            "cpu_pct": 95,
                            "memory": {"used_kb": 1000},
                        },
                        "qdiscs": {
                            name: {
                                "returncode": 0,
                                "data": [{
                                    "stats2": {
                                        "basic": {"bytes": 9_000_000},
                                        "backlog": {
                                            "bytes": 12 + index,
                                            "packets": 2,
                                        },
                                        "drops": index,
                                    }
                                }],
                            }
                            for name in (
                                "modem_m_lan",
                                "modem_m_wan",
                                "router_r_wan",
                                "router_ifb0",
                            )
                        },
                    }
                    output.write(json.dumps(record) + "\n")

            result = monitor_metrics(path, 1_000_000_000, 10, 90)

            self.assertEqual(
                result["host_cpu_pct"]["max_consecutive_samples_above_limit"],
                5,
            )
            self.assertEqual(
                result["qdiscs"]["modem_m_lan"]["max_backlog_bytes"],
                16,
            )


    def test_monitor_parses_top_level_tc_qdisc_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.jsonl"

            with path.open("w") as output:
                for index in range(5):
                    record = {
                        "monotonic_ns": 1_000_000_000
                        + index * 1_000_000_000,
                        "host": {
                            "cpu_pct": 10,
                            "memory": {"used_kb": 1000},
                        },
                        "qdiscs": {
                            name: {
                                "returncode": 0,
                                "data": [
                                    {
                                        "kind": "tbf",
                                        "backlog": 100 + index,
                                        "qlen": 5 + index,
                                        "drops": index,
                                    },
                                    {
                                        "kind": "netem",
                                        "backlog": 200 + index,
                                        "qlen": 10 + index,
                                        "drops": index + 1,
                                    },
                                ],
                            }
                            for name in (
                                "modem_m_lan",
                                "modem_m_wan",
                                "router_r_wan",
                                "router_ifb0",
                            )
                        },
                    }

                    output.write(json.dumps(record) + "\n")

            result = monitor_metrics(path, 1_000_000_000, 10, 90)

            modem = result["qdiscs"]["modem_m_lan"]

            self.assertEqual(modem["max_backlog_bytes"], 204)
            self.assertEqual(modem["max_backlog_packets"], 14)
            self.assertEqual(modem["max_cumulative_drops"], 5)


if __name__ == "__main__":
    unittest.main()
