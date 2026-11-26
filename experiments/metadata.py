
#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

QDISC_TARGETS = {
    "modem_m_lan": ("qos-modem", "m-lan"),
    "modem_m_wan": ("qos-modem", "m-wan"),
    "router_r_wan": ("qos-router", "r-wan"),
    "router_ifb0": ("qos-router", "ifb0"),
}


def command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode != 0:
            return None

        return result.stdout.strip()

    except (OSError, subprocess.TimeoutExpired):
        return None


def git_metadata():
    commit = command_output(["git", "rev-parse", "HEAD"])
    dirty_output = command_output(
        ["git", "status", "--porcelain"]
    )

    return {
        "commit": commit,
        "dirty": bool(dirty_output) if dirty_output is not None else None,
        "status_porcelain": dirty_output,
    }


def cpu_metadata():
    lscpu_output = command_output(["lscpu"]) or ""

    cpuinfo = Path("/proc/cpuinfo").read_text(
        errors="replace"
    )

    implementer = None
    for line in cpuinfo.splitlines():
        if line.startswith("CPU implementer"):
            implementer = line.split(":", 1)[1].strip()
            break


    def parse_lscpu(label: str):
        for line in lscpu_output.splitlines():
            if ":" not in line:
                continue

            key, value = line.split(":", 1)
            if key.strip() == label:
                value = value.strip()

                if value in {"", "-", "0", "unknown", "Unknown"}:
                    return None

                return value

        return None

    return {
        "architecture": platform.machine(),
        "vendor": parse_lscpu("Vendor ID"),
        "model_name": parse_lscpu("Model name"),
        "model": parse_lscpu("Model"),
        "cpu_count": os_cpu_count(),
        "cpu_implementer": implementer,
        "lscpu": lscpu_output,
    }


def os_cpu_count():
    import os
    return os.cpu_count()


def qdisc_snapshot(namespace: str, interface: str):
    result = subprocess.run(
        [
            "sudo", "-n", "ip", "netns", "exec",
            namespace, "tc", "-s", "-j", "qdisc", "show",
            "dev", interface,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    if result.returncode != 0:
        return {
            "error": result.stderr.strip(),
            "returncode": result.returncode,
        }

    try:
        return {
            "returncode": 0,
            "data": json.loads(result.stdout or "[]"),
        }
    except json.JSONDecodeError as exc:
        return {"error": str(exc)}



def netem_seed_capability():
    """
    Probe netem seed support in an isolated temporary network namespace.
    This does not modify the experiment testbed.
    """
    namespace = f"qos-netem-probe-{os.getpid()}"

    def run(command):
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    created = False

    try:
        result = run(["sudo", "-n", "ip", "netns", "add", namespace])

        if result.returncode != 0:
            return {
                "supported": None,
                "error": result.stderr.strip() or "Could not create probe namespace",
            }

        created = True

        result = run([
            "sudo", "-n", "ip", "-n", namespace,
            "link", "add", "seed0", "type", "dummy",
        ])

        if result.returncode != 0:
            return {
                "supported": None,
                "error": result.stderr.strip() or "Could not create probe interface",
            }

        result = run([
            "sudo", "-n", "ip", "netns", "exec", namespace,
            "tc", "qdisc", "add", "dev", "seed0",
            "root", "netem", "seed", "12345",
        ])

        return {
            "supported": result.returncode == 0,
            "probe_output": (
                result.stderr.strip() or result.stdout.strip() or None
            ),
        }

    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "supported": None,
            "error": str(exc),
        }

    finally:
        if created:
            run(["sudo", "-n", "ip", "netns", "delete", namespace])

def collect_metadata(
    scenario_path: Path,
    configuration: str,
    seed: int,
):
    scenario_bytes = scenario_path.read_bytes()

    qdiscs = {
        name: qdisc_snapshot(namespace, interface)
        for name, (namespace, interface) in QDISC_TARGETS.items()
    }

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "configuration": configuration,
        "seed": seed,
        "scenario": {
            "path": str(scenario_path),
            "sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        },
        "git": git_metadata(),
        "system": {
            "kernel": platform.release(),
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "cpu": cpu_metadata(),
        },
        "capabilities": {
            "netem_seed": netem_seed_capability(),
        },
        "tools": {
            "iproute2": command_output(["ip", "-Version"]),
            "iperf3": command_output(["iperf3", "--version"]),
        },
        "qdiscs": qdiscs,
    }


def write_metadata(run_dir: Path, metadata: dict):
    destination = run_dir / "metadata.json"
    temporary = run_dir / "metadata.json.tmp"

    temporary.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )

    temporary.replace(destination)

    return destination
