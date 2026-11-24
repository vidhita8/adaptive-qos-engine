#!/usr/bin/env python3

import csv
import json
import math
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results" / "static-qos-sweep"

BASE_RATES = (100, 20)
SHAPING_PERCENTAGES = range(70, 101, 5)
DIRECTIONS = ("upload", "download")

CALL_DURATION = 25
CALL_RATE = 50
BULK_STREAMS = 4
BULK_PORT = 5201
SERVER = "10.30.0.2"

FIELDS = [
    "modem_rate_mbps",
    "shaping_percent",
    "shaper_rate_mbps",
    "direction",
    "call_latency_column",
    "call_p95_ms",
    "call_p99_ms",
    "bulk_receiver_mbps",
    "call_samples",
    "call_lost",
    "call_loss_percent",
    "call_reordered",
]


def run_logged(command, log_path):
    with log_path.open("w") as log:
        subprocess.run(
            command,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )


def percentile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    low = math.floor(index)
    high = math.ceil(index)

    if low == high:
        return values[low]

    return values[low] + (values[high] - values[low]) * (index - low)


def check_servers():
    udp = subprocess.run(
        ["sudo", "ip", "netns", "exec", "qos-isp", "ss", "-lunp"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout

    tcp = subprocess.run(
        ["sudo", "ip", "netns", "exec", "qos-isp", "ss", "-lntp"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout

    for port in (6001, 6002):
        if not re.search(rf":{port}\b", udp):
            raise RuntimeError(f"UDP echo server is not listening on {port}")

    if not re.search(r":5201\b", tcp):
        raise RuntimeError("iperf3 server is not listening on port 5201")


def run_measurement(output_dir, base_rate, percentage, shaper_rate, direction):
    label = f"{base_rate}mbit-{percentage}pct-{direction}"

    call_csv = output_dir / f"{label}-call.csv"
    call_log = output_dir / f"{label}-call.log"
    iperf_json = output_dir / f"{label}-iperf.json"

    latency_column = (
        "forward_delay_ms" if direction == "upload"
        else "reverse_delay_ms"
    )

    call_command = [
        "sudo", "ip", "netns", "exec", "qos-call",
        "python3", str(ROOT / "traffic" / "udp_echo_client.py"),
        "--server", SERVER,
        "--port", "6001",
        "--rate", str(CALL_RATE),
        "--duration", str(CALL_DURATION),
        "--output", str(call_csv),
    ]

    iperf_command = [
        "sudo", "ip", "netns", "exec", "qos-bulk",
        "iperf3", "-c", SERVER,
        "-p", str(BULK_PORT),
        "-P", str(BULK_STREAMS),
        "-t", str(CALL_DURATION),
    ]

    if direction == "download":
        iperf_command.append("-R")

    iperf_command.append("-J")

    print(f"\n[TEST] {label} (shaper {shaper_rate} Mbps)")

    with call_log.open("w") as call_output, iperf_json.open("w") as bulk_output:
        call_process = subprocess.Popen(
            call_command,
            cwd=ROOT,
            stdout=call_output,
            stderr=subprocess.STDOUT,
        )

        time.sleep(0.25)

        bulk_process = subprocess.Popen(
            iperf_command,
            cwd=ROOT,
            stdout=bulk_output,
            stderr=subprocess.STDOUT,
        )

        try:
            bulk_status = bulk_process.wait(timeout=40)
            call_status = call_process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            bulk_process.kill()
            call_process.kill()
            raise RuntimeError(f"Timed out during {label}")

    if call_status != 0:
        raise RuntimeError(f"Call client failed. Inspect {call_log}")

    if bulk_status != 0:
        raise RuntimeError(f"iperf3 failed. Inspect {iperf_json}")

    with call_csv.open(newline="") as file:
        rows = list(csv.DictReader(file))

    latency_values = [
        float(row[latency_column])
        for row in rows
        if row[latency_column]
    ]

    if not latency_values:
        raise RuntimeError(f"No valid call latency samples in {call_csv}")

    lost = sum(int(row["lost"] or 0) for row in rows)
    reordered = sum(int(row["reordered"] or 0) for row in rows)

    with iperf_json.open() as file:
        iperf_data = json.load(file)

    end = iperf_data["end"]
    receiver = end.get("sum_received") or end.get("sum")

    if not receiver or "bits_per_second" not in receiver:
        raise RuntimeError(f"No receiver throughput found in {iperf_json}")

    bulk_mbps = receiver["bits_per_second"] / 1_000_000

    result = {
        "modem_rate_mbps": base_rate,
        "shaping_percent": percentage,
        "shaper_rate_mbps": shaper_rate,
        "direction": direction,
        "call_latency_column": latency_column,
        "call_p95_ms": round(percentile(latency_values, 0.95), 3),
        "call_p99_ms": round(percentile(latency_values, 0.99), 3),
        "bulk_receiver_mbps": round(bulk_mbps, 3),
        "call_samples": len(rows),
        "call_lost": lost,
        "call_loss_percent": round(100 * lost / max(len(rows), 1), 3),
        "call_reordered": reordered,
    }

    print(
        f"[RESULT] Call p95={result['call_p95_ms']} ms | "
        f"p99={result['call_p99_ms']} ms | "
        f"Bulk receiver={bulk_mbps:.2f} Mbps | "
        f"Loss={result['call_loss_percent']}%"
    )

    return result


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = RESULTS / run_id
    output_dir.mkdir(parents=True)

    summary_path = output_dir / "summary.csv"

    print("[SETUP] Checking sudo permissions...")
    subprocess.run(["sudo", "-v"], cwd=ROOT, check=True)

    print("[SETUP] Checking traffic servers...")
    check_servers()

    print(f"[SETUP] Results will be saved in: {output_dir}")

    try:
        with summary_path.open("w", newline="") as summary_file:
            writer = csv.DictWriter(summary_file, fieldnames=FIELDS)
            writer.writeheader()
            summary_file.flush()

            for base_rate in BASE_RATES:
                print(f"\n========== MODEM RATE: {base_rate} Mbps ==========")

                run_logged(
                    [
                        "sudo", "/usr/bin/bash",
                        "testbed/change-rate.sh",
                        f"{base_rate}mbit",
                    ],
                    output_dir / f"modem-{base_rate}mbit.log",
                )

                for percentage in SHAPING_PERCENTAGES:
                    shaper_rate = base_rate * percentage // 100

                    run_logged(
                        [
                            "sudo", "/usr/bin/bash",
                            "qos/apply-static.sh",
                            f"{shaper_rate}mbit",
                        ],
                        output_dir / (
                            f"apply-{base_rate}mbit-{percentage}pct.log"
                        ),
                    )

                    for direction in DIRECTIONS:
                        result = run_measurement(
                            output_dir,
                            base_rate,
                            percentage,
                            shaper_rate,
                            direction,
                        )

                        writer.writerow(result)
                        summary_file.flush()

                        # Allow modem queues and TCP state to settle.
                        time.sleep(1)

    finally:
        print("\n[RESTORE] Returning testbed to 100 Mbps modem / 95 Mbps QoS.")
        try:
            run_logged(
                [
                    "sudo", "/usr/bin/bash",
                    "testbed/change-rate.sh", "100mbit",
                ],
                output_dir / "restore-modem.log",
            )
            run_logged(
                [
                    "sudo", "/usr/bin/bash",
                    "qos/apply-static.sh", "95mbit",
                ],
                output_dir / "restore-qos.log",
            )
            print("[RESTORE] Testbed restored.")
        except subprocess.CalledProcessError:
            print("[WARNING] Automatic restoration failed. Check the restore logs.")

    print(f"\n[DONE] Summary: {summary_path}")


if __name__ == "__main__":
    main()
