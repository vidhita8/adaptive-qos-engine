#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def number(value):
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def percentile(values, q):
    return (
        round(float(np.percentile(values, q)), 6)
        if values else None
    )


def read_csv(path: Path):
    if not path.is_file():
        return []
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def rows_in_phase(rows, time_field, start, stop):
    return [
        row for row in rows
        if (
            (value := number(row.get(time_field))) is not None
            and start <= value < stop
        )
    ]


def udp_flow_metrics(rows):
    lost = sum(int(number(row.get("lost")) or 0) for row in rows)
    received = [
        row for row in rows
        if int(number(row.get("lost")) or 0) == 0
    ]
    sent = len(rows)

    result = {
        "samples_sent": sent,
        "samples_received": len(received),
        "lost_packets": lost,
        "loss_pct": round(100 * lost / sent, 6) if sent else None,
        "reordered_packets": sum(
            int(number(row.get("reordered")) or 0)
            for row in received
        ),
    }

    for field in (
        "forward_delay_ms",
        "reverse_delay_ms",
        "rtt_ms",
        "jitter_ms",
    ):
        values = [
            value for row in received
            if (value := number(row.get(field))) is not None
        ]
        result[field] = {
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "p99": percentile(values, 99),
            "mean": round(float(np.mean(values)), 6) if values else None,
        }

    return result


def http_flow_metrics(rows, phase_duration_s):
    successful = [
        row for row in rows
        if (number(row.get("bytes_received")) or 0) > 0
    ]
    received_bytes = sum(
        int(number(row.get("bytes_received")) or 0)
        for row in successful
    )
    download_ms = [
        value for row in successful
        if (value := number(row.get("download_ms"))) is not None
    ]
    stall_seconds = [
        value for row in rows
        if (value := number(row.get("stall_seconds"))) is not None
    ]
    stalled = sum(
        int(number(row.get("stalled")) or 0) for row in rows
    )

    return {
        "scheduled_chunks": len(rows),
        "successful_chunks": len(successful),
        "failed_chunks": len(rows) - len(successful),
        "stalled_chunks": stalled,
        "bytes_received": received_bytes,
        "throughput_mbps": (
            round(received_bytes * 8 / phase_duration_s / 1e6, 6)
            if phase_duration_s > 0 else None
        ),
        "download_ms": {
            "p50": percentile(download_ms, 50),
            "p95": percentile(download_ms, 95),
            "p99": percentile(download_ms, 99),
        },
        "delivery_pressure_stall_seconds": round(sum(stall_seconds), 6),
        "delivery_pressure_stall_events": stalled,
        "request_errors": [
            row["error"] for row in rows if row.get("error")
        ],
    }


def load_iperf_intervals(path: Path):
    if not path.is_file():
        return []

    payload = json.loads(path.read_text())
    intervals = []

    for item in payload.get("intervals", []):
        summary = item.get("sum_received") or item.get("sum") or {}
        start = number(summary.get("start"))
        stop = number(summary.get("end"))
        bps = number(summary.get("bits_per_second"))

        if start is not None and stop is not None and bps is not None and stop > start:
            intervals.append({
                "start_s": start,
                "end_s": stop,
                "bits_per_second": bps,
            })

    return intervals


def iperf_phase_metrics(intervals, flow_start_s, phase_start_s, phase_stop_s):
    active_start = max(flow_start_s, phase_start_s)

    if phase_stop_s <= active_start:
        return {
            "active": False,
            "throughput_mbps": None,
            "intervals": 0,
        }

    received_bits = 0.0
    used = 0

    for item in intervals:
        absolute_start = flow_start_s + item["start_s"]
        absolute_stop = flow_start_s + item["end_s"]

        overlap = max(
            0.0,
            min(absolute_stop, phase_stop_s)
            - max(absolute_start, active_start),
        )

        if overlap > 0:
            received_bits += item["bits_per_second"] * overlap
            used += 1

    active_duration = phase_stop_s - active_start
    throughput = (
        received_bits / active_duration / 1e6 if used else None
    )

    return {
        "active": True,
        "active_duration_s": round(active_duration, 6),
        "throughput_mbps": (
            round(throughput, 6) if throughput is not None else None
        ),
        "intervals": used,
    }


def find_backlogs(value):
    found = []

    if isinstance(value, dict):
        for key, child in value.items():
            if key == "backlog" and isinstance(child, dict):
                found.append(child)
            else:
                found.extend(find_backlogs(child))

    elif isinstance(value, list):
        for child in value:
            found.extend(find_backlogs(child))

    return found


def find_numbers(value, key):
    found = []

    if isinstance(value, dict):
        for current_key, child in value.items():
            if current_key == key:
                numeric_value = number(child)
                if numeric_value is not None:
                    found.append(numeric_value)
                elif isinstance(child, (dict, list)):
                    found.extend(find_numbers(child, key))
            else:
                found.extend(find_numbers(child, key))

    elif isinstance(value, list):
        for child in value:
            found.extend(find_numbers(child, key))

    return found


def monitor_metrics(path, measurement_start_ns, duration_s, cpu_limit_pct):
    records = []

    if path.is_file():
        with path.open() as file:
            for line in file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                mono = number(record.get("monotonic_ns"))
                if mono is None:
                    continue

                elapsed = (mono - measurement_start_ns) / 1e9

                if 0 <= elapsed < duration_s:
                    records.append((elapsed, record))

    records.sort(key=lambda item: item[0])

    expected_samples = math.ceil(duration_s)
    sample_count = len(records)
    sample_coverage_pct = (
        100 * sample_count / expected_samples
        if expected_samples else 0
    )

    elapsed_values = [elapsed for elapsed, _ in records]
    max_gap_s = (
        max(
            (right - left for left, right in zip(
                elapsed_values, elapsed_values[1:]
            )),
            default=None,
        )
    )

    cpu = [
        number(record.get("host", {}).get("cpu_pct"))
        for _, record in records
    ]
    cpu = [value for value in cpu if value is not None]

    memory = [
        number(record.get("host", {}).get("memory", {}).get("used_kb"))
        for _, record in records
    ]
    memory = [value for value in memory if value is not None]

    consecutive = 0
    max_consecutive = 0
    previous_high_time = None

    for elapsed, record in records:
        value = number(record.get("host", {}).get("cpu_pct"))

        if value is not None and value > cpu_limit_pct:
            if (
                previous_high_time is None
                or elapsed - previous_high_time <= 1.5
            ):
                consecutive += 1
            else:
                consecutive = 1

            previous_high_time = elapsed
        else:
            consecutive = 0
            previous_high_time = None

        max_consecutive = max(max_consecutive, consecutive)

    qdisc_summary = {}

    for target in (
        "modem_m_lan",
        "modem_m_wan",
        "router_r_wan",
        "router_ifb0",
    ):
        backlog_bytes = []
        backlog_packets = []
        drops = []
        samples = 0
        errors = 0

        for _, record in records:
            entry = record.get("qdiscs", {}).get(target, {})

            if (
                entry.get("returncode", 0) != 0
                or not isinstance(entry.get("data"), list)
                or not entry["data"]
            ):
                errors += 1
                continue

            samples += 1


            for qdisc in entry["data"]:
                stats = qdisc.get("stats2", {})

                # Modern tc JSON exposes backlog and qlen directly.
                direct_backlog = qdisc.get("backlog")
                direct_bytes = number(direct_backlog)
                direct_packets = number(qdisc.get("qlen"))

                if direct_bytes is not None:
                    backlog_bytes.append(direct_bytes)

                if direct_packets is not None:
                    backlog_packets.append(direct_packets)

                # Also support nested stats2.backlog from other tc versions.
                for backlog in find_backlogs(stats):
                    byte_count = number(backlog.get("bytes"))
                    packet_count = number(backlog.get("packets"))

                    if byte_count is not None:
                        backlog_bytes.append(byte_count)

                    if packet_count is not None:
                        backlog_packets.append(packet_count)

                direct_drops = number(qdisc.get("drops"))

                if direct_drops is not None:
                    drops.append(direct_drops)

                drops.extend(find_numbers(stats, "drops"))

        qdisc_summary[target] = {
            "samples": samples,
            "errors": errors,
            "coverage_pct": (
                round(100 * samples / expected_samples, 3)
                if expected_samples else 0
            ),
            "valid_coverage": (
                100 * samples / expected_samples >= 90
                if expected_samples else False
            ),
            "max_backlog_bytes": (
                max(backlog_bytes) if backlog_bytes else None
            ),
            "max_backlog_packets": (
                max(backlog_packets) if backlog_packets else None
            ),
            "max_cumulative_drops": max(drops) if drops else None,
        }

    return {
        "samples": sample_count,
        "coverage": {
            "expected_samples": expected_samples,
            "sample_coverage_pct": round(sample_coverage_pct, 3),
            "first_elapsed_s": (
                round(elapsed_values[0], 3) if elapsed_values else None
            ),
            "last_elapsed_s": (
                round(elapsed_values[-1], 3) if elapsed_values else None
            ),
            "max_gap_s": (
                round(max_gap_s, 3) if max_gap_s is not None else None
            ),
            "host_cpu_samples": len(cpu),
            "host_cpu_coverage_pct": (
                round(100 * len(cpu) / expected_samples, 3)
                if expected_samples else 0
            ),
            "valid": (
                sample_coverage_pct >= 90
                and (
                    100 * len(cpu) / expected_samples >= 90
                    if expected_samples else False
                )
            ),
        },
        "host_cpu_pct": {
            "mean": round(float(np.mean(cpu)), 6) if cpu else None,
            "p95": percentile(cpu, 95),
            "max": max(cpu) if cpu else None,
            "max_consecutive_samples_above_limit": max_consecutive,
        },
        "host_memory_used_kb": {
            "mean": round(float(np.mean(memory)), 3) if memory else None,
            "max": max(memory) if memory else None,
        },
        "qdiscs": qdisc_summary,
        "controller_process": None,
    }


def validate_saved_modem_qdiscs(metadata, scenario):
    reasons = []

    if not scenario.validity.expect_modem_rate_match:
        return reasons

    if not isinstance(metadata, dict):
        return ["Run metadata is missing; saved modem qdiscs cannot be validated"]

    snapshots = metadata.get("qdiscs", {})

    expected_rate = scenario.link.down_mbps * 1_000_000 / 8
    expected_delay = scenario.link.delay_ms / 1000
    expected_jitter = scenario.link.jitter_ms / 1000
    expected_loss = scenario.link.loss_pct / 100

    for key, interface in (
        ("modem_m_lan", "m-lan"),
        ("modem_m_wan", "m-wan"),
    ):
        snapshot = snapshots.get(key, {})
        qdiscs = snapshot.get("data")

        if snapshot.get("returncode") != 0 or not isinstance(qdiscs, list):
            reasons.append(
                f"Saved qdisc snapshot missing or failed for qos-modem:{interface}"
            )
            continue

        root = next(
            (item for item in qdiscs if item.get("root")),
            None,
        )
        child = next(
            (item for item in qdiscs if item.get("kind") == "netem"),
            None,
        )

        if (
            root is None
            or root.get("kind") != "tbf"
            or root.get("handle") != "1:"
        ):
            reasons.append(
                f"Saved qos-modem:{interface} root is not TBF handle 1:"
            )
            continue

        if (
            child is None
            or child.get("handle") != "10:"
            or child.get("parent") != "1:1"
        ):
            reasons.append(
                f"Saved qos-modem:{interface} netem child hierarchy mismatch"
            )
            continue

        actual_rate = number(root.get("options", {}).get("rate"))
        if actual_rate is None or abs(actual_rate - expected_rate) > 1:
            reasons.append(
                f"Saved qos-modem:{interface} rate mismatch: "
                f"expected {expected_rate:.0f} B/s, found {actual_rate}"
            )

        options = child.get("options", {})
        delay_options = options.get("delay", {})
        actual_delay = number(
            delay_options.get("delay")
            if isinstance(delay_options, dict)
            else delay_options
        )
        actual_jitter = number(
            delay_options.get("jitter")
            if isinstance(delay_options, dict)
            else None
        )
        actual_loss = number(options.get("loss"))
        actual_loss = actual_loss if actual_loss is not None else 0.0

        if actual_delay is None or abs(actual_delay - expected_delay) > 0.00001:
            reasons.append(
                f"Saved qos-modem:{interface} delay mismatch: "
                f"expected {expected_delay}s, found {actual_delay}"
            )

        if (
            actual_jitter is None
            or abs(actual_jitter - expected_jitter) > 0.00001
        ):
            reasons.append(
                f"Saved qos-modem:{interface} jitter mismatch: "
                f"expected {expected_jitter}s, found {actual_jitter}"
            )

        if abs(actual_loss - expected_loss) > 0.00001:
            reasons.append(
                f"Saved qos-modem:{interface} loss mismatch: "
                f"expected {expected_loss}, found {actual_loss}"
            )

    return reasons


def analyze_run(scenario, run_dir, traffic_result, configuration, metadata=None):
    raw_dir = Path(run_dir) / "raw"
    measurement_start_ns = int(traffic_result["measurement_start_ns"])

    udp_rows = {
        flow.id: read_csv(raw_dir / f"{flow.id}.csv")
        for flow in scenario.traffic if flow.kind == "udp_echo"
    }
    http_rows = {
        flow.id: read_csv(raw_dir / f"{flow.id}.csv")
        for flow in scenario.traffic if flow.kind == "http_chunks"
    }

    iso_flow = next(
        flow for flow in scenario.traffic
        if flow.kind == "iperf3_tcp"
    )
    intervals = load_iperf_intervals(
        raw_dir / f"{iso_flow.id}-iperf3.json"
    )

    phase_results = {}
    validity_reasons = []

    for phase in scenario.phases:
        duration = phase.to_s - phase.from_s
        phase_udp = {}
        combined_samples = 0

        for flow in scenario.traffic:
            if flow.kind != "udp_echo":
                continue

            selected = rows_in_phase(
                udp_rows[flow.id],
                "measurement_seconds",
                phase.from_s,
                phase.to_s,
            )
            phase_udp[flow.id] = udp_flow_metrics(selected)
            combined_samples += len(selected)

        if combined_samples < scenario.validity.min_samples_per_phase:
            validity_reasons.append(
                f"{phase.name}: {combined_samples} combined UDP samples; "
                f"minimum is {scenario.validity.min_samples_per_phase}"
            )

        phase_http = {}
        tv_rates = []

        for flow in scenario.traffic:
            if flow.kind != "http_chunks":
                continue

            selected = rows_in_phase(
                http_rows[flow.id],
                "scheduled_seconds",
                phase.from_s,
                phase.to_s,
            )

            result = http_flow_metrics(selected, duration)
            phase_http[flow.id] = result
            tv_rates.append(result["throughput_mbps"] or 0.0)

            # Every scheduled TV must deliver at least one successful
            # chunk during each analysis phase it overlaps.
            scheduled_in_phase = (
                flow.start_s < phase.to_s
                and flow.stop_s > phase.from_s
            )

            if (
                scheduled_in_phase
                and result["successful_chunks"] == 0
            ):
                validity_reasons.append(
                    f"{phase.name}: {flow.id} had zero successful "
                    "HTTP chunk transfers"
                )

        rate_sum = sum(tv_rates)
        rate_squares = sum(rate * rate for rate in tv_rates)
        jain = (
            rate_sum ** 2 / (len(tv_rates) * rate_squares)
            if tv_rates and rate_squares else None
        )

        bulk = iperf_phase_metrics(
            intervals,
            iso_flow.start_s,
            phase.from_s,
            phase.to_s,
        )

        bulk_mbps = bulk["throughput_mbps"]
        bulk_share = (
            bulk_mbps / scenario.link.down_mbps
            if bulk_mbps is not None else None
        )

        guarantee = scenario.link.down_mbps * 0.90 * 0.10
        starvation = None

        if configuration == "static_qos" and bulk["active"] and bulk_mbps is not None:
            starvation = bulk_mbps < guarantee

        phase_results[phase.name] = {
            "window_s": {
                "from": phase.from_s,
                "to": phase.to_s,
            },
            "udp": phase_udp,
            "combined_udp_samples": combined_samples,
            "http": phase_http,
            "tv_jain_fairness_index": (
                round(jain, 8) if jain is not None else None
            ),
            "iso_download": {
                **bulk,
                "link_share": (
                    round(bulk_share, 8) if bulk_share is not None else None
                ),
                "static_qos_guarantee_mbps": (
                    round(guarantee, 6)
                    if configuration == "static_qos" and bulk["active"]
                    else None
                ),
                "below_static_qos_guarantee": starvation,
            },
        }

    system = monitor_metrics(
        raw_dir / "monitor.jsonl",
        measurement_start_ns,
        scenario.duration_s,
        scenario.validity.max_host_cpu_pct,
    )

    coverage = system["coverage"]

    if coverage["sample_coverage_pct"] < 90:
        validity_reasons.append(
            "Monitor coverage below 90%: "
            f"{coverage['sample_coverage_pct']:.1f}%"
        )

    if coverage["host_cpu_coverage_pct"] < 90:
        validity_reasons.append(
            "Host CPU sample coverage below 90%: "
            f"{coverage['host_cpu_coverage_pct']:.1f}%"
        )

    for target, summary in system["qdiscs"].items():
        if summary["coverage_pct"] < 90:
            validity_reasons.append(
                f"Qdisc monitor coverage below 90% for {target}: "
                f"{summary['coverage_pct']:.1f}%"
            )

    high_cpu = system["host_cpu_pct"]["max_consecutive_samples_above_limit"]

    if high_cpu >= 5:
        validity_reasons.append(
            f"Host CPU exceeded {scenario.validity.max_host_cpu_pct:g}% "
            f"for {high_cpu} consecutive one-second samples"
        )

    modem_errors = validate_saved_modem_qdiscs(metadata, scenario)
    modem_validation = {
        "passed": not modem_errors,
        "errors": modem_errors,
    }

    if scenario.validity.expect_modem_rate_match:
        validity_reasons.extend(modem_errors)

    return {
        "configuration": configuration,
        "duration_s": scenario.duration_s,
        "phases": phase_results,
        "system": system,
        "modem_qdisc_validation": modem_validation,
        "controller_convergence_s": None,
        "valid": not validity_reasons,
        "validity_reasons": validity_reasons,
        "sample_threshold_scope": (
            "combined call and game UDP sent attempts per phase"
        ),
    }
