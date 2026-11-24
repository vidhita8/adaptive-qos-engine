# Testbed Verification

**Verification date:** 2026-11-24
**Environment:** Ubuntu 24.04.4 LTS, ARM64, Linux kernel 6.8.0-149-generic
**Project:** Adaptive QoS Engine

## 1. Topology

The testbed models the following network:

`Devices -> Router -> Modem -> ISP`

It contains 10 network namespaces:

- **Devices:** `qos-call`, `qos-game`, `qos-tv1`, `qos-tv2`, `qos-tv3`, `qos-bulk`, `qos-laptop`
- **Infrastructure:** `qos-router`, `qos-modem`, `qos-isp`

The seven device namespaces connect to the router through the Linux bridge `br-lan`.

The modem owns the only network bottleneck. The router currently performs no QoS shaping.

NAT is disabled. Static return routes provide connectivity across the namespaces.

## 2. Addressing and routing

| Network segment | IPv4 | IPv6 |
|---|---|---|
| Device LAN | `10.10.0.0/24` | `fd10:10::/64` |
| Router–modem | `10.20.0.0/30` | `fd10:20::/126` |
| Modem–ISP | `10.30.0.0/30` | `fd10:30::/126` |

Default gateways:

- Devices -> router: `10.10.0.1`, `fd10:10::1`
- Router -> modem: `10.20.0.2`, `fd10:20::2`
- Modem -> ISP: `10.30.0.2`, `fd10:30::2`

Return routes are configured on the modem and ISP so that LAN and transit networks are reachable without NAT.

IPv4 and IPv6 forwarding are enabled in the router and modem namespaces.

## 3. Modem bottleneck configuration

The modem applies the bottleneck in both directions:

| Direction | Interface | Egress traffic |
|---|---|---|
| Upload | `m-wan` | Modem -> ISP |
| Download | `m-lan` | Modem -> router |

The qdisc hierarchy on both interfaces is:

```text
TBF root:   handle 1:
  netem:    handle 10:, parent 1:1
```

Configured values:

| Parameter | Value |
|---|---:|
| TBF rate | 100 Mbps |
| TBF burst | 128 KB |
| TBF queue limit | 3 MB |
| Netem delay | 10 ms |
| Netem jitter | 2 ms |
| Netem loss | 0% |
| Netem packet limit | 2048 |

The 3 MB TBF queue is deliberate. At 100 Mbps, it represents approximately 240 ms of queued data, making bufferbloat observable under load.

Captured `tc -s qdisc show` output reported:

- Upload and download TBF rates: `100Mbit`
- Burst: `128Kb`
- TBF latency: `241ms`
- Netem child: `10: parent 1:1`
- Netem delay: `10ms 2ms`

The router WAN interface `r-wan` reported `noqueue`; it was not shaping traffic during this baseline.

## 4. Dynamic rate-change verification

`testbed/change-rate.sh` updates the modem's TBF rate using `tc qdisc change`.

The script reads qdisc kinds and handles from `topology.env` and verifies that the configured hierarchy exists before changing rates.

Runtime checks passed at:

| Rate | Upload | Download | Netem child preserved |
|---|---|---|---|
| 20 Mbps | PASS | PASS | Yes |
| 50 Mbps | PASS | PASS | Yes |
| 100 Mbps | PASS | PASS | Yes |

The testbed was restored to 100 Mbps after testing.

Observed TBF latency:

| Rate | Reported TBF latency |
|---|---:|
| 20 Mbps | 1.21 s |
| 50 Mbps | 482 ms |
| 100 Mbps | 241 ms |

The decreasing latency is consistent with the fixed 3 MB queue limit.

## 5. Connectivity smoke tests

Run the smoke test with:

```bash
make smoke
```

The smoke test checks every device against both the router and ISP over IPv4 and IPv6.

| Check | Count |
|---|---:|
| Devices | 7 |
| Destinations per device | 2 |
| IP versions | 2 |
| Total checks | 28 |

The smoke test sends three ping packets per destination to tolerate transient cold-start neighbor discovery.

Two consecutive clean teardown/setup cycles passed after this adjustment.

Latest clean rebuild result:

```text
Total:  28
Passed: 28
Failed: 0

[PASS] All smoke tests passed.
```

## 6. Throughput verification

**Tool:** iperf3 3.16
**Server:** `qos-isp`
**Client:** `qos-call`
**Configured modem rate:** 100 Mbps

### Upload

Command:

```bash
sudo ip netns exec qos-call \
  iperf3 -c 10.30.0.2 -t 15 -O 3
```

Observed throughput:

- Client sender: 95.8 Mbps
- Server receiver: 95.6 Mbps

### Download

Command:

```bash
sudo ip netns exec qos-call \
  iperf3 -c 10.30.0.2 -t 15 -O 3 -R
```

Observed throughput:

- Server sender: 95.5 Mbps
- Client receiver: 94.9 Mbps

Both directions reached approximately 95% of the configured 100 Mbps bottleneck.

## 7. Virtual interface offloads

The setup script disables TSO, GSO and GRO on virtual interfaces to keep packet behavior consistent during experiments.

Direct `ethtool -k` verification confirmed all three were off on:

- Seven device `eth0` interfaces
- Router `r-wan`
- Modem `m-lan`
- Modem `m-wan`

These are the 10 interfaces directly verified. The setup script applies the offload configuration to all 18 virtual interface endpoints, including the router bridge-facing interfaces and ISP endpoint.

## 8. Reproducibility

Tear down the testbed:

```bash
sudo ./testbed/down.sh
```

Recreate it:

```bash
sudo ./testbed/up.sh
```

Run the smoke tests:

```bash
make smoke
```

Change the modem rate:

```bash
sudo ./testbed/change-rate.sh 20mbit
sudo ./testbed/change-rate.sh 50mbit
sudo ./testbed/change-rate.sh 100mbit
```

The topology configuration is centralized in `testbed/topology.env`.

## 9. Current scope boundaries

- NAT is disabled; static return routes are used.
- No controller shaping is active on the router yet.
- The modem is the only bottleneck and impairment point.
- Experiment-run metadata capture of modem qdisc state is planned for Step 4, when the experiment framework is implemented.
