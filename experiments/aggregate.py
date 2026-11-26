#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from experiments.metrics import number, percentile, read_csv
from experiments.schema import Scenario


def load_run_records(experiment_dir: Path):
    records = []

    if not experiment_dir.is_dir():
        return records

    for run_dir in sorted(path for path in experiment_dir.iterdir() if path.is_dir()):
        status_path = run_dir / "status.json"
        metrics_path = run_dir / "metrics.json"

        if not status_path.is_file():
            continue

        try:
            status = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        metrics = None

        if metrics_path.is_file():
            try:
                metrics = json.loads(metrics_path.read_text())
            except (OSError, json.JSONDecodeError):
                pass

        records.append({
            "run_id": status.get("run_id", run_dir.name),
            "configuration": status.get("configuration"),
            "repetition": status.get("repetition"),
            "state": status.get("state", "unknown"),
            "valid": bool(
                status.get("state") == "completed"
                and status.get("valid") is True
                and metrics is not None
                and metrics.get("valid") is True
            ),
            "status": status,
            "metrics": metrics,
            "run_dir": run_dir,
        })

    return records


def bootstrap_median(values, seed, iterations=10000, confidence=0.95):
    values = np.asarray(
        [value for value in values if value is not None and np.isfinite(value)],
        dtype=float,
    )

    if len(values) == 0:
        return {
            "n": 0,
            "median": None,
            "ci_low": None,
            "ci_high": None,
        }

    median = float(np.median(values))

    if len(values) == 1:
        return {
            "n": 1,
            "median": round(median, 6),
            "ci_low": round(median, 6),
            "ci_high": round(median, 6),
        }

    rng = np.random.default_rng(seed)
    indices = rng.integers(
        0,
        len(values),
        size=(iterations, len(values)),
    )
    bootstrap_medians = np.median(values[indices], axis=1)

    alpha = (1 - confidence) / 2
    low, high = np.quantile(
        bootstrap_medians,
        [alpha, 1 - alpha],
    )

    return {
        "n": len(values),
        "median": round(median, 6),
        "ci_low": round(float(low), 6),
        "ci_high": round(float(high), 6),
    }


def summarize_values(values, seed):
    return bootstrap_median(values, seed)


def valid_metrics(records, configuration):
    return [
        record for record in records
        if record["configuration"] == configuration
        and record["valid"]
        and isinstance(record["metrics"], dict)
    ]


def pooled_udp_percentiles(records, phase):
    pooled = {
        "call": {"forward_delay_ms": [], "reverse_delay_ms": [], "rtt_ms": []},
        "game": {"forward_delay_ms": [], "reverse_delay_ms": [], "rtt_ms": []},
    }

    for record in records:
        for device in ("call", "game"):
            path = record["run_dir"] / "raw" / f"{device}.csv"

            rows = read_csv(path)

            for row in rows:
                elapsed = number(row.get("measurement_seconds"))
                lost = int(number(row.get("lost")) or 0)

                if (
                    elapsed is None
                    or not phase["from_s"] <= elapsed < phase["to_s"]
                    or lost
                ):
                    continue

                for field in pooled[device]:
                    value = number(row.get(field))
                    if value is not None:
                        pooled[device][field].append(value)

    return {
        device: {
            field: {
                "samples": len(values),
                "p50": percentile(values, 50),
                "p95": percentile(values, 95),
                "p99": percentile(values, 99),
            }
            for field, values in fields.items()
        }
        for device, fields in pooled.items()
    }


def metric_values(records, phase_name, path):
    values = []

    for record in records:
        current = record["metrics"]

        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)

        value = number(current)
        if value is not None:
            values.append(value)

    return values


def aggregate_configuration(records, scenario, configuration, seed):
    runs = valid_metrics(records, configuration)

    result = {
        "configuration": configuration,
        "run_counts": {
            "valid": len(runs),
            "invalid": sum(
                record["configuration"] == configuration
                and record["state"] == "completed"
                and not record["valid"]
                for record in records
            ),
            "failed": sum(
                record["configuration"] == configuration
                and record["state"] != "completed"
                for record in records
            ),
        },
        "phases": {},
    }

    for phase_index, phase in enumerate(scenario.phases):
        phase_result = {}
        phase_name = phase.name

        for device in ("call", "game"):
            device_result = {}

            for field in (
                "forward_delay_ms",
                "reverse_delay_ms",
                "rtt_ms",
                "jitter_ms",
            ):
                for quantile in ("p50", "p95", "p99"):
                    values = metric_values(
                        runs,
                        phase_name,
                        ("phases", phase_name, "udp", device, field, quantile),
                    )
                    device_result[f"{field}_{quantile}"] = summarize_values(
                        values,
                        seed + phase_index * 100 + len(device_result),
                    )

            device_result["loss_pct"] = summarize_values(
                metric_values(
                    runs,
                    phase_name,
                    ("phases", phase_name, "udp", device, "loss_pct"),
                ),
                seed + phase_index * 100 + 50,
            )

            device_result["reordered_packets"] = summarize_values(
                metric_values(
                    runs,
                    phase_name,
                    ("phases", phase_name, "udp", device, "reordered_packets"),
                ),
                seed + phase_index * 100 + 51,
            )

            phase_result[device] = device_result

        for field, path in (
            ("iso_download_mbps", ("iso_download", "throughput_mbps")),
            ("iso_download_link_share", ("iso_download", "link_share")),
            ("tv_jain_fairness_index", ("tv_jain_fairness_index",)),
        ):
            phase_result[field] = summarize_values(
                metric_values(
                    runs,
                    phase_name,
                    ("phases", phase_name, *path),
                ),
                seed + phase_index * 100 + 60,
            )

        starvation_values = [
            record["metrics"]["phases"][phase_name]["iso_download"][
                "below_static_qos_guarantee"
            ]
            for record in runs
            if record["metrics"]["phases"].get(phase_name, {}).get(
                "iso_download", {}
            ).get("below_static_qos_guarantee") is not None
        ]

        phase_result["bulk_guarantee"] = {
            "measured_runs": len(starvation_values),
            "runs_below_guarantee": sum(
                value is True for value in starvation_values
            ),
        }

        phase_result["http"] = {}

        for device in ("tv1", "tv2", "tv3"):
            phase_result["http"][device] = {}

            for field in (
                "throughput_mbps",
                "scheduled_chunks",
                "successful_chunks",
                "failed_chunks",
                "stalled_chunks",
                "delivery_pressure_stall_seconds",
            ):
                phase_result["http"][device][field] = summarize_values(
                    metric_values(
                        runs,
                        phase_name,
                        ("phases", phase_name, "http", device, field),
                    ),
                    seed + phase_index * 100 + 70 + len(
                        phase_result["http"][device]
                    ),
                )

            phase_result["http"][device]["download_ms"] = {}

            for quantile in ("p50", "p95", "p99"):
                phase_result["http"][device]["download_ms"][quantile] = (
                    summarize_values(
                        metric_values(
                            runs,
                            phase_name,
                            (
                                "phases", phase_name, "http",
                                device, "download_ms", quantile,
                            ),
                        ),
                        seed + phase_index * 100 + 80 + len(
                            phase_result["http"][device]["download_ms"]
                        ),
                    )
                )

        phase_result["pooled_udp_supplementary"] = pooled_udp_percentiles(
            runs,
            {
                "from_s": phase.from_s,
                "to_s": phase.to_s,
            },
        )

        result["phases"][phase_name] = phase_result

    for field, path in (
        ("host_cpu_mean_pct", ("system", "host_cpu_pct", "mean")),
        ("host_cpu_max_pct", ("system", "host_cpu_pct", "max")),
        ("host_memory_max_kb", ("system", "host_memory_used_kb", "max")),
        ("max_consecutive_high_cpu_samples", (
            "system", "host_cpu_pct", "max_consecutive_samples_above_limit"
        )),
    ):
        result[field] = summarize_values(
            [
                metric_values([record], "", path)[0]
                for record in runs
                if metric_values([record], "", path)
            ],
            seed + 900 + len(field),
        )

    result["controller_convergence_s"] = "n/a"

    result["qdiscs"] = {}

    for target in (
        "modem_m_lan",
        "modem_m_wan",
        "router_r_wan",
        "router_ifb0",
    ):
        result["qdiscs"][target] = {}

        for field in (
            "max_backlog_bytes",
            "max_backlog_packets",
            "max_cumulative_drops",
            "coverage_pct",
        ):
            result["qdiscs"][target][field] = summarize_values(
                metric_values(
                    runs,
                    "",
                    ("system", "qdiscs", target, field),
                ),
                seed + 1000 + len(result["qdiscs"][target]),
            )

    return result


def aggregate_experiment(experiment_dir: Path, scenario: Scenario):
    records = load_run_records(experiment_dir)
    configurations = {}

    for index, configuration in enumerate(
        ("no_qos", "static_qos", "adaptive")
    ):
        configured = next(
            (
                item for item in scenario.configurations
                if item.name == configuration
            ),
            None,
        )

        if configured is None or not configured.enabled:
            configurations[configuration] = {
                "status": "n/a",
                "reason": (
                    configured.reason if configured is not None
                    else "not configured"
                ),
            }
            continue

        configurations[configuration] = aggregate_configuration(
            records,
            scenario,
            configuration,
            scenario.seed + index * 10000,
        )

    return {
        "experiment_id": experiment_dir.name,
        "scenario": scenario.name,
        "expected_repetitions_per_configuration": scenario.repetitions,
        "discovered_runs": len(records),
        "configurations": configurations,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate Adaptive QoS experiment runs"
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--output")

    args = parser.parse_args()

    scenario_path = Path(args.scenario).resolve()
    experiment_dir = Path(args.experiment_dir).resolve()

    scenario = Scenario.model_validate(
        __import__("yaml").safe_load(scenario_path.read_text())
    )

    result = aggregate_experiment(experiment_dir, scenario)

    output = (
        Path(args.output).resolve()
        if args.output
        else experiment_dir / "aggregate.json"
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)

    print(f"[AGGREGATE] Saved: {output}")
    print(f"[AGGREGATE] Runs discovered: {result['discovered_runs']}")

    for name, summary in result["configurations"].items():
        if summary.get("status") == "n/a":
            print(f"[AGGREGATE] {name}: n/a ({summary['reason']})")
        else:
            counts = summary["run_counts"]
            print(
                f"[AGGREGATE] {name}: "
                f"{counts['valid']} valid, "
                f"{counts['invalid']} invalid, "
                f"{counts['failed']} failed"
            )


if __name__ == "__main__":
    main()
