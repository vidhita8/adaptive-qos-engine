# Bufferbloat Baseline — No QoS

## Objective

Measure how competing traffic affects latency-sensitive and streaming
applications when the modem is the only network bottleneck and no QoS
shaping is active on the router.

## Testbed configuration

- Bottleneck location: modem
- Initial link rate: 100 Mbps
- Reduced link rate: 20 Mbps
- TBF queue limit: 3 MB
- TBF burst: 128 KB
- Netem child queue limit: 2048 packets
- Added propagation delay: 10 ms
- Added jitter: 2 ms
- Configured packet loss: 0%
- Router WAN qdisc: noqueue
- QoS controller: disabled

The netem queue limit was initially left at its Linux default of
1000 packets. This restricted the effective queue size and prevented
the configured 3 MB TBF queue from being fully utilized.

The netem limit was explicitly increased to 2048 packets. The corrected
overload test reached a peak backlog of 2922.55 KiB (2046 packets).

## Traffic generators

| Traffic | Generator | Port |
|---|---|---:|
| Call | UDP timestamp echo | 6001 |
| Game | UDP timestamp echo | 6002 |
| Streaming | HTTP chunk transfer | 8080 |
| Bulk | iperf3 TCP/UDP | 5201 |

Call and game measurements record forward delay, reverse delay, RTT,
jitter, loss and packet reordering.

Streaming measurements record chunk download time, playback buffer
and stalls.

## Results

### Idle and competing traffic

| Experiment | Measurement | Result |
|---|---|---:|
| Call idle | Forward median | 11.44 ms |
| Call idle | Forward p95 | 13.84 ms |
| Call + bulk, 100 Mbps | Forward p95 | 115.22 ms |
| Call + bulk, 100 Mbps | Forward maximum | 122.53 ms |
| Laptop call + bulk download | Forward median | 10.11 ms |
| Laptop call + bulk download | Reverse median | 97.54 ms |
| Laptop call + bulk download | RTT median | 107.71 ms |

The laptop download experiment demonstrates directional queueing:
reverse-path delay increased substantially while forward-path delay
remained low.

### Synchronized 100-to-20 Mbps rate change

The call generator and four-stream TCP bulk transfer ran concurrently.
The modem rate was changed from 100 Mbps to 20 Mbps approximately
16 seconds into the experiment.

| Measurement | Before rate drop | After rate drop |
|---|---:|---:|
| Forward median | 90.96 ms | 506.87 ms |
| Forward p95 | 116.16 ms | 584.62 ms |
| Forward maximum | 427.72 ms | 601.61 ms |
| Reverse median | 10.25 ms | 10.06 ms |
| RTT median | 101.08 ms | 516.58 ms |

After the rate drop, 5 call probes were lost and 7 were reordered.

The reverse-path median remained around 10 ms, indicating that the
large delay increase was on the upload path.

### Saturated 20 Mbps upload queue

A separate diagnostic test offered 30 Mbps UDP traffic to the
20 Mbps bottleneck while sending 50 call probes per second.

| Measurement | Result |
|---|---:|
| Offered UDP rate | 30 Mbps |
| Receiver throughput | 19.4 Mbps |
| UDP bulk packet loss | 33% |
| Call probes sent | 2000 |
| Call probes lost | 177 (8.85%) |
| Call probes reordered | 51 |
| Peak queue backlog | 2922.55 KiB |
| Peak queued packets | 2046 |
| Forward delay median | 1189.08 ms |
| Forward delay p95 | 1193.37 ms |
| Forward delay maximum | 1214.48 ms |
| Reverse delay median | 10.49 ms |

The queue reached approximately 99.4% of its 3 MiB capacity.
The measured forward delay was close to the theoretical queue
serialization delay of approximately 1.2 seconds at 20 Mbps.

### Saturated 100 Mbps upload queue

A controlled overload test offered 120 Mbps of UDP bulk traffic to
the 100 Mbps bottleneck while measuring call latency.

| Measurement | Result |
|---|---:|
| Offered UDP rate | 120 Mbps |
| Receiver throughput | 97.1 Mbps |
| UDP bulk packet loss | 18% |
| Call probes sent | 2000 |
| Call probes received | 1148 |
| Call probe loss | 42.60% |
| Call probes reordered | 8 |
| Peak queue backlog | 2975.80 KiB |
| Peak queued packets | 2048 |
| Forward delay median | 241.11 ms |
| Forward delay p95 | 244.69 ms |
| Forward delay maximum | 248.68 ms |
| Reverse delay median | 10.78 ms |
| RTT median | 250.45 ms |

The modem queue reached its configured 2048-packet limit. Forward
application-observed delay reached approximately 241 ms, matching
the theoretical serialization delay of a 3 MB queue at 100 Mbps.

The high call-probe loss is a consequence of deliberately offering
more traffic than the bottleneck can transmit. This is a stress test,
not a normal operating condition.

### Streaming

| Measurement | Idle | Concurrent bulk download |
|---|---:|---:|
| Chunk download median | 183.52 ms | 927.64 ms |
| Chunk download p95 | 201.23 ms | 1316.75 ms |
| Chunk download maximum | 243.02 ms | 1790.63 ms |
| Stall events | 0 | 0 |
| Total stall time | 0 seconds | 0 seconds |

The playback buffer absorbed the increased download time. No stalls
occurred in either run.

## Plots

Latency plots are stored in `experiments/results/plots/`.

- `call-idle.png`
- `game-idle.png`
- `call-plus-bulk.png`
- `call-rate-drop.png`
- `call-20mbit-udp-overload.png`
- `laptop-call-plus-download.png`
- `stream-idle.png`
- `stream-plus-bulk-download.png`
- `call-100mbit-udp-overload.png`

Raw CSV measurements are stored in `experiments/results/baseline/`.
These generated results are excluded from Git by `.gitignore`; the
plotting scripts and this report are intended to be committed.

## Interpretation

The experiments demonstrate bufferbloat under competing traffic.

At 100 Mbps, saturating the modem queue increased median forward
application-observed delay to 241.11 ms, while reverse delay remained
around 10.78 ms.

At 20 Mbps, the saturated queue increased median forward delay to
1189.08 ms. Both results closely match the expected serialization
delay of the configured queue at their respective bottleneck rates.

The synchronized rate-drop experiment independently demonstrated
that reducing capacity from 100 Mbps to 20 Mbps causes a substantial
increase in forward delay.

Streaming download time increased by approximately five times under
bulk download traffic, but the playback buffer prevented stalls.

These results establish a measurable no-QoS baseline against which
the static QoS and adaptive QoS implementations can be evaluated.

## Measurement limitations

- One-way delay is application-observed using timestamps from the
  shared kernel clock. It includes application processing and
  scheduling effects.
- The theoretical TBF queue delay is a capacity estimate, not a
  guaranteed observed delay.
- The 20 Mbps saturated-queue diagnostic intentionally overloads UDP
  traffic and therefore produces packet loss.
- Streaming results depend on the configured playback buffer.
- No QoS policy was active during these baseline experiments.