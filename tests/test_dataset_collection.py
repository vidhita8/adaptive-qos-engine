import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from experiments.datasets.collect import (
    DEFAULT_PLAN,
    EXPECTED_COLUMNS,
    allocate_ports,
    build_manifest_dict,
    load_plan,
    main,
    merge_and_verify,
    write_manifests,
)
from experiments.datasets.schema import RESERVED_STATIC_PORTS, DatasetPlan
from traffic.flow_collector import load_manifest
class DatasetCollectionTests(unittest.TestCase):

    def test_default_plan_loads_and_covers_all_classes_and_devices(self):
        plan = load_plan(DEFAULT_PLAN)
        self.assertGreaterEqual(len(plan.configs), 4)

        labels = {stream.label for cfg in plan.configs for stream in cfg.streams}
        devices = {stream.device_id for cfg in plan.configs for stream in cfg.streams}
        laptop_labels = {
            stream.label
            for cfg in plan.configs
            for stream in cfg.streams
            if stream.device_id == "laptop"
        }

        self.assertEqual(labels, {"interactive", "gaming", "streaming", "bulk"})
        self.assertEqual(
            devices,
            {"call", "game", "tv1", "tv2", "tv3", "bulk", "laptop"},
        )
        self.assertEqual(laptop_labels, {"interactive", "gaming", "streaming", "bulk"})

    def test_duplicate_seeds_are_rejected(self):
        payload = {
            "name": "bad-plan",
            "configs": [
                {
                    "config_id": "c1",
                    "seed": 42,
                    "capture_duration_s": 10,
                    "streams": [
                        {
                            "id": "s1",
                            "device_id": "call",
                            "label": "interactive",
                            "kind": "udp_dataset",
                            "rate": 20,
                            "duration_s": 2,
                        }
                    ],
                },
                {
                    "config_id": "c2",
                    "seed": 42,
                    "capture_duration_s": 10,
                    "streams": [
                        {
                            "id": "s2",
                            "device_id": "call",
                            "label": "interactive",
                            "kind": "udp_dataset",
                            "rate": 20,
                            "duration_s": 2,
                        }
                    ],
                },
            ],
        }
        with self.assertRaises(ValidationError):
            DatasetPlan.model_validate(payload)

    def test_port_allocation_is_deterministic_unique_and_avoids_static_ports(self):
        plan = load_plan(DEFAULT_PLAN)
        cfg = plan.configs[0]

        ports_a = allocate_ports(plan, cfg)
        ports_b = allocate_ports(plan, cfg)
        self.assertEqual(ports_a, ports_b)
        self.assertEqual(len(set(ports_a.values())), len(cfg.streams))
        self.assertTrue(RESERVED_STATIC_PORTS.isdisjoint(ports_a.values()))

    def test_generated_manifests_load_cleanly_in_flow_collector(self):
        plan = load_plan(DEFAULT_PLAN)
        with tempfile.TemporaryDirectory() as directory:
            written = write_manifests(plan, Path(directory))
            self.assertEqual(len(written), len(plan.configs))

            for cfg in plan.configs:
                entries = load_manifest(written[cfg.config_id])
                self.assertEqual(len(entries), len(cfg.streams))
                for entry in entries:
                    if entry.label == "bulk":
                        self.assertGreater(entry.min_packets, 1)
                        self.assertGreater(entry.min_bytes, 1)
                    else:
                        self.assertEqual(entry.min_packets, 1)

    def test_build_manifest_dict_sets_bulk_thresholds(self):
        plan = load_plan(DEFAULT_PLAN)
        cfg = plan.configs[0]
        manifest = build_manifest_dict(cfg, allocate_ports(plan, cfg))
        bulk_entries = [f for f in manifest["flows"] if f["label"] == "bulk"]
        self.assertTrue(bulk_entries)
        for item in bulk_entries:
            self.assertGreaterEqual(item["min_packets"], 40)
            self.assertGreaterEqual(item["min_bytes"], 32768)

    def test_merge_and_verify_checks_schema_and_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            c1 = tmp / "c1.csv"
            c2 = tmp / "c2.csv"
            merged = tmp / "merged.csv"

            def write_sample(path: Path, cfg_id: str, labels: list[str]) -> None:
                with path.open("w", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=EXPECTED_COLUMNS)
                    writer.writeheader()
                    for idx, label in enumerate(labels):
                        writer.writerow({
                            "flow_id": f"flow-{idx:08d}",
                            "device_id": "laptop",
                            "generator_config_id": cfg_id,
                            "label": label,
                            "protocol": "udp" if label in ("interactive", "gaming") else "tcp",
                            "packet_size_mean": "160.0",
                            "packet_size_std": "10.0",
                            "inter_arrival_mean": "0.02",
                            "inter_arrival_std": "0.005",
                            "duration_s": "2.0",
                            "bytes_per_second": "8000.0",
                            "packets_per_second": "50.0",
                            "direction_ratio": "0.5",
                        })

            write_sample(c1, "cfg-1", ["interactive", "gaming", "streaming", "bulk"])
            write_sample(c2, "cfg-2", ["interactive", "gaming", "streaming", "bulk"])

            summary = merge_and_verify([c1, c2], merged, min_flows_per_class=2)
            self.assertTrue(summary["meets_threshold"])
            self.assertEqual(summary["total_flows"], 8)
            self.assertEqual(summary["class_counts"], {
                "bulk": 2,
                "gaming": 2,
                "interactive": 2,
                "streaming": 2,
            })

            summary_strict = merge_and_verify([c1, c2], merged, min_flows_per_class=3)
            self.assertFalse(summary_strict["meets_threshold"])


    def test_run_summary_includes_skipped_configs(self):
        plan = load_plan(DEFAULT_PLAN)

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            run_root = tmp / plan.name

            selected_cfg = plan.configs[0]

            # Prepare existing results for every skipped config.
            expected_stats = {}

            for cfg in plan.configs[1:]:
                config_dir = run_root / cfg.config_id
                config_dir.mkdir(parents=True)

                csv_path = config_dir / "flows.csv"
                csv_path.write_text(",".join(EXPECTED_COLUMNS) + "\n")

                stats = {
                    "exported_flows": 100,
                    "exported_by_label": {"bulk": 100},
                }
                (config_dir / "stats.json").write_text(
                    json.dumps(stats)
                )
                expected_stats[cfg.config_id] = stats

            selected_result = {
                "config_id": selected_cfg.config_id,
                "csv_path": str(
                    run_root / selected_cfg.config_id / "flows.csv"
                ),
                "stats": {"exported_flows": 50},
            }

            fake_manifests = {
                cfg.config_id: tmp / f"{cfg.config_id}.json"
                for cfg in plan.configs
            }

            fake_summary = {
                "total_flows": 750,
                "meets_threshold": True,
            }

            with (
                patch(
                    "experiments.datasets.collect.load_plan",
                    return_value=plan,
                ),
                patch(
                    "experiments.datasets.collect.write_manifests",
                    return_value=fake_manifests,
                ),
                patch(
                    "experiments.datasets.collect.run_config",
                    return_value=selected_result,
                ) as mocked_run,
                patch(
                    "experiments.datasets.collect.merge_and_verify",
                    return_value=fake_summary,
                ),
                patch(
                    "experiments.datasets.collect.DEFAULT_GENERATED_DIR",
                    tmp,
                ),
                patch(
                    "experiments.datasets.collect.DEFAULT_MANIFESTS_DIR",
                    tmp / "manifests",
                ),
                patch(
                    "sys.argv",
                    [
                        "collect.py",
                        "run",
                        "--configs",
                        selected_cfg.config_id,
                    ],
                ),
            ):
                main()

            mocked_run.assert_called_once()

            summary_path = run_root / "dataset_summary.json"
            summary = json.loads(summary_path.read_text())

            self.assertEqual(len(summary["runs"]), len(plan.configs))

            actual_config_ids = {
                item["config_id"] for item in summary["runs"]
            }
            expected_config_ids = {
                cfg.config_id for cfg in plan.configs
            }
            self.assertEqual(actual_config_ids, expected_config_ids)

            for item in summary["runs"]:
                if item["config_id"] in expected_stats:
                    self.assertEqual(
                        item["stats"],
                        expected_stats[item["config_id"]],
                    )

    def test_run_fails_when_skipped_config_csv_is_missing(self):
        plan = load_plan(DEFAULT_PLAN)

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            run_root = tmp / plan.name

            selected_cfg = plan.configs[0]
            skipped_cfg = plan.configs[1]

            # Stats exist, but the skipped config CSV does not.
            config_dir = run_root / skipped_cfg.config_id
            config_dir.mkdir(parents=True)
            (config_dir / "stats.json").write_text("{}")

            fake_manifests = {
                cfg.config_id: tmp / f"{cfg.config_id}.json"
                for cfg in plan.configs
            }

            with (
                patch(
                    "experiments.datasets.collect.load_plan",
                    return_value=plan,
                ),
                patch(
                    "experiments.datasets.collect.write_manifests",
                    return_value=fake_manifests,
                ),
                patch(
                    "experiments.datasets.collect.run_config",
                    return_value={},
                ),
                patch(
                    "experiments.datasets.collect.DEFAULT_GENERATED_DIR",
                    tmp,
                ),
                patch(
                    "experiments.datasets.collect.DEFAULT_MANIFESTS_DIR",
                    tmp / "manifests",
                ),
                patch(
                    "sys.argv",
                    [
                        "collect.py",
                        "run",
                        "--configs",
                        selected_cfg.config_id,
                    ],
                ),
            ):
                with self.assertRaisesRegex(
                    FileNotFoundError,
                    "Existing CSV required for skipped config",
                ):
                    main()

if __name__ == "__main__":
    unittest.main()
