# Step 5 — Flow Collector and Labeled Dataset

## Objective

Build a reliable per-flow collector that captures statistical traffic features from the live testbed and generate a sufficiently large labeled dataset for the future classifier (Step 6).

The dataset must:

- Contain several thousand captured flows per traffic class across multiple independently seeded generator configurations.
- Include no IP addresses, port numbers, or packet payloads in exported records.
- Cover all four traffic classes: `interactive`, `gaming`, `streaming`, and `bulk`.
- Be reproducible from a fixed plan file and a fresh testbed.

---

## Architecture

```
Live testbed (7 device namespaces)
        │
        │  raw AF_PACKET sockets (one per router-side device interface)
        ▼
traffic/flow_collector.py          ← per-flow feature extraction and labeling
        │
        │  JSON manifest (device × server-port → class + config)
        ▼
experiments/datasets/collect.py   ← orchestration: servers, generators, collector
        │
        │  CSV rows (no IP/port/payload)
        ▼
experiments/datasets/generated/   ← merged flows.csv + dataset_summary.json

### Components

| File | Role |
|---|---|
| `traffic/flow_collector.py` | Raw socket capture, bidirectional flow tracking, feature computation, manifest-based labeling, stats accounting |
| `traffic/dataset_udp_client.py` | UDP traffic generator with configurable packet sizes, rates, burstiness, and multi-round drain |
| `traffic/http_scheduled_client.py` | HTTP chunk downloader with configurable chunk-size and interval ranges and reproducible seed |
| `experiments/datasets/schema.py` | Pydantic v2 dataset plan and generator config schema |
| `experiments/datasets/collect.py` | End-to-end orchestration: port allocation, manifest generation, server startup, traffic generation, collector lifecycle, CSV merge and verification |
| `experiments/datasets/configs/broadband-dataset-v1.yaml` | Canonical dataset plan (10 generator configs) |
| `experiments/datasets/manifests/` | Per-config JSON manifests (tracked in git, deterministic) |

---

## Flow Collector Design

### Raw AF_PACKET capture

The collector opens one `AF_PACKET / SOCK_RAW` socket per router-side device-facing interface (e.g. `veth-call-rtr`, `veth-game-rtr`, …). Each socket is configured with a 2 MiB receive buffer and uses `select`-based multiplexing so a single thread serves all seven interfaces.

Each `recv(128)` call reads only the first 128 bytes of each frame — enough to cover the full Ethernet, IP/IPv6, and TCP/UDP headers. The IP length field (not the truncated payload) is used to record the original packet size. No packet payload is ever stored.

**Why not Scapy `AsyncSniffer`?** At 100 Mbps with seven concurrent interfaces, Scapy's default 64 KiB kernel receive buffer and full Python-layer parsing (~8,320 pps throughput) caused significant packet drops (~80 % of bulk TCP frames were lost in early testing). Switching to raw sockets with struct-based header-only parsing raises effective throughput to ~800,000 pps and eliminates drops.

Scapy's `process_packet()` API is retained for unit-test compatibility but is not used during live capture.

### Bidirectional flow tracking

A flow is identified by the device, protocol, and canonicalised endpoint pair `(src_ip, dst_ip, src_port, dst_port)`, so packets in both directions belong to the same flow record. The collector tracks packet counts separately for outbound (device → server) and inbound (server → device) traffic.

The `direction_ratio` feature is calculated as:

`outbound_packets / total_packet_count`

It represents the fraction of observed packets sent by the device, not the fraction in the dominant direction. Values near 0 indicate predominantly inbound traffic, values near 0.5 indicate roughly balanced packet counts, and values near 1.0 indicate predominantly outbound traffic.

Flows are closed and exported when the idle timeout (default 5 s) expires. Each exported record includes per-flow statistics computed from all observed packets.

### Manifest-based labeling

A JSON manifest maps `(device_id, protocol, server_port)` to a `(label, generator_config_id)` pair.
For every incoming packet the collector identifies the device (from the interface it arrived on), determines which endpoint is the server (the non-device address), reads its port as `server_port`, and looks up `(device_id, protocol, server_port)`. Flows that do not match any manifest entry are counted as `unmatched_endpoints` and are **not** exported — they are not silently discarded; they appear in the stats output.

#### iperf3 control-connection filtering

Each `iperf3 -c ... -P N` invocation creates N+2 TCP flows: N data streams plus 1–2 short control/teardown flows (typically 8–19 packets, 417–1,184 bytes). Without filtering, these appear as very short `bulk` flows and corrupt feature statistics.

**Solution:** Every `bulk` manifest entry carries `min_packets=40` and `min_bytes=32768`. The collector exports a flow only when both thresholds are satisfied. These thresholds are conservative enough to pass all legitimate iperf3 data streams while reliably filtering all observed control connections. Non-bulk entries keep `min_packets=1, min_bytes=1` so no short `gaming` or `streaming` flow is affected.

### Exported feature fields

The following metadata and feature fields appear in every row of `flows.csv`. No IP address, port number, or payload is included.

| Field | Description |
|---|---|
| `flow_id` | Unique identifier for the exported flow record |
| `label` | Traffic class (`interactive`, `gaming`, `streaming`, `bulk`) |
| `generator_config_id` | Generator configuration identifier (e.g. `cfg-01-home-mix-100m`) |
| `device_id` | Device namespace name (e.g. `call`, `game`, `laptop`) |
| `protocol` | `tcp` or `udp` |
| `packet_size_mean` | Mean IP-level packet size (bytes) |
| `packet_size_std` | Standard deviation of packet size (bytes) |
| `inter_arrival_mean` | Mean inter-packet arrival time (seconds) |
| `inter_arrival_std` | Standard deviation of inter-arrival time (seconds) |
| `duration_s` | Flow duration (seconds) |
| `bytes_per_second` | Mean throughput over the flow lifetime |
| `packets_per_second` | Mean packet rate over the flow lifetime |
| `direction_ratio` | Outbound packet count divided by total packet count. A packet is outbound when its source address belongs to the device namespace (device → server). Values near 0 indicate predominantly inbound traffic, near 0.5 indicate roughly balanced packet counts, and near 1.0 indicate predominantly outbound traffic. |

### Collector statistics

After each capture session the collector writes a JSON stats file containing:

- `exported_flows` and per-label breakdown.
- `filtered_flows` (failed `min_packets` or `min_bytes`) and per-label breakdown.
- `unmatched_packets`, `unmatched_endpoints`, `non_device_packets`, `invalid_packets`, `ignored_interface_packets`.

Stats are used to diagnose problems without having to re-run the capture.

---

## Dataset Plan

The plan file is `experiments/datasets/configs/broadband-dataset-v1.yaml`.

### Generator configs

| Config | Seed | Modem rate | Description |
|---|---:|---:|---|
| `cfg-01-home-mix-100m` | 101001 | 100mbit | Mixed home traffic |
| `cfg-02-unpaced-100m` | 102002 | 100mbit | Unpaced traffic generators |
| `cfg-03-midrate-50m` | 103003 | 50mbit | Mixed traffic at a mid-range modem rate |
| `cfg-04-parallel-100m` | 104004 | 100mbit | Parallel bulk connections |
| `cfg-05-constrained-20m` | 105005 | 20mbit | Constrained modem capacity |
| `cfg-06-laptop-mixed-100m` | 106006 | 100mbit | Concurrent mixed traffic from the laptop |
| `cfg-07-asym-50m` | 107007 | 50mbit | Asymmetric traffic configuration |
| `cfg-08-single-and-many-100m` | 108008 | 100mbit | Single and multiple connections |
| `cfg-09-streaming-heavy-100m` | 109009 | 100mbit | Streaming-heavy traffic |
| `cfg-10-mixed-50m` | 110010 | 50mbit | Mixed traffic at a mid-range modem rate |


### Port allocation

`allocate_ports()` uses `random.Random(config.seed)` to assign server ports deterministically per config, drawing from a range that excludes reserved ports (5201, 6001, 6002, 8080). The same plan file always produces the same port assignments. Ports are written into the manifest and used by generator processes; they never appear in exported feature records.

### Manifests

One JSON manifest is generated per config and stored in `experiments/datasets/manifests/`. Manifests are tracked in git (they are configuration, not generated data). The generated CSV files under `experiments/datasets/generated/` are excluded from git via `.gitignore`.

---

## Observed Dataset Counts

The following counts were obtained from one completed `broadband-dataset-v1` campaign run and verified by `merge_and_verify`. They represent results from that particular execution; re-running the campaign on the same testbed may yield slightly different counts due to timing and queuing variation. The completed campaign exceeded the minimum threshold of 3,000 flows per class.

### By class

| Class | Flows |
|---|---:|
| `interactive` | 3,702 |
| `gaming` | 3,563 |
| `streaming` | 3,185 |
| `bulk` | 3,244 |
| **Total** | **13,694** |

All four classes exceed the `min_flows_per_class=3000` threshold. `meets_threshold: true`.

### By device

| Device | Flows |
|---|---:|
| `laptop` | 3,662 |
| `call` | 2,704 |
| `game` | 2,823 |
| `bulk` | 1,834 |
| `tv2` | 1,008 |
| `tv1` | 897 |
| `tv3` | 766 |

### By config

Each of the 10 configs contributed between 843 and 1,795 flows. All four traffic classes are represented in every config.

### Protocol split

| Protocol | Flows |
|---|---:|
| UDP | 7,265 |
| TCP | 6,429 |



---

## Reproducibility

The testbed must be running before executing the `run` command. Plan validation, manifest generation, and verification of an existing CSV do not require an active testbed.

```bash
# Activate the virtual environment
source .venv/bin/activate

# Validate the plan file
python -m experiments.datasets.collect validate \
    --plan experiments/datasets/configs/broadband-dataset-v1.yaml

# Regenerate manifests (deterministic; same output every time)
python -m experiments.datasets.collect generate-manifests \
    --plan experiments/datasets/configs/broadband-dataset-v1.yaml

# Re-run all 10 generator configs and produce a merged CSV
# (requires sudo and a running testbed)
python -m experiments.datasets.collect run \
    --plan experiments/datasets/configs/broadband-dataset-v1.yaml \
    --min-per-class 3000

# Verify an existing merged CSV without re-running capture
python -m experiments.datasets.collect verify \
    --csv experiments/datasets/generated/broadband-dataset-v1/flows.csv \
    --min-per-class 3000
```
The CLI defaults to `--min-per-class 2000`. The completed dataset campaign explicitly used a threshold of 3000 flows per class.

The `run` subcommand:

1. Validates the plan.
2. Allocates ports and writes manifests.
3. For each config: sets the modem rate, starts UDP/HTTP/iperf3 servers, starts the flow collector, runs all traffic generators for the configured duration, stops generators, waits for the idle timeout, stops the collector, and restores the modem rate.
4. After all configs: merges per-config CSVs into a single `flows.csv`, writes `dataset_summary.json`, and asserts the minimum-flow threshold.

#### Process ownership and cleanup

Every `Popen` object created inside `run_config()` — servers, the flow collector, UDP/HTTP clients, and iperf3 client subprocesses created by worker threads — is registered in a shared `all_procs` list. The `finally` block sets the worker stop event, snapshots the registry under a lock, and invokes `_terminate_procs()` on every registered process group.

`_terminate_procs()` sends `SIGTERM` to process groups, waits up to 5 seconds, and escalates remaining groups to `SIGKILL`. It then reaps the Popen leaders and checks whether any process groups remain. Cleanup failures are collected and reported rather than silently discarded.

iperf3 worker threads carry a `stop_event` flag that is set before `_terminate_procs` runs. This prevents a thread from launching a new `Popen` while cleanup is already terminating existing ones.

Worker threads are non-daemon and are joined after process termination; failures to exit within the join timeout are reported.

Modem rate restoration to 100mbit is attempted in every `finally` block. If it fails, a `[CLEANUP] WARNING:` message is printed with the error detail.

---

## Known Limitations

**8 invalid packets per config.** The raw socket receives Ethernet frames, but the collector only parses supported IPv4 and IPv6 TCP/UDP traffic. Frames with unsupported EtherTypes or malformed network/transport headers are counted as `invalid_packets` and are not exported. The observed count was 8 per config in this campaign.

**Unmatched ICMP packets.** The constrained `cfg-05` configuration recorded 37 unmatched ICMP packets, and `cfg-10` recorded 7. These packets are not exported as flows. They are separate from the labeled TCP/UDP traffic used for the dataset.

**Scapy packet-processing path retained for tests.** The `process_packet()` method is retained for unit tests. Live capture uses raw AF_PACKET sockets and a separate header parser; it does not use Scapy's packet-capture loop.

**Laptop mixed-traffic device coverage.** The `laptop` device runs interactive, gaming, streaming, and bulk traffic concurrently. Its flows are labeled by class, not by concurrent-session membership. The classifier will treat them as independent flows.

---

## Tests
| Test file | Coverage |
|---|---|
| `tests/test_flow_collector.py` | Flow tracking, feature computation, manifest loading, `min_packets`/`min_bytes` threshold filtering, stats accounting, raw Ethernet parsing |
| `tests/test_dataset_udp_client.py` | UDP generator argument validation, rounds and drain-ms behavior, socket draining |
| `tests/test_http_scheduled_client.py` | HTTP client defaults, range-flag reproducibility, validation errors |
| `tests/test_dataset_collection.py` | Plan loading, port allocation (no reserved ports, determinism), manifest dict construction, threshold logic |
| `tests/test_collect_cleanup.py` | Process registration, worker failures, process-group termination, cleanup after failures |
```
Ran 67 tests
OK
```
