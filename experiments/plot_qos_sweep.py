#!/usr/bin/env python3

import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results" / "static-qos-sweep"


def find_summary():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path

    summaries = sorted(
        RESULTS.glob("*/summary.csv"),
        key=lambda path: path.stat().st_mtime,
    )

    if not summaries:
        raise FileNotFoundError("No sweep summary.csv found.")

    return summaries[-1]


def main():
    summary_path = find_summary()

    with summary_path.open(newline="") as file:
        rows = list(csv.DictReader(file))

    if len(rows) != 28:
        raise ValueError(
            f"Expected 28 measurements, found {len(rows)}. "
            "Check the sweep summary before plotting."
        )

    output_dir = summary_path.parent

    for modem_rate in (100, 20):
        selected = [
            row for row in rows
            if int(row["modem_rate_mbps"]) == modem_rate
        ]

        fig, (latency_ax, throughput_ax) = plt.subplots(
            2, 1, figsize=(11, 9), sharex=True
        )

        for direction in ("upload", "download"):
            direction_rows = sorted(
                [
                    row for row in selected
                    if row["direction"] == direction
                ],
                key=lambda row: int(row["shaping_percent"]),
            )

            ratios = [
                int(row["shaping_percent"])
                for row in direction_rows
            ]

            p95 = [
                float(row["call_p95_ms"])
                for row in direction_rows
            ]

            p99 = [
                float(row["call_p99_ms"])
                for row in direction_rows
            ]

            throughput = [
                float(row["bulk_receiver_mbps"])
                for row in direction_rows
            ]

            latency_ax.plot(
                ratios, p95, marker="o",
                label=f"{direction.capitalize()} p95",
            )
            latency_ax.plot(
                ratios, p99, marker="s", linestyle="--",
                label=f"{direction.capitalize()} p99",
            )

            throughput_ax.plot(
                ratios, throughput, marker="o",
                label=direction.capitalize(),
            )

        latency_ax.set_title(
            f"Call Latency Under Bulk Load — {modem_rate} Mbps Modem"
        )
        latency_ax.set_ylabel("One-way call latency (ms)")
        latency_ax.grid(True, alpha=0.3)
        latency_ax.legend()

        throughput_ax.set_title("Bulk TCP Throughput")
        throughput_ax.set_xlabel("Router shaping rate (% of modem capacity)")
        throughput_ax.set_ylabel("Receiver throughput (Mbps)")
        throughput_ax.set_xticks(range(70, 101, 5))
        throughput_ax.grid(True, alpha=0.3)
        throughput_ax.legend()

        fig.tight_layout()

        output = output_dir / f"qos-sweep-{modem_rate}mbit.png"
        fig.savefig(output, dpi=160)
        plt.close(fig)

        print(f"Saved: {output}")


if __name__ == "__main__":
    main()
