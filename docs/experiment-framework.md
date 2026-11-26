# Experiment Framework

## Overview

The experiment framework runs reproducible, paired comparisons of three QoS configurations:

- **No QoS:** Traffic passes through the testbed without router-side QoS shaping.
- **Static QoS:** Router-side HTB shaping with interactive, streaming, bulk, and default traffic classes.
- **Adaptive:** Reserved configuration placeholder. Not implemented yet; reported as `n/a`.

The framework uses scenario YAML files, Pydantic validation, a controlled run lifecycle, per-second resource and queue monitoring, per-phase metrics, bootstrap confidence intervals, plots, and a generated Markdown report.

## Scenario Configuration

Scenario definitions are stored in `experiments/scenarios/`.

The `iso-download.yaml` scenario specifies:

- Modem upload and download rates, delay, jitter, loss, and scheduled link events.
- Active devices and traffic generators.
- Flow start and stop times.
- Warm-up duration and measurement duration.
- Analysis phases.
- Number of repetitions and randomization strategy.
- Run-validity thresholds.

Pydantic models in `experiments/schema.py` validate scenario structure and constraints. Invalid scenarios are rejected before execution.

Validate a scenario with:

```bash
python -m experiments.validate experiments/scenarios/iso-download.yaml
```

## ISO Download Scenario

The scenario runs a call, a game, and three TV streaming clients throughout the measurement period. A four-stream TCP ISO download starts at measurement time 30 seconds and runs in the download direction.

| Phase | Measurement interval | Traffic condition |
|---|---:|---|
| Baseline | 0–30 s | Call, game, and TVs |
| Bulk ramp | 30–40 s | ISO download starts |
| Bulk steady | 40–120 s | Sustained ISO download |

All clients use a shared monotonic measurement clock. Traffic scheduled before measurement time zero runs during the 10-second warm-up.

The TV clients request 1 MB chunks every two seconds. Their measured download duration and delivery-pressure stalls are application-level observations, not direct measurements of video playback stalls.

## Randomization and Repetitions

The scenario runs ten repetitions for each enabled configuration, giving twenty planned configuration-runs.

Randomization is blocked:

- Each block contains one No QoS run and one Static QoS run.
- Their execution order is randomized within the block.
- Both configurations in a block use the same seed.
- The seed is derived from the scenario seed and repetition number.

This paired design reduces the influence of time-dependent changes in the host or test environment.

Adaptive QoS is disabled until its controller is implemented.

## Run Lifecycle

Each run follows this sequence:

1. Reset the testbed.
2. Run connectivity, modem qdisc, and interface-offload preflight checks.
3. Apply the selected QoS configuration.
4. Capture run metadata and qdisc state.
5. Start traffic servers and verify readiness.
6. Start resource and qdisc monitoring.
7. Execute warm-up and the scheduled traffic timeline.
8. Collect raw traffic measurements and monitoring samples.
9. Stop processes and monitoring.
10. Analyze per-phase metrics and evaluate run validity.
11. Save run status and tear down the testbed.

Cleanup is attempted even when a run fails or receives an interrupt signal.

## Run Validity and Retries

A run can be marked invalid when measurement requirements are not satisfied. Checks include:

- Traffic generator failures.
- Insufficient samples in an analysis phase.
- Unexpected modem qdisc configuration or rate.
- Sustained host CPU saturation.
- Insufficient monitoring coverage.

Host CPU saturation is assessed using consecutive one-second samples above the configured threshold, rather than a single transient spike.

Invalid and failed attempts are retained as evidence. The orchestrator retries an unsuccessful repetition once. A valid completed run is skipped when resuming an existing campaign.

Run state and validity are recorded in each run's `status.json`.

## Metadata and Monitoring

Each run records:

- Scenario name, configuration, and seed.
- Scenario YAML SHA-256 hash.
- Git commit and dirty-working-tree status.
- Kernel, architecture, CPU information, iproute2, and iperf3 versions.
- Qdisc snapshots for modem `m-lan` and `m-wan`, and router `r-wan` and `ifb0`.
- Netem seed capability detection.

The installed iproute2 version does not support the `netem seed` option. This limitation is recorded in metadata. The experiment seed still controls the blocked run plan; it does not make netem impairment randomness reproducible.

The monitor samples host CPU and memory and modem/router qdisc statistics approximately once per second.

## Metrics

Metrics are calculated separately for baseline, bulk-ramp, and bulk-steady phases.

The analysis includes:

- Call and game forward/reverse one-way delay and RTT p50, p95, and p99.
- Jitter, packet loss, and packet reordering.
- ISO download throughput and link share.
- TV HTTP chunk download times, throughput, failures, and delivery-pressure stalls.
- Jain fairness index across the three TVs.
- Bulk throughput share and Static QoS minimum-guarantee checks.
- Modem and router queue backlog, packet counts, and drops.
- Host CPU and memory usage.
- Controller convergence time, reported as `n/a` until an adaptive controller exists.

Per-run measurements are aggregated across valid repetitions. Bootstrap confidence intervals are calculated over the per-run values. Pooled packet percentiles are supplementary and are reported separately from per-run statistics.

## Outputs

Experiment results are stored under:

```text
experiments/results/<experiment-id>/
```

Each run contains:

- `raw/` — traffic CSV files, iperf3 JSON, and monitor JSONL.
- `logs/` — traffic server and client logs.
- `metadata.json` — environment, configuration, and qdisc snapshots.
- `metrics.json` — per-phase analyzed metrics.
- `status.json` — lifecycle state and validity.

The experiment directory also contains:

- `aggregate.json` — per-configuration and per-phase aggregates.
- `report.md` — generated experiment report.
- `latency-by-phase.png`
- `throughput-by-phase.png`
- `system-health.png`

Invalid and failed attempts remain available for inspection and are counted in the report.

## Running the Campaign

Run the complete scenario with:

```bash
make demo
```

Or specify an experiment ID:

```bash
python -m experiments.runner demo \
  --scenario experiments/scenarios/iso-download.yaml \
  --experiment-id iso-download-example
```

To resume a campaign, reuse its experiment ID.

A single configuration-run can be executed independently using the runner's `run` command.

## Limitations

- Adaptive QoS is not implemented yet.
- Netem random seed configuration is unsupported by the installed iproute2 version.
- HTTP delivery-pressure measurements do not directly represent actual video playback stalls.
- One-way delay is application-observed using the shared kernel clock; it is not a physical wire-latency measurement.
- Results depend on host scheduling and available CPU resources. Invalid runs are retained rather than silently discarded.
