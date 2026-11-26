
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt


CONFIGURATIONS = ("no_qos", "static_qos")
PHASES = ("baseline", "bulk_ramp", "bulk_steady")


def load_aggregate(path: Path) -> dict:
    data = json.loads(path.read_text())

    if not isinstance(data.get("configurations"), dict):
        raise ValueError("Aggregate is missing configurations")

    return data


def statistic(summary, field="median"):
    if not isinstance(summary, dict):
        return None
    value = summary.get(field)
    return float(value) if isinstance(value, (int, float)) else None


def add_ci(ax, x, summary, color=None, label=None):
    median = statistic(summary)
    low = statistic(summary, "ci_low")
    high = statistic(summary, "ci_high")

    if median is None:
        return

    lower = median - low if low is not None else 0
    upper = high - median if high is not None else 0

    ax.errorbar(
        x,
        median,
        yerr=[[lower], [upper]],
        marker="o",
        capsize=4,
        color=color,
        label=label,
    )


def plot_latency(data: dict, output: Path):
    fig, axes = plt.subplots(
        2, 2, figsize=(12, 8), sharex=True
    )

    metrics = (
        ("call", "forward_delay_ms_p95", "Call p95"),
        ("call", "forward_delay_ms_p99", "Call p99"),
        ("game", "forward_delay_ms_p95", "Game p95"),
        ("game", "forward_delay_ms_p99", "Game p99"),
    )

    colors = {"no_qos": "tab:blue", "static_qos": "tab:orange"}

    for ax, (device, field, title) in zip(axes.flat, metrics):
        for configuration in CONFIGURATIONS:
            summary = data["configurations"].get(configuration, {})
            if summary.get("status") == "n/a":
                continue

            x_values = []
            y_values = []
            lower_errors = []
            upper_errors = []

            for index, phase in enumerate(PHASES):
                metric = (
                    summary.get("phases", {})
                    .get(phase, {})
                    .get(device, {})
                    .get(field)
                )
                median = statistic(metric)

                if median is None:
                    continue

                low = statistic(metric, "ci_low")
                high = statistic(metric, "ci_high")

                x_values.append(index)
                y_values.append(median)
                lower_errors.append(
                    median - low if low is not None else 0
                )
                upper_errors.append(
                    high - median if high is not None else 0
                )

            if x_values:
                ax.errorbar(
                    x_values,
                    y_values,
                    yerr=[lower_errors, upper_errors],
                    marker="o",
                    capsize=4,
                    label=configuration,
                    color=colors[configuration],
                )

        ax.set_title(title)
        ax.set_ylabel("One-way delay (ms)")
        ax.set_xticks(range(len(PHASES)), PHASES)
        ax.grid(True, alpha=0.3)
        ax.legend()

    fig.suptitle("Interactive Traffic Latency by Phase")
    fig.tight_layout()
    fig.savefig(output, dpi=160)
    plt.close(fig)


def plot_throughput(data: dict, output: Path):
    fig, ax = plt.subplots(figsize=(10, 5))
    colors = {"no_qos": "tab:blue", "static_qos": "tab:orange"}

    for configuration in CONFIGURATIONS:
        summary = data["configurations"].get(configuration, {})
        if summary.get("status") == "n/a":
            continue

        x_values = []
        y_values = []
        lower_errors = []
        upper_errors = []

        for index, phase in enumerate(PHASES):
            metric = (
                summary.get("phases", {})
                .get(phase, {})
                .get("iso_download_mbps")
            )
            median = statistic(metric)

            if median is None:
                continue

            low = statistic(metric, "ci_low")
            high = statistic(metric, "ci_high")

            x_values.append(index)
            y_values.append(median)
            lower_errors.append(median - low if low is not None else 0)
            upper_errors.append(high - median if high is not None else 0)

        if x_values:
            ax.errorbar(
                x_values,
                y_values,
                yerr=[lower_errors, upper_errors],
                marker="o",
                capsize=4,
                label=configuration,
                color=colors[configuration],
            )

    ax.set_title("ISO Download Throughput")
    ax.set_ylabel("Receiver throughput (Mbps)")
    ax.set_xticks(range(len(PHASES)), PHASES)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output, dpi=160)
    plt.close(fig)


def plot_system_health(data: dict, output: Path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    configurations = [
        name for name in CONFIGURATIONS
        if data["configurations"].get(name, {}).get("status") != "n/a"
    ]

    targets = ("modem_m_lan", "modem_m_wan", "router_r_wan", "router_ifb0")

    for configuration in configurations:
        summary = data["configurations"][configuration]
        cpu = statistic(summary.get("host_cpu_max_pct"))
        if cpu is not None:
            axes[0].scatter(configuration, cpu, label=configuration)

    axes[0].set_title("Maximum Host CPU Across Runs")
    axes[0].set_ylabel("CPU utilization (%)")
    axes[0].grid(True, alpha=0.3)

    for configuration in configurations:
        summary = data["configurations"][configuration]
        for target in targets:
            backlog = statistic(
                summary.get("qdiscs", {}).get(target, {}).get(
                    "max_backlog_bytes"
                )
            )
            if backlog is not None:
                axes[1].scatter(
                    f"{configuration}\n{target}",
                    backlog / 1024,
                )

    axes[1].set_title("Maximum Qdisc Backlog")
    axes[1].set_ylabel("Backlog (KiB)")
    axes[1].tick_params(axis="x", labelrotation=35)
    axes[1].grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(output, dpi=160)
    plt.close(fig)


def format_ci(summary, unit=""):
    if not isinstance(summary, dict) or summary.get("median") is None:
        return "n/a"

    median = summary["median"]
    low = summary.get("ci_low")
    high = summary.get("ci_high")
    count = summary.get("n", 0)

    if low is None or high is None:
        return f"{median:.3f}{unit} (n={count})"

    return (
        f"{median:.3f}{unit} "
        f"(95% CI {low:.3f}–{high:.3f}{unit}, n={count})"
    )



def generate_report(data: dict, output: Path):
    lines = [
        f"# Experiment Report: {data.get('scenario', 'unknown')}",
        "",
        f"- Experiment ID: `{data.get('experiment_id', 'unknown')}`",
        f"- Expected repetitions per configuration: "
        f"{data.get('expected_repetitions_per_configuration', 'n/a')}",
        f"- Run directories discovered: {data.get('discovered_runs', 0)}",
        "",
        "## Configuration results",
        "",
    ]

    for configuration in ("no_qos", "static_qos", "adaptive"):
        summary = data["configurations"].get(configuration, {})

        lines.extend([f"### {configuration}", ""])

        if summary.get("status") == "n/a":
            lines.extend([
                f"Status: **n/a** — {summary.get('reason', 'not configured')}",
                "",
            ])
            continue

        counts = summary.get("run_counts", {})

        lines.extend([
            f"- Valid runs: {counts.get('valid', 0)}",
            f"- Invalid runs: {counts.get('invalid', 0)}",
            f"- Failed runs: {counts.get('failed', 0)}",
            f"- Controller convergence: "
            f"{summary.get('controller_convergence_s', 'n/a')}",
            "",
        ])

        for phase in PHASES:
            phase_data = summary.get("phases", {}).get(phase, {})

            lines.extend([f"#### {phase}", ""])

            # Interactive traffic
            for device in ("call", "game"):
                metrics = phase_data.get(device, {})

                lines.extend([
                    f"**{device.capitalize()}**",
                    "",
                    f"- Forward p50: "
                    f"{format_ci(metrics.get('forward_delay_ms_p50'), ' ms')}",
                    f"- Forward p95: "
                    f"{format_ci(metrics.get('forward_delay_ms_p95'), ' ms')}",
                    f"- Forward p99: "
                    f"{format_ci(metrics.get('forward_delay_ms_p99'), ' ms')}",
                    f"- Reverse p95: "
                    f"{format_ci(metrics.get('reverse_delay_ms_p95'), ' ms')}",
                    f"- RTT p95: "
                    f"{format_ci(metrics.get('rtt_ms_p95'), ' ms')}",
                    f"- Jitter p95: "
                    f"{format_ci(metrics.get('jitter_ms_p95'), ' ms')}",
                    f"- Packet loss: "
                    f"{format_ci(metrics.get('loss_pct'), '%')}",
                    f"- Reordered packets: "
                    f"{format_ci(metrics.get('reordered_packets'))}",
                    "",
                ])

            # Bulk download and fairness
            lines.extend([
                "**Bulk download and fairness**",
                "",
                f"- ISO throughput: "
                f"{format_ci(phase_data.get('iso_download_mbps'), ' Mbps')}",
                f"- Link share: "
                f"{format_ci(phase_data.get('iso_download_link_share'), '%')}",
                f"- TV Jain fairness: "
                f"{format_ci(phase_data.get('tv_jain_fairness_index'))}",
            ])

            guarantee = phase_data.get("bulk_guarantee", {})
            lines.extend([
                f"- Bulk guarantee violations: "
                f"{guarantee.get('runs_below_guarantee', 0)} "
                f"/ {guarantee.get('measured_runs', 0)} measured runs",
                "",
            ])

            # Streaming traffic
            lines.extend([
                "**Streaming traffic**",
                "",
                "| TV | Throughput (Mbps) | Chunks scheduled | "
                "Successful | Failed | Stalled | Delivery-pressure stall (s) "
                "| Download p95 (ms) |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ])

            http = phase_data.get("http", {})

            for tv in ("tv1", "tv2", "tv3"):
                metrics = http.get(tv, {})
                download = metrics.get("download_ms", {})

                lines.append(
                    f"| {tv} "
                    f"| {format_ci(metrics.get('throughput_mbps'))} "
                    f"| {format_ci(metrics.get('scheduled_chunks'))} "
                    f"| {format_ci(metrics.get('successful_chunks'))} "
                    f"| {format_ci(metrics.get('failed_chunks'))} "
                    f"| {format_ci(metrics.get('stalled_chunks'))} "
                    f"| {format_ci(metrics.get('delivery_pressure_stall_seconds'))} "
                    f"| {format_ci(download.get('p95'), ' ms')} |"
                )

            lines.append("")

            # Pooled percentiles are supplementary, not the primary
            # across-run bootstrap confidence intervals.
            pooled = phase_data.get("pooled_udp_supplementary", {})

            lines.extend([
                "**Pooled UDP percentiles (supplementary):**",
                "",
                "| Flow | Metric | Samples | p50 (ms) | p95 (ms) | p99 (ms) |",
                "|---|---|---:|---:|---:|---:|",
            ])

            for device in ("call", "game"):
                for metric in (
                    "forward_delay_ms",
                    "reverse_delay_ms",
                    "rtt_ms",
                ):
                    values = pooled.get(device, {}).get(metric, {})

                    lines.append(
                        f"| {device} | {metric} "
                        f"| {values.get('samples', 0)} "
                        f"| {values.get('p50') if values.get('p50') is not None else 'n/a'} "
                        f"| {values.get('p95') if values.get('p95') is not None else 'n/a'} "
                        f"| {values.get('p99') if values.get('p99') is not None else 'n/a'} |"
                    )

            lines.append("")

        # Host health across valid runs
        lines.extend([
            "#### Host health",
            "",
            f"- Mean host CPU: "
            f"{format_ci(summary.get('host_cpu_mean_pct'), '%')}",
            f"- Maximum host CPU: "
            f"{format_ci(summary.get('host_cpu_max_pct'), '%')}",
            f"- Maximum host memory: "
            f"{format_ci(summary.get('host_memory_max_kb'), ' KB')}",
            f"- Maximum consecutive high-CPU samples: "
            f"{format_ci(summary.get('max_consecutive_high_cpu_samples'))}",
            "",
            "#### Queue health",
            "",
            "| Interface | Maximum backlog (KiB) | Maximum backlog (packets) "
            "| Cumulative drops | Monitor coverage (%) |",
            "|---|---:|---:|---:|---:|",
        ])

        for target in (
            "modem_m_lan",
            "modem_m_wan",
            "router_r_wan",
            "router_ifb0",
        ):
            qdisc = summary.get("qdiscs", {}).get(target, {})

            backlog = qdisc.get("max_backlog_bytes")
            backlog_kib = None

            if isinstance(backlog, dict) and backlog.get("median") is not None:
                backlog_kib = backlog["median"] / 1024

            lines.append(
                f"| {target} "
                f"| {f'{backlog_kib:.2f}' if backlog_kib is not None else 'n/a'} "
                f"| {format_ci(qdisc.get('max_backlog_packets'))} "
                f"| {format_ci(qdisc.get('max_cumulative_drops'))} "
                f"| {format_ci(qdisc.get('coverage_pct'), '%')} |"
            )

        lines.append("")

    lines.extend([
        "## Plots",
        "",
        "![Latency comparison](latency-comparison.png)",
        "",
        "![Throughput comparison](throughput-comparison.png)",
        "",
        "![System health](system-health.png)",
        "",
        "## Interpretation notes",
        "",
        "- Primary latency and throughput summaries use per-run values "
        "and bootstrap confidence intervals.",
        "- Pooled UDP percentiles combine individual packet samples and "
        "are supplementary; they are not independent run-level observations.",
        "- HTTP delivery-pressure stalls measure download time exceeding "
        "the configured chunk interval. They do not directly measure "
        "client playback-buffer stalls.",
        "- Adaptive QoS is not implemented in this experiment and is "
        "reported as n/a.",
        "- Controller convergence is n/a until the adaptive controller "
        "is implemented.",
        "",
    ])

    output.write_text("\n".join(lines) + "\n")


def generate_all(experiment_dir: Path):
    aggregate_path = experiment_dir / "aggregate.json"
    data = load_aggregate(aggregate_path)

    plot_latency(data, experiment_dir / "latency-comparison.png")
    plot_throughput(data, experiment_dir / "throughput-comparison.png")
    plot_system_health(data, experiment_dir / "system-health.png")
    generate_report(data, experiment_dir / "report.md")


def main():
    parser = argparse.ArgumentParser(
        description="Generate experiment plots and Markdown report"
    )
    parser.add_argument("--experiment-dir", required=True)
    args = parser.parse_args()

    experiment_dir = Path(args.experiment_dir).resolve()
    generate_all(experiment_dir)

    print(f"[REPORT] Generated plots and report in {experiment_dir}")


if __name__ == "__main__":
    main()
