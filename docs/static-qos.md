
cat > docs/static-qos.md <<'EOF'
# Step 3 — Static QoS and Shaping-Ratio Sweep

## Objective

Implement a static QoS policy on the router to protect latency-sensitive
traffic during bulk transfers. Evaluate the trade-off between latency and
bulk throughput by sweeping the router shaping rate from 70% to 100% of
the modem bottleneck capacity.

The modem remains the actual bottleneck. The router shapes traffic slightly
below that bottleneck to prevent large queues from forming in the modem.

## 1. QoS Architecture

QoS is implemented inside the `qos-router` network namespace.

Two independent shaping paths are configured:

- Upload: HTB on `r-wan`.
- Download: ingress redirection from `r-wan` to IFB device `ifb0`,
  followed by HTB shaping on `ifb0`.

Both paths use four HTB classes. Each class has an `fq_codel` leaf qdisc.

### Traffic classification

The static baseline uses fixed transport-layer ports:

| Traffic class | Protocol and port | Mark |
|---|---|---|
| Interactive call | UDP 6001 | `0x10` |
| Interactive game | UDP 6002 | `0x11` |
| Streaming | TCP 8080 | `0x20` |
| Bulk transfer | TCP 5201 | `0x30` |
| Default | Unmatched traffic | `0x40` |

Upload classification uses nftables in the `inet qos` table.
Packets are marked in the router's prerouting chain and classified
using `tc filter ... fw` on `r-wan`.

Download classification happens at router WAN ingress. TC flower filters
match server source ports, apply an skb mark, and redirect packets to
`ifb0`. The mark is preserved during this redirect and is used by the
IFB fw filters.

IPv4 and IPv6 flower filters are installed. A catch-all matchall filter
redirects unmatched download traffic to IFB so it is handled by the
default class rather than bypassing shaping.

### HTB class configuration

| Class | Class ID | Priority | Guaranteed share |
|---|---|---:|---:|
| Interactive | `1:10` | 0 | 15% |
| Streaming | `1:20` | 1 | 30% |
| Bulk | `1:30` | 2 | 10% |
| Default | `1:40` | 3 | 5% |

These guarantees are percentages of the configured router shaping rate.
They are minimum allocations, not hard throughput caps. Classes can borrow
unused capacity up to the HTB root ceiling.

Bulk therefore receives a guaranteed minimum of 10% while remaining able
to use additional available bandwidth.

Each class uses `fq_codel` with:

- Target: 5 ms
- Interval: 100 ms

The HTB root uses `r2q 100` to avoid excessively small class quantum
warnings.

## 2. Validation at 95 Mbps

The modem bottleneck was configured at 100 Mbps and the router shaper
at 95 Mbps.

Call traffic ran at 50 packets per second while a four-stream TCP bulk
transfer ran concurrently for 25 seconds.

### Upload

| Metric | Result |
|---|---:|
| Call samples | 1,500 |
| Call packet loss | 0 |
| Call packets reordered | 0 |
| Forward latency median | 10.34 ms |
| Forward latency p95 | 12.55 ms |
| Forward latency p99 | 13.85 ms |
| Forward latency maximum | 35.95 ms |
| Bulk receiver throughput | 89.6 Mbps |

### Download

| Metric | Result |
|---|---:|
| Call samples | 1,500 |
| Call packet loss | 0 |
| Call packets reordered | 1 |
| Reverse latency median | 10.38 ms |
| Reverse latency p95 | 12.86 ms |
| Reverse latency p99 | 14.81 ms |
| Reverse latency maximum | 44.56 ms |
| Bulk receiver throughput | 90.0 Mbps |

The router class counters confirmed that interactive and bulk packets
were being classified and processed by their respective HTB classes.
The IFB counters also confirmed download traffic was redirected and shaped.

No class drops were observed in these validation runs.

## 3. Shaping-Ratio Sweep

The sweep evaluated:

- Modem capacities: 100 Mbps and 20 Mbps.
- Router shaping ratios: 70%, 75%, 80%, 85%, 90%, 95%, and 100%.
- Directions: upload and download.
- Call traffic: UDP echo at 50 packets per second.
- Bulk traffic: four parallel TCP streams.
- Duration: 25 seconds per condition.
- Total conditions: 28.

For upload, forward call latency was measured. For download, reverse
call latency was measured.

The sweep runner records raw call CSV files, iperf3 JSON output,
logs, and an incremental `summary.csv`.

### 100 Mbps modem

| Ratio | Upload p95 | Upload p99 | Upload bulk | Download p95 | Download p99 | Download bulk |
|---:|---:|---:|---:|---:|---:|---:|
| 70% | 11.907 | 12.064 | 66.247 | 11.908 | 12.194 | 66.100 |
| 75% | 11.934 | 12.116 | 70.945 | 11.905 | 12.786 | 70.755 |
| 80% | 11.881 | 12.093 | 75.690 | 11.867 | 12.109 | 75.663 |
| 85% | 11.916 | 12.319 | 80.374 | 11.952 | 13.267 | 80.487 |
| 90% | 11.936 | 12.078 | 85.115 | 11.965 | 14.594 | 85.225 |
| 95% | 11.914 | 12.166 | 89.683 | 11.962 | 24.663 | 87.303 |
| 100% | 11.956 | 12.086 | 94.669 | 11.996 | 28.941 | 94.827 |

### 20 Mbps modem

| Ratio | Upload p95 | Upload p99 | Upload bulk | Download p95 | Download p99 | Download bulk |
|---:|---:|---:|---:|---:|---:|---:|
| 70% | 11.931 | 12.456 | 13.242 | 11.841 | 12.029 | 13.170 |
| 75% | 11.894 | 12.089 | 14.081 | 11.895 | 12.148 | 14.093 |
| 80% | 11.899 | 12.093 | 15.084 | 11.884 | 12.063 | 15.099 |
| 85% | 11.889 | 12.228 | 16.051 | 11.869 | 12.049 | 16.106 |
| 90% | 11.945 | 12.875 | 16.804 | 11.898 | 12.048 | 16.944 |
| 95% | 11.890 | 12.075 | 17.935 | 11.907 | 12.092 | 17.951 |
| 100% | 11.883 | 12.135 | 18.856 | 75.200 | 88.819 | 18.832 |

Latency values are milliseconds. Bulk throughput values are Mbps.
All 28 conditions recorded 1,250 call samples and zero call packet loss.

### Plots

The plotting script generates:

- `qos-sweep-100mbit.png`
- `qos-sweep-20mbit.png`

The plots show call p95/p99 latency in both directions alongside
bulk TCP receiver throughput.

Generated experiment data is stored under:

`experiments/results/static-qos-sweep/`

These generated results are intentionally excluded from Git.

## 4. Findings

### 100 Mbps bottleneck

At 70–90%, call p95 remains close to 12 ms in both directions.

Increasing the ratio improves bulk throughput. However, download p99
increases noticeably near the bottleneck:

- 90%: 14.59 ms download p99.
- 95%: 24.66 ms download p99.
- 100%: 28.94 ms download p99.

### 20 Mbps bottleneck

Call latency remains close to 12 ms through 95% shaping.

At 100%, download latency increases sharply:

- Download p95: 75.20 ms.
- Download p99: 88.82 ms.

Bulk throughput reaches approximately 18.83 Mbps, but this comes
with a substantial latency penalty.

### Interpretation

A shaping rate below the actual bottleneck leaves headroom for the
router to control queues before they build up at the modem.

A 90% shaping ratio provides a sensible balance in these experiments:
it maintains low call latency while allowing substantial bulk throughput.

This is an empirical result for this testbed, not a universal optimal
shaping ratio. The appropriate operating point depends on the accuracy
of the bottleneck capacity estimate and changing network conditions.

## 5. Limitations

- Classification uses fixed ports for this static baseline. The adaptive
  classifier developed in later steps must not depend on fixed ports.
- IPv6 classification filters are installed, but the concurrent QoS
  traffic validation and shaping sweep exercised IPv4 traffic.
- TC filters report `not_in_hw` in this software-only namespace testbed.
  This is expected because hardware offload is not being used.
- The testbed does not implement NAT.
- Traffic generators and servers are synthetic rather than real consumer
  applications.
- Each sweep condition lasts 25 seconds; longer-duration behavior is not
  established by these measurements.
- Latency is application-observed one-way delay using timestamps on a
  shared Linux kernel clock. It is not a physical wire-latency measurement.
- The classification policy is static. It does not yet estimate capacity
  or adapt its policy automatically.

## 6. Reproducibility

Configure the static policy at a selected shaping rate:

```bash
sudo /usr/bin/bash qos/apply-static.sh 95mbit
```

Run the shaping-ratio sweep:

```bash
python experiments/run_static_qos_sweep.py
```

Generate the plots:

```bash
python experiments/plot_qos_sweep.py
```

The sweep runner restores the modem to 100 Mbps and the router shaper
to 95 Mbps after completion.
EOF
