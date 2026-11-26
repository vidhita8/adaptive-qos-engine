
#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from experiments.metadata import collect_metadata, write_metadata
from experiments.metrics import analyze_run
from experiments.monitor import RunMonitor
from experiments.schema import Scenario


ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "experiments" / "results"


class RunManager:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.processes: dict[str, subprocess.Popen] = {}
        self.stopping = False
        self.log_handles = {}

    def start_process(
        self,
        name: str,
        command: list[str],
        namespace: str | None = None,
        output_file: Path | None = None,
    ) -> subprocess.Popen:
        if name in self.processes:
            raise RuntimeError(f"Process already registered: {name}")

        actual_command = list(command)

        if namespace:
            actual_command = [
                "sudo",
                "-n",
                "ip",
                "netns",
                "exec",
                namespace,
                *actual_command,
            ]
        stdout = subprocess.DEVNULL
        stderr = subprocess.STDOUT

        if output_file:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = output_file.open("w")
            self.log_handles[name] = log_handle
            stdout = log_handle

        process = subprocess.Popen(
            actual_command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            process_group=0,
        )

        self.processes[name] = process
        return process

    def stop_processes(self):
        if self.stopping:
            return

        self.stopping = True

        for process in self.processes.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

        deadline = time.monotonic() + 5

        for process in self.processes.values():
            remaining = max(0, deadline - time.monotonic())

            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

                process.wait()

        for log_handle in self.log_handles.values():
            log_handle.close()

        self.log_handles.clear()

    def process_status(self):
        return {
            name: {
                "pid": process.pid,
                "returncode": process.poll(),
            }
            for name, process in self.processes.items()
        }


def run_privileged(command: list[str]):
    subprocess.run(
        ["sudo", *command],
        cwd=ROOT,
        check=True,
    )


def wait_for_listener(
    manager: RunManager,
    name: str,
    port: int,
    timeout: float = 10.0,
):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        process = manager.processes[name]

        if process.poll() is not None:
            raise RuntimeError(
                f"{name} exited before port {port} became ready"
            )

        result = subprocess.run(
            [
                "sudo",
                "-n",
                "ip",
                "netns",
                "exec",
                "qos-isp",
                "ss",
                "-H",
                "-lntu",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            pattern = rf":{port}\s"

            if re.search(pattern, result.stdout):
                print(f"[SERVER] {name} ready on port {port}")
                return

        time.sleep(0.25)

    raise TimeoutError(
        f"{name} did not listen on port {port} within {timeout}s"
    )


def start_servers(manager: RunManager):
    manager.start_process(
        "udp_echo",
        [
            sys.executable,
            "traffic/udp_echo_server.py",
            "--ports",
            "6001",
            "6002",
        ],
        namespace="qos-isp",
        output_file=manager.run_dir / "logs" / "udp-echo.log",
    )

    manager.start_process(
        "http_chunk",
        [
            sys.executable,
            "traffic/http_chunk_server.py",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
        ],
        namespace="qos-isp",
        output_file=manager.run_dir / "logs" / "http-chunk.log",
    )

    manager.start_process(
        "iperf3",
        ["iperf3", "-s", "-p", "5201"],
        namespace="qos-isp",
        output_file=manager.run_dir / "logs" / "iperf3.log",
    )

    wait_for_listener(manager, "udp_echo", 6001)
    wait_for_listener(manager, "udp_echo", 6002)
    wait_for_listener(manager, "http_chunk", 8080)
    wait_for_listener(manager, "iperf3", 5201)


def wait_until_ns(target_ns: int):
    """Wait against the shared monotonic experiment clock."""
    while True:
        remaining_ns = target_ns - time.monotonic_ns()

        if remaining_ns <= 0:
            return

        time.sleep(min(remaining_ns / 1e9, 0.05))



def check_servers_alive(manager: RunManager):
    for name in ("udp_echo", "http_chunk", "iperf3"):
        process = manager.processes.get(name)

        if process is None:
            raise RuntimeError(f"Required server was not started: {name}")

        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"Server {name} exited during the experiment "
                f"with return code {return_code}"
            )


def run_traffic(scenario: Scenario, manager: RunManager):
    raw_dir = manager.run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # All clients share one measurement-time-zero timestamp.
    # Negative start_s values place traffic in warm-up.
    measurement_start_ns = (
        time.monotonic_ns()
        + int(scenario.warmup_s * 1e9)
    )

    client_names = []
    download_flow = None

    for flow in scenario.traffic:
        if flow.kind == "iperf3_tcp":
            if flow.direction != "download":
                raise ValueError(
                    f"Unsupported ISO direction: {flow.direction}"
                )

            if download_flow is not None:
                raise ValueError(
                    "This scenario supports one ISO download flow"
                )

            download_flow = flow
            continue

        namespace = f"qos-{flow.device}"
        output_file = raw_dir / f"{flow.id}.csv"

        if flow.kind == "udp_echo":
            command = [
                sys.executable,
                "traffic/udp_echo_client.py",
                "--server",
                "10.30.0.2",
                "--port",
                str(flow.port),
                "--rate",
                str(flow.pps),
                "--payload-bytes",
                str(flow.payload_bytes),
                "--measurement-start-ns",
                str(measurement_start_ns),
                "--start-s",
                str(flow.start_s),
                "--stop-s",
                str(flow.stop_s),
                "--output",
                str(output_file),
            ]

        elif flow.kind == "http_chunks":
            command = [
                sys.executable,
                "traffic/http_scheduled_client.py",
                "--server",
                "10.30.0.2",
                "--port",
                str(flow.port),
                "--chunk-bytes",
                str(flow.chunk_bytes),
                "--interval",
                str(flow.interval_s),
                "--measurement-start-ns",
                str(measurement_start_ns),
                "--start-s",
                str(flow.start_s),
                "--stop-s",
                str(flow.stop_s),
                "--output",
                str(output_file),
            ]

        else:
            raise ValueError(
                f"Unsupported traffic kind: {flow.kind}"
            )

        manager.start_process(
            flow.id,
            command,
            namespace=namespace,
            output_file=manager.run_dir / "logs" / f"{flow.id}.log",
        )

        client_names.append(flow.id)

        print(
            f"[TRAFFIC] Started {flow.id} in {namespace}; "
            f"scheduled {flow.start_s}s to {flow.stop_s}s"
        )

    if download_flow is None:
        raise ValueError("Scenario has no iperf3 ISO download flow")

    # Start the download at its scheduled time relative to measurement t=0.
    download_start_ns = (
        measurement_start_ns
        + int(download_flow.start_s * 1e9)
    )

    wait_until_ns(download_start_ns)

    download_duration = (
        download_flow.stop_s - download_flow.start_s
    )

    iperf_output = raw_dir / f"{download_flow.id}-iperf3.json"

    manager.start_process(
        download_flow.id,
        [
            "iperf3",
            "-c",
            "10.30.0.2",
            "-p",
            str(download_flow.port),
            "-R",
            "-P",
            str(download_flow.streams),
            "-t",
            str(int(download_duration)),
            "-J",
        ],
        namespace=f"qos-{download_flow.device}",
        output_file=iperf_output,
    )

    client_names.append(download_flow.id)

    print(
        f"[TRAFFIC] Started {download_flow.id} at "
        f"t={download_flow.start_s}s using reverse iperf3"
    )

    # Wait until the latest configured flow stop time.
    latest_stop_s = max(flow.stop_s for flow in scenario.traffic)

    measurement_stop_ns = (
        measurement_start_ns + int(latest_stop_s * 1e9)
    )

    wait_until_ns(measurement_stop_ns)

    # Allow UDP clients to collect late replies and HTTP clients
    # to finish any request already in progress.
    for name in client_names:
        process = manager.processes[name]

        try:
            returncode = process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"Traffic client {name} did not finish within 40s "
                "after the scheduled flow stop"
            )

        if returncode != 0:
            raise RuntimeError(
                f"Traffic client {name} exited with code {returncode}"
            )

        print(f"[TRAFFIC] {name} finished successfully")

    return {
        "measurement_start_ns": measurement_start_ns,
        "measurement_stop_s": latest_stop_s,
        "clients": client_names,
    }



def load_topology_config() -> dict[str, str]:
    """Read simple KEY=value definitions from topology.env."""
    import shlex

    config = {}
    path = ROOT / "testbed" / "topology.env"

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        values = shlex.split(raw_value, comments=True)

        if values:
            config[key.strip()] = values[0]

    return config


def checked_command(
    command: list[str],
    timeout: float = 15,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def read_qdisc_json(namespace: str, interface: str) -> list[dict]:
    result = checked_command([
        "sudo", "-n", "ip", "netns", "exec", namespace,
        "tc", "-s", "-j", "qdisc", "show", "dev", interface,
    ])

    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot inspect qdisc on {namespace}:{interface}: "
            f"{result.stderr.strip()}"
        )

    return json.loads(result.stdout)


def numeric_value(value):
    """Extract a numeric value from tc JSON scalar or object fields."""
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, dict):
        for item in value.values():
            if isinstance(item, (int, float)):
                return float(item)

    return None


def check_modem_qdisc(
    namespace: str,
    interface: str,
    expected_mbps: float,
    expected_delay_ms: float,
    expected_jitter_ms: float,
    expected_loss_pct: float,
) -> dict:
    qdiscs = read_qdisc_json(namespace, interface)

    root = next(
        (item for item in qdiscs if item.get("root")),
        None,
    )
    child = next(
        (item for item in qdiscs if item.get("kind") == "netem"),
        None,
    )

    if root is None or root.get("kind") != "tbf":
        raise RuntimeError(
            f"{namespace}:{interface} does not have a root TBF qdisc"
        )

    if child is None:
        raise RuntimeError(
            f"{namespace}:{interface} is missing its netem child qdisc"
        )

    expected_bytes_per_second = expected_mbps * 1_000_000 / 8
    actual_rate = root.get("options", {}).get("rate")

    if actual_rate is None or abs(
        float(actual_rate) - expected_bytes_per_second
    ) > 1:
        raise RuntimeError(
            f"{namespace}:{interface} TBF rate mismatch: "
            f"expected {expected_bytes_per_second:.0f} B/s, "
            f"found {actual_rate}"
        )

    options = child.get("options", {})
    delay = options.get("delay", {})

    actual_delay = numeric_value(
        delay.get("delay") if isinstance(delay, dict) else delay
    )
    actual_jitter = numeric_value(
        delay.get("jitter") if isinstance(delay, dict) else None
    )
    actual_loss = numeric_value(options.get("loss"))

    # tc reports delay and jitter in seconds.
    expected_delay_s = expected_delay_ms / 1000
    expected_jitter_s = expected_jitter_ms / 1000

    if actual_delay is None or abs(actual_delay - expected_delay_s) > 0.00001:
        raise RuntimeError(
            f"{namespace}:{interface} delay mismatch: "
            f"expected {expected_delay_s}s, found {actual_delay}"
        )

    if actual_jitter is None or abs(
        actual_jitter - expected_jitter_s
    ) > 0.00001:
        raise RuntimeError(
            f"{namespace}:{interface} jitter mismatch: "
            f"expected {expected_jitter_s}s, found {actual_jitter}"
        )

    # tc may omit the loss field when configured loss is zero.
    actual_loss = actual_loss or 0.0
    expected_loss = expected_loss_pct / 100

    if abs(actual_loss - expected_loss) > 0.00001:
        raise RuntimeError(
            f"{namespace}:{interface} loss mismatch: "
            f"expected {expected_loss}, found {actual_loss}"
        )

    return {
        "namespace": namespace,
        "interface": interface,
        "rate_bytes_per_second": actual_rate,
        "delay_seconds": actual_delay,
        "jitter_seconds": actual_jitter,
        "loss_fraction": actual_loss,
        "result": "passed",
    }


def check_interface_offloads(namespace: str, interface: str) -> dict:
    result = checked_command([
        "sudo", "-n", "ip", "netns", "exec", namespace,
        "ethtool", "-k", interface,
    ])

    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot inspect offloads on {namespace}:{interface}: "
            f"{result.stderr.strip()}"
        )

    required = (
        "tcp-segmentation-offload",
        "generic-segmentation-offload",
        "generic-receive-offload",
    )

    enabled = []

    for feature in required:
        match = re.search(
            rf"^{re.escape(feature)}:\s+(on|off)",
            result.stdout,
            re.MULTILINE,
        )

        if match is None:
            raise RuntimeError(
                f"Could not find {feature} on {namespace}:{interface}"
            )

        if match.group(1) != "off":
            enabled.append(feature)

    if enabled:
        raise RuntimeError(
            f"Offloads enabled on {namespace}:{interface}: "
            + ", ".join(enabled)
        )

    return {
        "namespace": namespace,
        "interface": interface,
        "tso": "off",
        "gso": "off",
        "gro": "off",
        "result": "passed",
    }


def run_preflight(scenario: Scenario) -> dict:
    """Validate connectivity, modem impairment, and veth offloads."""
    config = load_topology_config()

    results = {
        "connectivity": "not_run",
        "modem_qdiscs": [],
        "offloads": [],
    }

    print("[PREFLIGHT] Running IPv4/IPv6 connectivity smoke test")

    smoke = checked_command(
        ["sudo", "-n", "./testbed/smoke.sh"],
        timeout=180,
    )

    if smoke.returncode != 0:
        raise RuntimeError(
            "Connectivity preflight failed:\n"
            + smoke.stdout[-3000:]
            + smoke.stderr[-1000:]
        )

    results["connectivity"] = "passed"
    print("[PREFLIGHT] Connectivity: passed")

    print("[PREFLIGHT] Checking modem qdisc rates and impairments")

    results["modem_qdiscs"].append(
        check_modem_qdisc(
            "qos-modem",
            config["MODEM_ROUTER_IF"],
            scenario.link.down_mbps,
            scenario.link.delay_ms,
            scenario.link.jitter_ms,
            scenario.link.loss_pct,
        )
    )

    results["modem_qdiscs"].append(
        check_modem_qdisc(
            "qos-modem",
            config["MODEM_ISP_IF"],
            scenario.link.up_mbps,
            scenario.link.delay_ms,
            scenario.link.jitter_ms,
            scenario.link.loss_pct,
        )
    )

    print("[PREFLIGHT] Modem qdiscs: passed")

    print("[PREFLIGHT] Checking virtual interface offloads")

    device_namespaces = config["DEVICE_NAMESPACES"].split()
    device_interface = config["DEVICE_INTERFACE"]

    interfaces = [
        (namespace, device_interface)
        for namespace in device_namespaces
    ]

    router_interfaces = [
        config["CALL_ROUTER_IF"],
        config["GAME_ROUTER_IF"],
        config["TV1_ROUTER_IF"],
        config["TV2_ROUTER_IF"],
        config["TV3_ROUTER_IF"],
        config["BULK_ROUTER_IF"],
        config["LAPTOP_ROUTER_IF"],
        config["ROUTER_MODEM_IF"],
    ]

    interfaces.extend(
        ("qos-router", interface)
        for interface in router_interfaces
    )

    interfaces.extend([
        ("qos-modem", config["MODEM_ROUTER_IF"]),
        ("qos-modem", config["MODEM_ISP_IF"]),
        ("qos-isp", config["ISP_INTERFACE"]),
    ])

    for namespace, interface in interfaces:
        results["offloads"].append(
            check_interface_offloads(namespace, interface)
        )

    print(
        f"[PREFLIGHT] Offloads: passed "
        f"({len(results['offloads'])} interfaces)"
    )

    results["result"] = "passed"
    print("[PREFLIGHT] All checks passed")

    return results


def reset_testbed():
    run_privileged(["./testbed/up.sh"])


def write_status(run_dir: Path, status: dict):
    temporary = run_dir / "status.json.tmp"
    destination = run_dir / "status.json"

    temporary.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n"
    )

    temporary.replace(destination)


def run_one(
    scenario: Scenario,
    scenario_path: Path,
    experiment_id: str,
    configuration: str,
    repetition: int,
    seed: int,
    run_id: str | None = None,
):
    if run_id is None:
        run_id = (
            f"{configuration}-"
            f"{repetition:02d}-"
            f"{uuid.uuid4().hex[:8]}"
        )
    # Refresh sudo authentication before each configuration-run.
    subprocess.run(["sudo", "-v"], cwd=ROOT, check=True)

    run_dir = RESULTS_ROOT / experiment_id / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    manager = RunManager(run_dir)
    monitor = None

    status = {
        "experiment_id": experiment_id,
        "run_id": run_id,
        "scenario": scenario.name,
        "configuration": configuration,
        "repetition": repetition,
        "seed": seed,
        "state": "starting",
        "valid": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "error": None,
        "processes": {},
        "monitor": None,
    }

    def handle_signal(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")

    previous_sigint = signal.signal(signal.SIGINT, handle_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)

    try:
        status["state"] = "resetting"
        write_status(run_dir, status)

        reset_testbed()

        status["state"] = "preflight"
        write_status(run_dir, status)

        preflight_result = run_preflight(scenario)
        status["preflight"] = preflight_result
        write_status(run_dir, status)

        status["state"] = "configuring"
        write_status(run_dir, status)

        if configuration == "static_qos":
            shaped_rate_mbps = (
                min(
                    scenario.link.up_mbps,
                    scenario.link.down_mbps,
                )
                * 0.90
            )

            run_privileged([
                "./qos/apply-static.sh",
                f"{shaped_rate_mbps:g}mbit",
            ])

        elif configuration != "no_qos":
            raise ValueError(
                f"Unsupported configuration: {configuration}"
            )

        # Collect metadata once, after configuration has been applied.
        metadata = collect_metadata(
            scenario_path=scenario_path,
            configuration=configuration,
            seed=seed,
        )

        metadata_path = write_metadata(run_dir, metadata)
        status["metadata"] = str(metadata_path)
        write_status(run_dir, status)

        print(f"[METADATA] Saved: {metadata_path}")

        status["state"] = "starting_servers"
        write_status(run_dir, status)

        start_servers(manager)

        # Start resource and qdisc sampling after server readiness,
        # before warm-up and traffic generation begin.
        monitor_path = run_dir / "raw" / "monitor.jsonl"
        monitor = RunMonitor(monitor_path, interval_s=1.0)
        monitor.start()

        status["monitor"] = str(monitor_path)
        status["state"] = "running"
        write_status(run_dir, status)

        traffic_result = run_traffic(scenario, manager)
        status["traffic"] = traffic_result

        check_servers_alive(manager)
        status["servers_alive_after_traffic"] = True

        # Stop sampling before process shutdown and testbed teardown.
        monitor.stop()
        monitor = None

        status["state"] = "analyzing"
        write_status(run_dir, status)

        metrics = analyze_run(
            scenario=scenario,
            run_dir=run_dir,
            traffic_result=traffic_result,
            configuration=configuration,
            metadata=metadata,
        )

        metrics_path = run_dir / "metrics.json"
        temporary_metrics_path = run_dir / "metrics.json.tmp"
        temporary_metrics_path.write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n"
        )
        temporary_metrics_path.replace(metrics_path)

        status["metrics"] = str(metrics_path)
        status["valid"] = metrics["valid"]
        status["validity_reasons"] = metrics["validity_reasons"]
        status["state"] = "completed"

        if not metrics["valid"]:
            status["error"] = "Run completed but failed validity checks"
        else:
            status["error"] = None

        write_status(run_dir, status)

        print(f"[METRICS] Saved: {metrics_path}")
        print(f"[VALIDITY] {'VALID' if metrics['valid'] else 'INVALID'}")

        for reason in metrics["validity_reasons"]:
            print(f"[VALIDITY] {reason}")

    except KeyboardInterrupt as exc:
        status["state"] = "interrupted"
        status["error"] = str(exc)

    except Exception as exc:
        status["state"] = "failed"
        status["error"] = f"{type(exc).__name__}: {exc}"

    finally:
        # Ignore further termination signals while cleanup is in progress.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        try:
            # Stop the monitor first so it cannot query namespaces while
            # the testbed is being deleted.
            if monitor is not None:
                try:
                    monitor.stop()
                    status["monitor_stopped_during_cleanup"] = True
                except Exception as exc:
                    status["monitor_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    status["state"] = "failed"
                    status["valid"] = False

            # Always attempt process cleanup, even if monitor shutdown failed.
            try:
                manager.stop_processes()
            except Exception as exc:
                status["process_cleanup_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                status["state"] = "failed"
                status["valid"] = False

            try:
                status["processes"] = manager.process_status()
            except Exception as exc:
                status["process_status_error"] = str(exc)

            # Always attempt testbed teardown, even if earlier cleanup failed.
            try:
                reset_testbed()
                status["teardown"] = "success"
            except Exception as exc:
                status["teardown"] = f"failed: {exc}"
                status["state"] = "failed"
                status["valid"] = False

            status["finished_at"] = datetime.now(
                timezone.utc
            ).isoformat()

            try:
                write_status(run_dir, status)
            except Exception as exc:
                print(f"[ERROR] Could not write final status: {exc}")

        finally:
            # Restore the caller's signal handlers regardless of cleanup errors.
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)

    print(f"[RUN] {run_id}: {status['state']}")
    print(f"[RUN] Status: {run_dir / 'status.json'}")

    if status["state"] != "completed":
        raise RuntimeError(
            f"Run {run_id} ended with state {status['state']}: "
            f"{status['error']}"
        )

    # Invalid measurements are still completed runs. The experiment
    # orchestrator decides whether to retry them; their evidence stays saved.
    return run_dir


def main():
    from experiments.orchestrator import execute_campaign

    parser = argparse.ArgumentParser(
        description="Adaptive QoS experiment runner"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--scenario", required=True)
    run_parser.add_argument(
        "--configuration",
        choices=["no_qos", "static_qos"],
        required=True,
    )
    run_parser.add_argument("--repetition", type=int, default=1)
    run_parser.add_argument("--seed", type=int)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the complete randomized experiment campaign",
    )
    demo_parser.add_argument("--scenario", required=True)
    demo_parser.add_argument(
        "--experiment-id",
        help="Reuse an existing experiment ID to resume a campaign",
    )

    args = parser.parse_args()

    scenario_path = Path(args.scenario).resolve()

    if not scenario_path.is_file():
        parser.error(f"Scenario not found: {scenario_path}")

    scenario_data = yaml.safe_load(scenario_path.read_text())
    scenario = Scenario.model_validate(scenario_data)

    subprocess.run(["sudo", "-v"], check=True)

    if args.command == "run":
        if not 1 <= args.repetition <= scenario.repetitions:
            parser.error("Repetition is outside the scenario range")

        experiment_id = (
            f"{scenario.name}-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )

        seed = (
            args.seed
            if args.seed is not None
            else scenario.seed + args.repetition - 1
        )

        run_one(
            scenario,
            scenario_path,
            experiment_id,
            args.configuration,
            args.repetition,
            seed,
        )
        return

    experiment_id = args.experiment_id or (
        f"{scenario.name}-"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )

    print(f"[CAMPAIGN] Experiment: {experiment_id}")
    print(f"[CAMPAIGN] Scenario: {scenario.name}")
    print(f"[CAMPAIGN] Repetitions: {scenario.repetitions}")

    result = execute_campaign(
        scenario=scenario,
        scenario_path=scenario_path,
        experiment_id=experiment_id,
        results_root=RESULTS_ROOT,
        executor=run_one,
    )

    print("\n===== CAMPAIGN SUMMARY =====")
    print(f"Experiment: {result['experiment_id']}")
    print(f"Planned configuration-runs: {result['planned_runs']}")
    print(f"Successful configuration-runs: {result['successful_runs']}")
    print(f"Unsuccessful configuration-runs: {result['unsuccessful_runs']}")

if __name__ == "__main__":
    main()