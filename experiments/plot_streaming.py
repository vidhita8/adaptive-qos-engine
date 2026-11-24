
import csv
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results" / "baseline"
PLOTS = ROOT / "results" / "plots"

EXPERIMENTS = [
    {
        "file": "stream-idle.csv",
        "label": "Streaming idle",
        "output": "stream-idle.png",
    },
    {
        "file": "stream-plus-bulk-download.csv",
        "label": "Streaming + bulk download",
        "output": "stream-plus-bulk-download.png",
    },
]


def plot_streaming(experiment):
    csv_path = RESULTS / experiment["file"]
    output_path = PLOTS / experiment["output"]

    with csv_path.open(newline="") as file:
        rows = list(csv.DictReader(file))

    chunks = [int(row["chunk"]) for row in rows]
    download_ms = [float(row["download_ms"]) for row in rows]
    buffer_seconds = [
        float(row["buffer_after_seconds"]) for row in rows
    ]
    stalled = [
        int(row["stalled"]) for row in rows
    ]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True
    )

    ax1.plot(chunks, download_ms, marker="o", markersize=4)
    ax1.set_ylabel("Chunk download time (ms)")
    ax1.set_title(experiment["label"])
    ax1.grid(True, alpha=0.3)

    ax2.plot(
        chunks,
        buffer_seconds,
        marker="o",
        markersize=4,
        label="Playback buffer",
    )
    ax2.set_ylabel("Buffer (seconds)")
    ax2.set_xlabel("Chunk number")
    ax2.grid(True, alpha=0.3)

    for chunk, is_stalled in zip(chunks, stalled):
        if is_stalled:
            ax1.axvline(chunk, color="red", linestyle="--", alpha=0.5)
            ax2.axvline(chunk, color="red", linestyle="--", alpha=0.5)

    fig.tight_layout()
    PLOTS.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=160)
    plt.close(fig)

    print(f"Saved: {output_path}")
    print(f"Chunks: {len(rows)}")
    print(f"Stalls: {sum(stalled)}")


for experiment in EXPERIMENTS:
    plot_streaming(experiment)
