
#!/usr/bin/env python3

import csv
import statistics
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "experiments" / "results" / "baseline"
PLOTS = ROOT / "experiments" / "results" / "plots"

EXPERIMENTS = [
    {
        "file": "call-idle.csv",
        "title": "Call Latency — Idle Network",
        "output": "call-idle.png",
    },
    {
        "file": "game-idle.csv",
        "title": "Game Latency — Idle Network",
        "output": "game-idle.png",
    },
    {
        "file": "call-plus-bulk.csv",
        "title": "Call Latency — Call + Bulk at 100 Mbps",
        "output": "call-plus-bulk.png",
    },
    {
        "file": "call-rate-drop-synced.csv",
        "title": "Call Latency — 100 to 20 Mbps Rate Drop",
        "output": "call-rate-drop.png",
        "event_file": Path("/tmp/rate-drop-event-synced.txt"),
    },
    {
        "file": "call-20mbit-udp-overload-fixed.csv",
        "title": "Call Latency — Saturated 20 Mbps Upload Queue",
        "output": "call-20mbit-udp-overload.png",
    },
    {
        "file": "call-100mbit-udp-overload.csv",
        "title": "Call Latency — Saturated 100 Mbps Upload Queue",
        "output": "call-100mbit-udp-overload.png",
    },
    {
        "file": "laptop-call-plus-download.csv",
        "title": "Laptop Call Latency — Concurrent Bulk Download",
        "output": "laptop-call-plus-download.png",
    },

]


def load_samples(csv_path):
    samples = []

    with csv_path.open(newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            if not row["client_send_ns"] or not row["forward_delay_ms"]:
                continue

            samples.append({
                "timestamp": int(row["client_send_ns"]),
                "delay": float(row["forward_delay_ms"]),
            })

    if not samples:
        raise ValueError(f"No valid latency samples in {csv_path}")

    samples.sort(key=lambda sample: sample["timestamp"])

    start_ns = samples[0]["timestamp"]

    for sample in samples:
        sample["elapsed"] = (sample["timestamp"] - start_ns) / 1e9

    return samples, start_ns


def rolling_median(samples, window_seconds=1):
    buckets = {}

    for sample in samples:
        bucket = int(sample["elapsed"] // window_seconds)
        buckets.setdefault(bucket, []).append(sample["delay"])

    times = []
    medians = []

    for bucket, values in sorted(buckets.items()):
        times.append((bucket + 0.5) * window_seconds)
        medians.append(statistics.median(values))

    return times, medians



def plot_experiment(experiment):
    csv_path = RESULTS / experiment["file"]
    output_path = PLOTS / experiment["output"]

    samples, start_ns = load_samples(csv_path)

    fig, ax = plt.subplots(figsize=(12, 5))

    for column, label in [
        ("forward_delay_ms", "Forward delay"),
        ("reverse_delay_ms", "Reverse delay"),
    ]:
        values = []

        with csv_path.open(newline="") as file:
            for row in csv.DictReader(file):
                if row.get(column) and row.get("client_send_ns"):
                    values.append({
                        "timestamp": int(row["client_send_ns"]),
                        "delay": float(row[column]),
                    })

        values.sort(key=lambda item: item["timestamp"])

        if not values:
            continue

        elapsed = [
            (item["timestamp"] - start_ns) / 1e9
            for item in values
        ]

        delays = [item["delay"] for item in values]

        ax.scatter(elapsed, delays, s=7, alpha=0.15)

        series = [
            {"elapsed": t, "delay": d}
            for t, d in zip(elapsed, delays)
        ]

        median_times, median_delays = rolling_median(series)

        ax.plot(
            median_times,
            median_delays,
            linewidth=2,
            label=f"{label} (1-second median)",
        )

    if "event_file" in experiment:
        event_path = experiment["event_file"]

        if event_path.exists():
            event_ns = int(event_path.read_text().strip())
            event_seconds = (event_ns - start_ns) / 1e9

            ax.axvline(
                event_seconds,
                linestyle="--",
                linewidth=2,
                label="Rate changed to 20 Mbps",
            )

    ax.set_title(experiment["title"])
    ax.set_xlabel("Elapsed time (seconds)")
    ax.set_ylabel("One-way application-observed delay (ms)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=160)
    plt.close(fig)

    print(f"Saved: {output_path}")
    print()


def main():
    for experiment in EXPERIMENTS:
        plot_experiment(experiment)


if __name__ == "__main__":
    main()
