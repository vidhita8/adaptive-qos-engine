import csv
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from experiments.metrics import analyze_run
from experiments.schema import Scenario


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "experiments/scenarios/iso-download.yaml"


class MetricsIntegrationTests(unittest.TestCase):
    def test_complete_three_phase_analysis(self):
        scenario = Scenario.model_validate(
            yaml.safe_load(SCENARIO_PATH.read_text())
        )

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            raw = run_dir / "raw"
            raw.mkdir()

            # Generate UDP samples at the configured call/game rates.
            for flow in scenario.traffic:
                if flow.kind != "udp_echo":
                    continue

                rate = flow.pps
                path = raw / f"{flow.id}.csv"

                with path.open("w", newline="") as output:
                    writer = csv.DictWriter(
                        output,
                        fieldnames=[
                            "sequence", "measurement_seconds",
                            "client_send_ns", "server_receive_ns",
                            "server_send_ns", "client_receive_ns",
                            "forward_delay_ms", "reverse_delay_ms",
                            "rtt_ms", "jitter_ms", "reordered", "lost",
                        ],
                    )
                    writer.writeheader()

                    sequence = 0
                    for phase in scenario.phases:
                        count = int(rate * (phase.to_s - phase.from_s))
                        for index in range(count):
                            elapsed = phase.from_s + index / rate
                            writer.writerow({
                                "sequence": sequence,
                                "measurement_seconds": elapsed,
                                "client_send_ns": 1,
                                "server_receive_ns": 2,
                                "server_send_ns": 3,
                                "client_receive_ns": 4,
                                "forward_delay_ms": 10,
                                "reverse_delay_ms": 11,
                                "rtt_ms": 21,
                                "jitter_ms": 1,
                                "reordered": 0,
                                "lost": 0,
                            })
                            sequence += 1

            # Generate one successful HTTP chunk every two seconds per TV.
            for flow in scenario.traffic:
                if flow.kind != "http_chunks":
                    continue

                path = raw / f"{flow.id}.csv"
                with path.open("w", newline="") as output:
                    writer = csv.DictWriter(
                        output,
                        fieldnames=[
                            "chunk", "scheduled_seconds", "started_seconds",
                            "schedule_lag_ms", "download_ms",
                            "bytes_received", "stall_seconds", "stalled",
                            "error",
                        ],
                    )
                    writer.writeheader()

                    chunk = 0
                    elapsed = flow.start_s
                    while elapsed < flow.stop_s:
                        if elapsed >= 0:
                            writer.writerow({
                                "chunk": chunk,
                                "scheduled_seconds": elapsed,
                                "started_seconds": elapsed,
                                "schedule_lag_ms": 0,
                                "download_ms": 500,
                                "bytes_received": flow.chunk_bytes,
                                "stall_seconds": 0,
                                "stalled": 0,
                                "error": "",
                            })
                            chunk += 1
                        elapsed += flow.interval_s

            # Simulate one-second iperf3 receiver intervals for the
            # 90-second ISO download, beginning at measurement t=30.
            intervals = [
                {
                    "sum_received": {
                        "start": second,
                        "end": second + 1,
                        "bits_per_second": 80_000_000,
                    }
                }
                for second in range(90)
            ]
            (raw / "iso-iperf3.json").write_text(
                json.dumps({"intervals": intervals})
            )

            # Simulate one-second monitor records across the measurement.
            measurement_start_ns = 1_000_000_000
            monitor_path = raw / "monitor.jsonl"

            with monitor_path.open("w") as output:
                for second in range(120):
                    qdiscs = {
                        name: {
                            "returncode": 0,
                            "data": [{
                                "stats2": {
                                    "backlog": {
                                        "bytes": 1024,
                                        "packets": 2,
                                    },
                                    "drops": 0,
                                }
                            }],
                        }
                        for name in (
                            "modem_m_lan",
                            "modem_m_wan",
                            "router_r_wan",
                            "router_ifb0",
                        )
                    }

                    output.write(json.dumps({
                        "monotonic_ns": (
                            measurement_start_ns + second * 1_000_000_000
                        ),
                        "host": {
                            "cpu_pct": 20,
                            "memory": {"used_kb": 1_000_000},
                        },
                        "qdiscs": qdiscs,
                    }) + "\n")

            result = analyze_run(
                scenario,
                run_dir,
                {"measurement_start_ns": measurement_start_ns},
                "static_qos",
                metadata={
                    "qdiscs": {
                        key: {
                            "returncode": 0,
                            "data": [
                                {
                                    "kind": "tbf",
                                    "handle": "1:",
                                    "root": True,
                                    "options": {
                                        "rate": 12_500_000,
                                        "burst": 131072,
                                        "lat": 241172,
                                    },
                                },
                                {
                                    "kind": "netem",
                                    "handle": "10:",
                                    "parent": "1:1",
                                    "options": {
                                        "limit": 2048,
                                        "delay": {
                                            "delay": 0.01,
                                            "jitter": 0.002,
                                        },
                                        "loss": 0.0,
                                    },
                                },
                            ],
                        }
                        for key in ("modem_m_lan", "modem_m_wan")
                    }
                },
            )

            self.assertTrue(result["valid"], result["validity_reasons"])

            phases = result["phases"]

            self.assertEqual(
                phases["baseline"]["combined_udp_samples"], 3300
            )
            self.assertEqual(
                phases["bulk_ramp"]["combined_udp_samples"], 1100
            )
            self.assertEqual(
                phases["bulk_steady"]["combined_udp_samples"], 8800
            )

            self.assertFalse(
                phases["baseline"]["iso_download"]["active"]
            )
            self.assertAlmostEqual(
                phases["bulk_ramp"]["iso_download"]["throughput_mbps"],
                80,
            )
            self.assertAlmostEqual(
                phases["bulk_steady"]["iso_download"]["throughput_mbps"],
                80,
            )

            self.assertAlmostEqual(
                phases["bulk_steady"]["tv_jain_fairness_index"], 1.0
            )

            self.assertEqual(
                result["system"]["qdiscs"]["modem_m_lan"][
                    "max_backlog_bytes"
                ],
                1024,
            )


if __name__ == "__main__":
    unittest.main()
