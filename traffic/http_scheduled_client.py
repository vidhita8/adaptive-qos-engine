
#!/usr/bin/env python3

import argparse
import csv
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Scheduled HTTP chunk download client"
    )
    parser.add_argument("--server", default="10.30.0.2")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--chunk-bytes", type=int, default=1_000_000)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument(
        "--measurement-start-ns",
        type=int,
        default=None,
        help="Shared monotonic timestamp marking experiment measurement time zero",
    )
    parser.add_argument(
        "--start-s",
        type=float,
        default=0.0,
        help="Flow start time relative to experiment measurement time zero",
    )
    parser.add_argument(
        "--stop-s",
        type=float,
        default=None,
        help="Flow stop time relative to experiment measurement time zero",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", required=True)

    args = parser.parse_args()

    stop_s = (
        args.stop_s
        if args.stop_s is not None
        else args.start_s + args.duration
    )

    if (
        args.port < 1
        or args.port > 65535
        or args.chunk_bytes < 1
        or args.interval <= 0
        or stop_s <= args.start_s
        or args.timeout <= 0
    ):
        parser.error(
            "port and chunk size must be valid; interval and timeout must "
            "be positive; stop-s must exceed start-s"
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    measurement_start_ns = (
        args.measurement_start_ns
        if args.measurement_start_ns is not None
        else time.monotonic_ns()
    )

    flow_start_ns = (
        measurement_start_ns + int(args.start_s * 1e9)
    )
    flow_stop_ns = (
        measurement_start_ns + int(stop_s * 1e9)
    )
    interval_ns = int(args.interval * 1e9)

    rows = []
    chunk_number = 0

    while True:
        scheduled_ns = flow_start_ns + chunk_number * interval_ns

        if scheduled_ns >= flow_stop_ns:
            break

        while True:
            now_ns = time.monotonic_ns()
            remaining_ns = scheduled_ns - now_ns

            if remaining_ns <= 0:
                break

            time.sleep(min(remaining_ns / 1e9, 0.05))

        request_start_ns = time.monotonic_ns()

        if request_start_ns >= flow_stop_ns:
            break

        scheduled_seconds = (
            scheduled_ns - measurement_start_ns
        ) / 1e9
        started_seconds = (
            request_start_ns - measurement_start_ns
        ) / 1e9

        url = (
            f"http://{args.server}:{args.port}/chunk"
            f"?size_bytes={args.chunk_bytes}"
        )

        download_ms = ""
        bytes_received = 0
        stall_seconds = ""
        stalled = 1
        error = ""

        try:
            request = urllib.request.Request(url)

            with urllib.request.urlopen(
                request, timeout=args.timeout
            ) as response:
                body = response.read()

            completed_ns = time.monotonic_ns()
            download_seconds = (
                completed_ns - request_start_ns
            ) / 1e9

            bytes_received = len(body)
            download_ms = round(download_seconds * 1000, 3)

            # Delivery-pressure indicator, not a full playback-buffer model.
            stall_value = max(0.0, download_seconds - args.interval)
            stall_seconds = round(stall_value, 3)
            stalled = int(stall_value > 0)

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            error = str(exc)

        rows.append({
            "chunk": chunk_number,
            "scheduled_seconds": round(scheduled_seconds, 6),
            "started_seconds": round(started_seconds, 6),
            "schedule_lag_ms": round(
                max(0, request_start_ns - scheduled_ns) / 1e6,
                3,
            ),
            "download_ms": download_ms,
            "bytes_received": bytes_received,
            "stall_seconds": stall_seconds,
            "stalled": stalled,
            "error": error,
        })

        chunk_number += 1

    with output.open("w", newline="") as file:
        fieldnames = [
            "chunk",
            "scheduled_seconds",
            "started_seconds",
            "schedule_lag_ms",
            "download_ms",
            "bytes_received",
            "stall_seconds",
            "stalled",
            "error",
        ]

        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    successful = [
        row for row in rows
        if row["bytes_received"] > 0
    ]
    stalled_count = sum(row["stalled"] for row in rows)

    print(f"Scheduled chunks: {len(rows)}")
    print(f"Successful:       {len(successful)}")
    print(f"Stalled:          {stalled_count}")
    print(f"Failed:           {len(rows) - len(successful)}")
    print(f"CSV:              {output}")


if __name__ == "__main__":
    main()
