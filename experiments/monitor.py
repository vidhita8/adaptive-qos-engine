
#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


QDISC_TARGETS = {
    "modem_m_lan": ("qos-modem", "m-lan"),
    "modem_m_wan": ("qos-modem", "m-wan"),
    "router_r_wan": ("qos-router", "r-wan"),
    "router_ifb0": ("qos-router", "ifb0"),
}


def read_cpu_times():
    with open("/proc/stat") as file:
        fields = file.readline().split()

    values = [int(value) for value in fields[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)

    return total, idle


def read_memory():
    values = {}

    with open("/proc/meminfo") as file:
        for line in file:
            key, value = line.split(":", 1)

            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value.strip().split()[0])

    total = values.get("MemTotal")
    available = values.get("MemAvailable")

    return {
        "total_kb": total,
        "available_kb": available,
        "used_kb": (
            total - available
            if total is not None and available is not None
            else None
        ),
    }


def read_qdisc(namespace: str, interface: str):
    command = [
        "sudo",
        "-n",
        "ip",
        "netns",
        "exec",
        namespace,
        "tc",
        "-s",
        "-j",
        "qdisc",
        "show",
        "dev",
        interface,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

        if result.returncode != 0:
            return {
                "returncode": result.returncode,
                "error": result.stderr.strip(),
            }

        return {
            "returncode": 0,
            "data": json.loads(result.stdout or "[]"),
        }

    except subprocess.TimeoutExpired:
        return {
            "returncode": None,
            "error": "qdisc query timed out",
        }

    except OSError as exc:
        return {
            "returncode": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    except json.JSONDecodeError as exc:
        return {
            "returncode": 0,
            "error": f"invalid qdisc JSON: {exc}",
        }

class RunMonitor:
    def __init__(
        self,
        output_file: Path,
        interval_s: float = 1.0,
    ):
        if interval_s <= 0:
            raise ValueError("Monitoring interval must be positive")

        self.output_file = output_file
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Monitor has already been started")

        self.output_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.thread = threading.Thread(
            target=self._sample_loop,
            name="qos-run-monitor",
            daemon=True,
        )

        self.thread.start()

    def stop(self):
        if self.thread is None:
            return

        self.stop_event.set()
        self.thread.join(timeout=15)

        if self.thread.is_alive():
            raise TimeoutError(
                "Monitoring thread did not stop within 15 seconds"
            )

        if self.error:
            raise RuntimeError(
                f"Monitoring failed: {self.error}"
            )

    def _sample_loop(self):
        try:
            previous_cpu = read_cpu_times()
            previous_sample_ns = None
            interval_ns = int(self.interval_s * 1e9)

            next_sample_ns = time.monotonic_ns()

            with self.output_file.open("w") as output:
                while not self.stop_event.is_set():
                    sample_started_ns = time.monotonic_ns()

                    current_cpu = read_cpu_times()

                    total_delta = (
                        current_cpu[0] - previous_cpu[0]
                    )
                    idle_delta = (
                        current_cpu[1] - previous_cpu[1]
                    )

                    cpu_pct = None

                    if total_delta > 0:
                        cpu_pct = round(
                            100 * (1 - idle_delta / total_delta),
                            2,
                        )

                    previous_cpu = current_cpu

                    qdiscs = {
                        name: read_qdisc(namespace, interface)
                        for name, (namespace, interface)
                        in QDISC_TARGETS.items()
                    }

                    sample_finished_ns = time.monotonic_ns()

                    record = {
                        "timestamp_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "monotonic_ns": sample_started_ns,
                        "sample_duration_ms": round(
                            (
                                sample_finished_ns
                                - sample_started_ns
                            ) / 1e6,
                            2,
                        ),
                        "interval_ms": (
                            round(
                                (
                                    sample_started_ns
                                    - previous_sample_ns
                                ) / 1e6,
                                2,
                            )
                            if previous_sample_ns is not None
                            else None
                        ),
                        "host": {
                            "cpu_pct": cpu_pct,
                            "memory": read_memory(),
                        },
                        "qdiscs": qdiscs,
                    }

                    output.write(
                        json.dumps(
                            record,
                            separators=(",", ":"),
                        ) + "\n"
                    )
                    output.flush()

                    previous_sample_ns = sample_started_ns

                    # Advance to the next scheduled sample.
                    next_sample_ns += interval_ns

                    # If collection took longer than the interval,
                    # skip missed deadlines instead of sampling repeatedly.
                    now_ns = time.monotonic_ns()

                    if next_sample_ns <= now_ns:
                        missed = (
                            (now_ns - next_sample_ns)
                            // interval_ns
                        ) + 1

                        next_sample_ns += missed * interval_ns

                    wait_s = max(
                        0,
                        (next_sample_ns - time.monotonic_ns()) / 1e9,
                    )

                    self.stop_event.wait(wait_s)

        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.stop_event.set()
