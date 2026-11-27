#!/usr/bin/env python3
"""Reproducible dataset generation, manifest builder, and verification."""

from __future__ import annotations
import os
import signal
import argparse
import csv
import json
import random
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from experiments.datasets.schema import (
    RESERVED_STATIC_PORTS,
    DatasetPlan,
    GeneratorConfig,
    TrafficSpec,
)
from traffic.flow_collector import FEATURE_FIELDS, METADATA_FIELDS


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = ROOT / "experiments" / "datasets" / "configs" / "broadband-dataset-v1.yaml"
DEFAULT_MANIFESTS_DIR = ROOT / "experiments" / "datasets" / "manifests"
DEFAULT_GENERATED_DIR = ROOT / "experiments" / "datasets" / "generated"
ISP_SERVER_IPV4 = "10.30.0.2"
EXPECTED_COLUMNS = list(METADATA_FIELDS + FEATURE_FIELDS)


def load_plan(path: Path) -> DatasetPlan:
    """Load and validate a YAML dataset generation plan."""
    data = yaml.safe_load(path.read_text())
    return DatasetPlan.model_validate(data)


def stream_protocol(stream: TrafficSpec) -> str:
    if stream.kind == "udp_dataset":
        return "udp"
    return "tcp"


def allocate_ports(plan: DatasetPlan, config: GeneratorConfig) -> dict[str, int]:
    """Deterministically assign unique randomized server ports for one config."""
    rng = random.Random(config.seed)
    used_ports = set(RESERVED_STATIC_PORTS)
    ports: dict[str, int] = {}

    span = plan.port_range.max_port - plan.port_range.min_port + 1
    if span - len(RESERVED_STATIC_PORTS) < len(config.streams):
        raise ValueError(
            f"Port range too small for {len(config.streams)} streams in {config.config_id}"
        )

    for stream in config.streams:
        while True:
            candidate = rng.randint(plan.port_range.min_port, plan.port_range.max_port)
            if candidate not in used_ports:
                used_ports.add(candidate)
                ports[stream.id] = candidate
                break

    return ports


def build_manifest_dict(
    config: GeneratorConfig,
    ports: dict[str, int],
) -> dict[str, Any]:
    """Build a JSON-serializable flow ground-truth manifest for one config."""
    flows = []
    for stream in config.streams:
        entry: dict[str, Any] = {
            "stream_id": stream.id,
            "device_id": stream.device_id,
            "protocol": stream_protocol(stream),
            "server_port": ports[stream.id],
            "label": stream.label,
        }
        if stream.kind == "iperf3_tcp":
            entry["min_packets"] = stream.min_packets
            entry["min_bytes"] = stream.min_bytes
        flows.append(entry)

    return {
        "generator_config_id": config.config_id,
        "seed": config.seed,
        "modem_rate": config.modem_rate,
        "flows": flows,
    }


def write_manifests(
    plan: DatasetPlan,
    manifests_dir: Path,
) -> dict[str, Path]:
    """Generate and save deterministic manifests for every config in the plan."""
    manifests_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for config in plan.configs:
        ports = allocate_ports(plan, config)
        manifest = build_manifest_dict(config, ports)
        manifest_path = manifests_dir / f"{config.config_id}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        written[config.config_id] = manifest_path

    return written


def _netns_popen(
    namespace: str,
    command: list[str],
    log_path: Path | None = None,
) -> subprocess.Popen:
    """Launch a command inside a network namespace in its own process group."""
    full_cmd = ["sudo", "-n", "ip", "netns", "exec", namespace, *command]

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w") as log_handle:
            return subprocess.Popen(
                full_cmd,
                cwd=ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )

    return subprocess.Popen(
        full_cmd,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

def _wait_for_ports(ports: list[int], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    remaining = set(ports)
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["sudo", "-n", "ip", "netns", "exec", "qos-isp", "ss", "-H", "-lntu"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            listening = {
                int(match.group(1))
                for match in re.finditer(r":(\d+)\s", result.stdout)
            }
            remaining -= listening
            if not remaining:
                return
        time.sleep(0.2)

    raise TimeoutError(f"Servers did not listen on ports: {sorted(remaining)}")


def _set_modem_rate(rate: str) -> None:
    subprocess.run(
        ["sudo", "-n", str(ROOT / "testbed" / "change-rate.sh"), rate],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )


class CleanupError(RuntimeError):
    """Raised when one or more processes could not be terminated during cleanup."""


def _process_group_exists(pgid: int) -> bool:
    """Return whether a process group still exists."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(pgid: int, sig: int) -> None:
    """Signal a process group, ignoring the case where it no longer exists."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _terminate_procs(
    procs: list[subprocess.Popen],
    label: str,
) -> list[str]:
    """Terminate tracked process groups, escalate if necessary, and reap leaders.

    Every tracked Popen must have been created with start_new_session=True.
    Returns cleanup failures for the caller to report.
    """
    failures: list[str] = []
    unique_procs = {proc.pid: proc for proc in procs}

    # First signal every process group. Do this before waiting on any one group.
    for proc in unique_procs.values():
        try:
            _signal_process_group(proc.pid, signal.SIGTERM)
        except OSError as exc:
            failures.append(
                f"{label}: SIGTERM failed for process group {proc.pid}: {exc}"
            )

    # Give all groups time to exit. A dead Popen leader does not imply that
    # its descendants have exited.
    deadline = time.monotonic() + 5
    remaining = set(unique_procs)

    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            proc = unique_procs[pid]

            try:
                proc.poll()  # Reap the leader if it has exited.
            except OSError as exc:
                failures.append(f"{label}: poll failed for pid {pid}: {exc}")
                remaining.discard(pid)
                continue

            try:
                exists = _process_group_exists(pid)
            except OSError as exc:
                failures.append(
                    f"{label}: could not inspect process group {pid}: {exc}"
                )
                remaining.discard(pid)
                continue

            if not exists:
                remaining.discard(pid)

        if remaining:
            time.sleep(0.05)

    # Escalate groups that remain, including groups whose sudo leader exited.
    for pid in sorted(remaining):
        print(
            f"[CLEANUP] {label} process group {pid} survived SIGTERM — "
            "sending SIGKILL",
            flush=True,
        )
        try:
            _signal_process_group(pid, signal.SIGKILL)
        except OSError as exc:
            failures.append(
                f"{label}: SIGKILL failed for process group {pid}: {exc}"
            )

    # Reap every Popen leader and check whether any process groups survived.
    for pid, proc in unique_procs.items():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            failures.append(f"{label}: process leader pid={pid} survived SIGKILL")
        except OSError as exc:
            failures.append(f"{label}: wait failed for pid={pid}: {exc}")

        try:
            if _process_group_exists(pid):
                failures.append(
                    f"{label}: process group {pid} still exists after cleanup"
                )
        except OSError as exc:
            failures.append(
                f"{label}: could not verify process group {pid}: {exc}"
            )

    return failures

_WAIT_POLL_INTERVAL = 0.25   # seconds between polls while waiting for iperf3 to finish


def _run_iperf3_rounds_impl(
    stream_ns: str,
    port: int,
    stream: Any,
    log_path: Path,
    errors: list[str],
    all_procs: list[subprocess.Popen],
    all_procs_lock: threading.Lock,
    stop_event: threading.Event,

) -> None:
    """Run iperf3 rounds in a worker thread, tracking every Popen in all_procs.

    Synchronization contract
    ------------------------
    The check-then-create-then-register sequence is performed while holding
    ``all_procs_lock``.  Cleanup acquires the same lock (after setting
    ``stop_event``) before iterating ``all_procs``.  This makes the sequence
    atomic relative to cleanup: either the worker creates and registers a process
    before cleanup scans the list, or cleanup sets ``stop_event`` and acquires the
    lock first so the worker's ``stop_event.is_set()`` check inside the lock will
    observe ``True`` and return without creating a new subprocess.

    The lock is released before the bounded wait loop so that cleanup can acquire
    it to scan ``all_procs`` without being blocked by a waiting worker thread.

    If ``stop_event`` fires while the worker is waiting for the current
    subprocess to finish, the worker returns. Main-thread cleanup owns
    process-group termination for every registered subprocess."""
    if stream.start_delay_s > 0:
        stop_event.wait(timeout=stream.start_delay_s)
        if stop_event.is_set():
            return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log_handle:
        for round_idx in range(stream.rounds):
            # --- Atomic: check stop → create Popen → register ---
            # Hold the lock for the entire check-create-register sequence so
            # cleanup cannot scan all_procs between the check and the append.
            with all_procs_lock:
                if stop_event.is_set():
                    return

                cmd = [
                    "sudo", "-n", "ip", "netns", "exec", stream_ns,
                    "iperf3", "-c", ISP_SERVER_IPV4, "-p", str(port),
                    "-P", str(stream.streams),
                    "-t", str(stream.round_duration_s),
                ]
                if stream.direction == "download":
                    cmd.append("-R")
                if stream.bitrate:
                    cmd.extend(["-b", stream.bitrate])

                proc = subprocess.Popen(
                    cmd,
                    cwd=ROOT,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                all_procs.append(proc)
            # --- Lock released; waiting never holds the lock ---

            # Bounded wait: poll every _WAIT_POLL_INTERVAL seconds so the thread
            # responds to stop_event promptly even if proc.wait() would block.
            while True:
                try:
                    rc = proc.wait(timeout=_WAIT_POLL_INTERVAL)
                    break   # process finished normally
                except subprocess.TimeoutExpired:
                    if stop_event.is_set():
                        # Main-thread cleanup owns process-group termination.
                        return
            if rc != 0:
                errors.append(
                    f"{stream.id} round {round_idx + 1} failed with code {rc}"
                )
                return
            if stream.gap_s > 0 and round_idx + 1 < stream.rounds:
                stop_event.wait(timeout=stream.gap_s)

def _run_iperf3_rounds(
    stream_ns: str,
    port: int,
    stream: Iperf3BulkSpec,
    log_path: Path,
    errors: list[str],
    all_procs: list[subprocess.Popen],
    all_procs_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """Run iperf3 rounds and propagate worker exceptions to the main thread."""
    try:
        _run_iperf3_rounds_impl(
            stream_ns,
            port,
            stream,
            log_path,
            errors,
            all_procs,
            all_procs_lock,
            stop_event,
        )
    except Exception as exc:
        errors.append(
            f"{stream.id} worker failed: {type(exc).__name__}: {exc}"
        )
def run_config(
    plan: DatasetPlan,
    config: GeneratorConfig,
    manifest_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Execute one generator configuration on the live testbed and capture flows.

    Every subprocess is registered in a shared process registry. This includes
    servers, clients, the flow collector, and iperf3 clients launched by worker
    threads.
    During cleanup, the main thread sets stop_event, snapshots the registry
    under its lock, terminates all registered process groups, joins worker
    threads, and restores the modem rate. Cleanup failures are reported without
    hiding an earlier collection exception.
    """
    ports = allocate_ports(plan, config)
    config_dir = output_dir / config.config_id
    logs_dir = config_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    csv_path = config_dir / "flows.csv"
    stats_path = config_dir / "stats.json"
    if csv_path.exists():
        csv_path.unlink()
    if stats_path.exists():
        stats_path.unlink()

    udp_ports = [
        ports[s.id] for s in config.streams if s.kind == "udp_dataset"
    ]
    http_ports = [
        ports[s.id] for s in config.streams if s.kind == "http_chunks"
    ]
    iperf_ports = [
        ports[s.id] for s in config.streams if s.kind == "iperf3_tcp"
    ]

    # Unified process registry — every Popen is appended here immediately after
    # creation.  The finally block terminates all entries unconditionally.
    all_procs: list[subprocess.Popen] = []
    all_procs_lock = threading.Lock()

    def register_proc(proc: subprocess.Popen) -> None:
        with all_procs_lock:
            all_procs.append(proc)

    # Signalled before cleanup so worker threads stop launching new subprocesses.
    stop_event = threading.Event()

    threads: list[threading.Thread] = []
    thread_errors: list[str] = []
    collector: subprocess.Popen | None = None
    client_procs: list[subprocess.Popen] = []

    print(
        f"[DATASET] Running {config.config_id} (seed={config.seed}, "
        f"modem_rate={config.modem_rate}, duration={config.capture_duration_s}s)",
        flush=True,
    )

    primary_error: BaseException | None = None
    primary_traceback = None
    cleanup_errors: list[str] = []

    try:
        _set_modem_rate(config.modem_rate)

        # --- Servers ---
        if udp_ports:
            proc = _netns_popen(
                "qos-isp",
                [sys.executable, "traffic/udp_echo_server.py", "--ports", *map(str, udp_ports)],
                logs_dir / "udp-echo-server.log",
            )
            register_proc(proc)
        for port in http_ports:
            proc = _netns_popen(
                "qos-isp",
                [sys.executable, "traffic/http_chunk_server.py", "--port", str(port)],
                logs_dir / f"http-server-{port}.log",
            )
            register_proc(proc)
        for port in iperf_ports:
            proc = _netns_popen(
                "qos-isp",
                ["iperf3", "-s", "-p", str(port)],
                logs_dir / f"iperf3-server-{port}.log",
            )
            register_proc(proc)

        _wait_for_ports(udp_ports + http_ports + iperf_ports)

        # --- Flow collector ---
        collector = _netns_popen(
            "qos-router",
            [
                sys.executable,
                "-m",
                "traffic.flow_collector",
                "--duration",
                str(config.capture_duration_s),
                "--idle-timeout",
                str(plan.idle_timeout_s),
                "--manifest",
                str(manifest_path),
                "--output",
                str(csv_path),
                "--stats-output",
                str(stats_path),
            ],
            logs_dir / "flow-collector.log",
        )
        register_proc(collector)
        time.sleep(1.5)

        # --- Clients ---
        for idx, stream in enumerate(config.streams):
            stream_seed = config.seed * 100 + idx + 1
            namespace = f"qos-{stream.device_id}"
            port = ports[stream.id]

            if stream.kind == "udp_dataset":
                cmd = [
                    sys.executable,
                    "traffic/dataset_udp_client.py",
                    "--server", ISP_SERVER_IPV4,
                    "--port", str(port),
                    "--rate", str(stream.rate),
                    "--duration", str(stream.duration_s),
                    "--sessions", str(stream.sessions),
                    "--rounds", str(stream.rounds),
                    "--drain-ms", str(stream.drain_ms),
                    "--pattern", stream.pattern,
                    "--burst-size", str(stream.burst_size),
                    "--intra-burst-gap-ms", str(stream.intra_burst_gap_ms),
                    "--min-payload-bytes", str(stream.min_payload_bytes),
                    "--max-payload-bytes", str(stream.max_payload_bytes),
                    "--seed", str(stream_seed),
                ]
                proc = _netns_popen(namespace, cmd, logs_dir / f"{stream.id}.log")
                register_proc(proc)
                client_procs.append(proc)

            elif stream.kind == "http_chunks":
                cmd = [
                    sys.executable,
                    "traffic/http_scheduled_client.py",
                    "--server", ISP_SERVER_IPV4,
                    "--port", str(port),
                    "--min-chunk-bytes", str(stream.min_chunk_bytes),
                    "--max-chunk-bytes", str(stream.max_chunk_bytes),
                    "--min-interval", str(stream.min_interval_s),
                    "--max-interval", str(stream.max_interval_s),
                    "--duration", str(stream.duration_s),
                    "--start-s", str(stream.start_delay_s),
                    "--seed", str(stream_seed),
                    "--output", str(config_dir / f"{stream.id}-chunks.csv"),
                ]
                proc = _netns_popen(namespace, cmd, logs_dir / f"{stream.id}.log")
                register_proc(proc)
                client_procs.append(proc)

            elif stream.kind == "iperf3_tcp":
                # Worker threads append their own Popen objects to all_procs so
                # that cleanup can terminate them even if the thread is mid-round.
                thread = threading.Thread(
                    target=_run_iperf3_rounds,
                    args=(
                        namespace, port, stream,
                        logs_dir / f"{stream.id}.log",
                        thread_errors,
                        all_procs,
                        all_procs_lock,
                        stop_event,
                    ),
                    daemon=False,
                )
                thread.start()
                threads.append(thread)

        # --- Wait for clients (UDP / HTTP) ---
        client_timeout = config.capture_duration_s + 30
        for proc in client_procs:
            try:
                rc = proc.wait(timeout=client_timeout)
            except subprocess.TimeoutExpired:
                raise TimeoutError(
                    f"UDP/HTTP client pid={proc.pid} did not finish within "
                    f"{client_timeout}s in {config.config_id}"
                )
            if rc != 0:
                raise RuntimeError(
                    f"UDP/HTTP client pid={proc.pid} exited with code {rc} "
                    f"in {config.config_id}"
                )

        # --- Wait for iperf3 worker threads ---
        thread_timeout = config.capture_duration_s + 30
        for thread in threads:
            thread.join(timeout=thread_timeout)
            if thread.is_alive():
                raise TimeoutError(
                    f"iperf3 worker thread timed out after {thread_timeout}s "
                    f"in {config.config_id}"
                )

        if thread_errors:
            raise RuntimeError("; ".join(thread_errors))

        # --- Wait for collector ---
        try:
            collector_rc = collector.wait(timeout=config.capture_duration_s + 30)
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"flow_collector did not exit within "
                f"{config.capture_duration_s + 30}s in {config.config_id}"
            )
        if collector_rc != 0:
            raise RuntimeError(f"flow_collector exited with code {collector_rc}")

    except BaseException as exc:
        primary_error = exc
        primary_traceback = exc.__traceback__
    finally:
        # 1. Stop workers from launching additional subprocesses.
        stop_event.set()

        # 2. Snapshot every registered process under the shared lock.
        try:
            with all_procs_lock:
                procs_to_cleanup = list(all_procs)
        except Exception as exc:
            procs_to_cleanup = []
            cleanup_errors.append(
                f"failed to snapshot process registry: {exc}"
            )

        # 3. Terminate process groups without holding the registry lock.
        try:
            failures = _terminate_procs(
                procs_to_cleanup,
                label=config.config_id,
            )
            cleanup_errors.extend(failures)
        except Exception as exc:
            cleanup_errors.append(
                f"process-group termination failed: {exc}"
            )

        # 4. Join every worker thread. The stop event interrupts its
        # bounded waits; process termination is owned by this thread.
        for thread in threads:
            try:
                thread.join()
            except Exception as exc:
                cleanup_errors.append(
                    f"failed to join worker thread {thread.name}: {exc}"
                )
            if thread.is_alive():
                cleanup_errors.append(
                    f"worker thread {thread.name} is still alive"
                )

        # 5. Restore the modem rate, even if an earlier cleanup step failed.
        try:
            _set_modem_rate("100mbit")
        except Exception as exc:
            cleanup_errors.append(
                f"failed to restore modem rate to 100mbit: {exc}"
            )

    # 6. Report cleanup failures without hiding the original collection error.
    if primary_error is not None:
        for error in cleanup_errors:
            primary_error.add_note(f"Cleanup failure: {error}")
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_errors:
        raise CleanupError(
            f"Cleanup failed for {config.config_id}: "
            + "; ".join(cleanup_errors)
        )
    stats = json.loads(stats_path.read_text()) if stats_path.is_file() else {}
    print(f"[DATASET] Completed {config.config_id}: {stats.get('exported_by_label', {})}", flush=True)
    return {
        "config_id": config.config_id,
        "csv_path": str(csv_path),
        "stats": stats,
    }


def merge_and_verify(
    csv_paths: list[Path],
    merged_output: Path,
    min_flows_per_class: int = 2000,
) -> dict[str, Any]:
    """Merge per-config CSVs, verify schema & privacy guarantees, and count flows."""
    merged_rows: list[dict[str, str]] = []
    class_counts: Counter[str] = Counter()
    device_counts: Counter[str] = Counter()
    config_counts: Counter[str] = Counter()
    configs_per_class: dict[str, set[str]] = defaultdict(set)
    protocol_counts: Counter[str] = Counter()

    flow_counter = 1

    for path in csv_paths:
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != EXPECTED_COLUMNS:
                raise ValueError(
                    f"Unexpected columns in {path}: {reader.fieldnames}"
                )
            for row in reader:
                for col in EXPECTED_COLUMNS:
                    val = row.get(col)
                    if val is None or val == "" or val.lower() == "nan":
                        raise ValueError(f"Missing or NaN field {col!r} in {path}")

                row["flow_id"] = f"flow-{flow_counter:08d}"
                flow_counter += 1
                merged_rows.append(row)

                label = row["label"]
                cfg_id = row["generator_config_id"]
                class_counts[label] += 1
                device_counts[row["device_id"]] += 1
                config_counts[cfg_id] += 1
                configs_per_class[label].add(cfg_id)
                protocol_counts[row["protocol"]] += 1

    merged_output.parent.mkdir(parents=True, exist_ok=True)
    with merged_output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        writer.writerows(merged_rows)

    expected_classes = ("interactive", "gaming", "streaming", "bulk")
    below_threshold = {
        cls: class_counts.get(cls, 0)
        for cls in expected_classes
        if class_counts.get(cls, 0) < min_flows_per_class
    }

    summary = {
        "total_flows": len(merged_rows),
        "columns": EXPECTED_COLUMNS,
        "class_counts": dict(sorted(class_counts.items())),
        "device_counts": dict(sorted(device_counts.items())),
        "config_counts": dict(sorted(config_counts.items())),
        "configs_per_class": {
            cls: sorted(cfgs) for cls, cfgs in sorted(configs_per_class.items())
        },
        "protocol_counts": dict(sorted(protocol_counts.items())),
        "min_flows_per_class": min_flows_per_class,
        "meets_threshold": not below_threshold,
        "below_threshold": below_threshold,
    }
    return summary



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dataset generation and verification CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate")
    p_val.add_argument("--plan", type=Path, default=DEFAULT_PLAN)

    p_man = sub.add_parser("generate-manifests")
    p_man.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    p_man.add_argument("--manifests-dir", type=Path, default=DEFAULT_MANIFESTS_DIR)

    p_run = sub.add_parser("run")
    p_run.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    p_run.add_argument("--manifests-dir", type=Path, default=DEFAULT_MANIFESTS_DIR)
    p_run.add_argument("--output-dir", type=Path, default=DEFAULT_GENERATED_DIR)
    p_run.add_argument("--configs", nargs="*", default=None)
    p_run.add_argument("--min-per-class", type=int, default=2000)

    p_ver = sub.add_parser("verify")
    p_ver.add_argument("--csv", type=Path, required=True)
    p_ver.add_argument("--min-per-class", type=int, default=2000)

    args = parser.parse_args()

    if args.command == "validate":
        plan = load_plan(args.plan)
        print(f"[VALID] {plan.name}: {len(plan.configs)} generator configs")
        return

    if args.command == "generate-manifests":
        plan = load_plan(args.plan)
        written = write_manifests(plan, args.manifests_dir)
        print(
            f"[MANIFESTS] Wrote {len(written)} manifest(s) "
            f"to {args.manifests_dir}"
        )
        return

    if args.command == "run":
        plan = load_plan(args.plan)
        manifests = write_manifests(plan, args.manifests_dir)
        run_root = args.output_dir / plan.name
        selected = set(args.configs) if args.configs else None

        # Ensure requested config IDs actually exist in the plan.
        known_configs = {cfg.config_id for cfg in plan.configs}
        if selected is not None:
            unknown_configs = selected - known_configs
            if unknown_configs:
                parser.error(
                    "Unknown config ID(s): "
                    + ", ".join(sorted(unknown_configs))
                )

        csv_paths: list[Path] = []
        run_results = []

        for cfg in plan.configs:
            cfg_csv = run_root / cfg.config_id / "flows.csv"
            stats_path = run_root / cfg.config_id / "stats.json"

            if selected is not None and cfg.config_id not in selected:
                # Reuse existing results for configs not selected in this run.
                if not cfg_csv.is_file():
                    raise FileNotFoundError(
                        f"Existing CSV required for skipped config "
                        f"{cfg.config_id}: {cfg_csv}"
                    )

                if not stats_path.is_file():
                    raise FileNotFoundError(
                        f"Existing stats required for skipped config "
                        f"{cfg.config_id}: {stats_path}"
                    )

                run_results.append({
                    "config_id": cfg.config_id,
                    "csv_path": str(cfg_csv),
                    "stats": json.loads(stats_path.read_text()),
                })
                csv_paths.append(cfg_csv)
                continue

            res = run_config(
                plan, cfg, manifests[cfg.config_id], run_root
            )
            run_results.append(res)
            csv_paths.append(cfg_csv)

        merged_csv = run_root / "flows.csv"
        summary = merge_and_verify(
            csv_paths,
            merged_csv,
            min_flows_per_class=args.min_per_class,
        )
        summary["plan_name"] = plan.name
        summary["runs"] = run_results

        summary_path = run_root / "dataset_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )

        print(json.dumps(summary, indent=2, sort_keys=True))

        if not summary["meets_threshold"]:
            sys.exit(1)

        return

    if args.command == "verify":
        summary = merge_and_verify(
            [args.csv],
            args.csv,
            min_flows_per_class=args.min_per_class,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))

        if not summary["meets_threshold"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
