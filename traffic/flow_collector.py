
#!/usr/bin/env python3
"""Capture packet metadata and export labeled bidirectional flows."""

from __future__ import annotations

import argparse
import csv
import json
import selectors
import signal
import socket
import statistics
import struct
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scapy.all import IP, IPv6, TCP, UDP


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "experiments" / "datasets" / "flows.csv"

INTERFACES = {
    "r-call": "call",
    "r-game": "game",
    "r-tv1": "tv1",
    "r-tv2": "tv2",
    "r-tv3": "tv3",
    "r-bulk": "bulk",
    "r-laptop": "laptop",
}

METADATA_FIELDS = (
    "flow_id",
    "device_id",
    "generator_config_id",
    "label",
)

FEATURE_FIELDS = (
    "protocol",
    "packet_size_mean",
    "packet_size_std",
    "inter_arrival_mean",
    "inter_arrival_std",
    "duration_s",
    "bytes_per_second",
    "packets_per_second",
    "direction_ratio",
)


@dataclass(frozen=True)
class ManifestEntry:
    device_id: str
    protocol: str
    server_port: int
    label: str
    generator_config_id: str
    min_packets: int = 1
    min_bytes: int = 1

    def __post_init__(self) -> None:
        if self.min_packets < 1:
            raise ValueError("min_packets must be a positive integer")
        if self.min_bytes < 1:
            raise ValueError("min_bytes must be a positive integer")


@dataclass
class Flow:
    flow_id: str
    device_id: str
    generator_config_id: str
    label: str
    protocol: str
    first_seen: float
    last_seen: float
    min_packets: int = 1
    min_bytes: int = 1

    packet_sizes: list[int] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    total_bytes: int = 0
    outbound_packets: int = 0
    inbound_packets: int = 0

    def add_packet(
        self,
        timestamp: float,
        packet_size: int,
        outbound: bool,
    ) -> None:
        self.packet_sizes.append(packet_size)
        self.timestamps.append(timestamp)
        self.total_bytes += packet_size
        self.first_seen = min(self.first_seen, timestamp)
        self.last_seen = max(self.last_seen, timestamp)

        if outbound:
            self.outbound_packets += 1
        else:
            self.inbound_packets += 1

    def meets_thresholds(self) -> bool:
        return (
            len(self.packet_sizes) >= self.min_packets
            and self.total_bytes >= self.min_bytes
        )

    def to_record(self) -> dict[str, Any]:
        packet_count = len(self.packet_sizes)
        duration = max(0.0, self.last_seen - self.first_seen)

        ordered_timestamps = sorted(self.timestamps)
        intervals = [
            later - earlier
            for earlier, later in zip(
                ordered_timestamps,
                ordered_timestamps[1:],
            )
        ]

        return {
            "flow_id": self.flow_id,
            "device_id": self.device_id,
            "generator_config_id": self.generator_config_id,
            "label": self.label,
            "protocol": self.protocol,
            "packet_size_mean": statistics.fmean(self.packet_sizes),
            "packet_size_std": (
                statistics.pstdev(self.packet_sizes)
                if packet_count > 1 else 0.0
            ),
            "inter_arrival_mean": (
                statistics.fmean(intervals) if intervals else 0.0
            ),
            "inter_arrival_std": (
                statistics.pstdev(intervals)
                if len(intervals) > 1 else 0.0
            ),
            "duration_s": duration,
            "bytes_per_second": (
                self.total_bytes / duration if duration > 0 else 0.0
            ),
            "packets_per_second": (
                packet_count / duration if duration > 0 else 0.0
            ),
            "direction_ratio": (
                self.outbound_packets / packet_count
                if packet_count else 0.0
            ),
        }


class FlowCollector:
    """Collect bidirectional flows and attach generator ground truth."""

    def __init__(
        self,
        device_addresses: dict[str, set[str]],
        manifest: list[ManifestEntry],
        idle_timeout_s: float = 5.0,
    ) -> None:
        if idle_timeout_s <= 0:
            raise ValueError("idle_timeout_s must be positive")

        self.device_addresses = device_addresses
        self.idle_timeout_s = idle_timeout_s

        self.manifest: dict[
            tuple[str, str, int], ManifestEntry
        ] = {}

        for entry in manifest:
            key = (
                entry.device_id,
                entry.protocol,
                entry.server_port,
            )
            if key in self.manifest:
                raise ValueError(f"Duplicate manifest entry: {key}")
            self.manifest[key] = entry

        self.flows: dict[tuple[Any, ...], Flow] = {}
        self.completed: list[dict[str, Any]] = []
        self.next_flow_id = 1
        self.lock = threading.Lock()
        self.ignored_interface_packets = 0
        self.invalid_packets = 0
        self.non_device_packets = 0
        self.unmatched_packets = 0
        self.unmatched_endpoints: dict[tuple[str, str, int], int] = defaultdict(int)
        self.filtered_flows = 0
        self.filtered_by_label: dict[str, int] = defaultdict(int)
        self.exported_flows = 0
        self.exported_by_label: dict[str, int] = defaultdict(int)

    def _finalize_flow(self, flow: Flow) -> None:
        if flow.meets_thresholds():
            self.completed.append(flow.to_record())
            self.exported_flows += 1
            self.exported_by_label[flow.label] += 1
        else:
            self.filtered_flows += 1
            self.filtered_by_label[flow.label] += 1

    @staticmethod
    def _extract_packet_info(
        packet: Any,
    ) -> tuple[str, str, int, int, str, int]:
        ip_layer = packet.getlayer(IP)
        if ip_layer is not None:
            src, dst = ip_layer.src, ip_layer.dst
            packet_size = int(ip_layer.len or len(ip_layer))
            ip_proto = ip_layer.proto
            is_ipv6 = False
        else:
            ipv6_layer = packet.getlayer(IPv6)
            if ipv6_layer is None:
                raise ValueError("Unsupported packet: no IP layer")
            src, dst = ipv6_layer.src, ipv6_layer.dst
            packet_size = len(ipv6_layer)
            ip_proto = ipv6_layer.nh
            is_ipv6 = True

        tcp_layer = packet.getlayer(TCP)
        if tcp_layer is not None:
            return src, dst, int(tcp_layer.sport), int(tcp_layer.dport), "tcp", packet_size

        udp_layer = packet.getlayer(UDP)
        if udp_layer is not None:
            return src, dst, int(udp_layer.sport), int(udp_layer.dport), "udp", packet_size

        if not is_ipv6:
            protocol = "icmp" if ip_proto == 1 else "other"
        else:
            protocol = "icmpv6" if ip_proto == 58 else "other"
        return src, dst, 0, 0, protocol, packet_size

    @staticmethod
    def _extract_ethernet_info(
        frame: bytes,
    ) -> tuple[str, str, int, int, str, int]:
        if len(frame) < 14:
            raise ValueError("Truncated Ethernet frame")

        offset = 14
        ethertype = struct.unpack("!H", frame[12:14])[0]
        while ethertype in (0x8100, 0x88A8):
            if len(frame) < offset + 4:
                raise ValueError("Truncated VLAN tag")
            ethertype = struct.unpack("!H", frame[offset + 2:offset + 4])[0]
            offset += 4

        if ethertype == 0x0800:
            if len(frame) < offset + 20:
                raise ValueError("Truncated IPv4 header")
            ihl = (frame[offset] & 0x0F) * 4
            if ihl < 20 or len(frame) < offset + ihl:
                raise ValueError("Invalid IPv4 header length")
            packet_size, ip_proto = struct.unpack(
                "!H5xB", frame[offset + 2:offset + 10]
            )
            src = socket.inet_ntop(socket.AF_INET, frame[offset + 12:offset + 16])
            dst = socket.inet_ntop(socket.AF_INET, frame[offset + 16:offset + 20])
            l4_offset = offset + ihl
            if ip_proto == 6 and len(frame) >= l4_offset + 4:
                sport, dport = struct.unpack("!HH", frame[l4_offset:l4_offset + 4])
                return src, dst, sport, dport, "tcp", packet_size
            if ip_proto == 17 and len(frame) >= l4_offset + 4:
                sport, dport = struct.unpack("!HH", frame[l4_offset:l4_offset + 4])
                return src, dst, sport, dport, "udp", packet_size
            protocol = "icmp" if ip_proto == 1 else "other"
            return src, dst, 0, 0, protocol, packet_size

        if ethertype == 0x86DD:
            if len(frame) < offset + 40:
                raise ValueError("Truncated IPv6 header")
            payload_len, next_hdr = struct.unpack(
                "!H B", frame[offset + 4:offset + 7]
            )
            packet_size = 40 + payload_len
            src = socket.inet_ntop(socket.AF_INET6, frame[offset + 8:offset + 24])
            dst = socket.inet_ntop(socket.AF_INET6, frame[offset + 24:offset + 40])
            l4_offset = offset + 40
            if next_hdr == 6 and len(frame) >= l4_offset + 4:
                sport, dport = struct.unpack("!HH", frame[l4_offset:l4_offset + 4])
                return src, dst, sport, dport, "tcp", packet_size
            if next_hdr == 17 and len(frame) >= l4_offset + 4:
                sport, dport = struct.unpack("!HH", frame[l4_offset:l4_offset + 4])
                return src, dst, sport, dport, "udp", packet_size
            protocol = "icmpv6" if next_hdr == 58 else "other"
            return src, dst, 0, 0, protocol, packet_size

        raise ValueError(f"Unsupported EtherType: {ethertype:#x}")

    def _record_packet(
        self,
        interface: str,
        src: str,
        dst: str,
        src_port: int,
        dst_port: int,
        protocol: str,
        packet_size: int,
        timestamp: float,
    ) -> None:
        device_id = INTERFACES[interface]
        known_addresses = self.device_addresses.get(device_id, set())

        if src in known_addresses:
            outbound = True
            server_port = dst_port
        elif dst in known_addresses:
            outbound = False
            server_port = src_port
        else:
            with self.lock:
                self.non_device_packets += 1
            return

        # The generator manifest supplies ground-truth labels.
        entry = self.manifest.get(
            (device_id, protocol, server_port)
        )
        if entry is None:
            with self.lock:
                self.unmatched_packets += 1
                self.unmatched_endpoints[(device_id, protocol, server_port)] += 1
            return

        endpoint_a = (src, src_port)
        endpoint_b = (dst, dst_port)

        if endpoint_a > endpoint_b:
            endpoint_a, endpoint_b = endpoint_b, endpoint_a

        key = (device_id, protocol, endpoint_a, endpoint_b)

        with self.lock:
            flow = self.flows.get(key)

            # A reused five-tuple after an idle period is a new flow.
            if (
                flow is not None
                and timestamp - flow.last_seen >= self.idle_timeout_s
            ):
                self._finalize_flow(flow)
                del self.flows[key]
                flow = None

            if flow is None:
                flow = Flow(
                    flow_id=f"flow-{self.next_flow_id:08d}",
                    device_id=device_id,
                    generator_config_id=entry.generator_config_id,
                    label=entry.label,
                    protocol=protocol,
                    first_seen=timestamp,
                    last_seen=timestamp,
                    min_packets=entry.min_packets,
                    min_bytes=entry.min_bytes,
                )
                self.next_flow_id += 1
                self.flows[key] = flow

            flow.add_packet(timestamp, packet_size, outbound)

    def process_raw_ethernet(
        self,
        frame: bytes,
        interface: str,
        timestamp: float | None = None,
    ) -> None:
        if interface not in INTERFACES:
            with self.lock:
                self.ignored_interface_packets += 1
            return

        try:
            src, dst, src_port, dst_port, protocol, packet_size = (
                self._extract_ethernet_info(frame)
            )
        except (ValueError, OSError, struct.error):
            with self.lock:
                self.invalid_packets += 1
            return

        self._record_packet(
            interface,
            src,
            dst,
            src_port,
            dst_port,
            protocol,
            packet_size,
            time.time() if timestamp is None else timestamp,
        )

    def process_packet(
        self,
        packet: Any,
        interface: str,
        timestamp: float | None = None,
    ) -> None:
        if interface not in INTERFACES:
            with self.lock:
                self.ignored_interface_packets += 1
            return

        try:
            src, dst, src_port, dst_port, protocol, packet_size = (
                self._extract_packet_info(packet)
            )
        except (ValueError, AttributeError, TypeError):
            with self.lock:
                self.invalid_packets += 1
            return

        if timestamp is None:
            timestamp = float(packet.time)

        self._record_packet(
            interface,
            src,
            dst,
            src_port,
            dst_port,
            protocol,
            packet_size,
            timestamp,
        )

    def expire(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()

        with self.lock:
            expired = [
                key
                for key, flow in self.flows.items()
                if now - flow.last_seen >= self.idle_timeout_s
            ]

            for key in expired:
                flow = self.flows.pop(key)
                self._finalize_flow(flow)

    def flush(self) -> list[dict[str, Any]]:
        with self.lock:
            for flow in self.flows.values():
                self._finalize_flow(flow)

            self.flows.clear()
            records = self.completed
            self.completed = []

        return records

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return {
                "exported_flows": self.exported_flows,
                "exported_by_label": dict(sorted(self.exported_by_label.items())),
                "filtered_flows": self.filtered_flows,
                "filtered_by_label": dict(sorted(self.filtered_by_label.items())),
                "unmatched_packets": self.unmatched_packets,
                "unmatched_endpoints": {
                    f"{device}:{proto}:{port}": count
                    for (device, proto, port), count in sorted(
                        self.unmatched_endpoints.items()
                    )
                },
                "non_device_packets": self.non_device_packets,
                "invalid_packets": self.invalid_packets,
                "ignored_interface_packets": self.ignored_interface_packets,
                "active_flows": len(self.flows),
            }


def load_device_addresses(path: Path) -> dict[str, set[str]]:
    """Load device addresses from the project's topology.env."""
    values: dict[str, str] = {}

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    addresses: dict[str, set[str]] = defaultdict(set)

    for device in INTERFACES.values():
        for suffix in ("IPV4", "IPV6"):
            value = values.get(f"{device.upper()}_{suffix}")
            if value:
                addresses[device].add(value.split("/")[0])

    missing = [
        device for device in INTERFACES.values()
        if not addresses[device]
    ]

    if missing:
        raise ValueError(
            "Missing device addresses in topology.env: "
            + ", ".join(missing)
        )

    return dict(addresses)


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Load per-flow ground-truth metadata."""
    data = json.loads(path.read_text())

    if not isinstance(data, dict):
        raise ValueError("Manifest must be a JSON object")

    config_id = data.get("generator_config_id")
    if not isinstance(config_id, str) or not config_id:
        raise ValueError("Manifest needs generator_config_id")

    flows = data.get("flows")
    if not isinstance(flows, list) or not flows:
        raise ValueError("Manifest needs a non-empty flows list")

    entries = []

    for item in flows:
        if not isinstance(item, dict):
            raise ValueError("Each manifest flow must be an object")

        device = item.get("device_id")
        protocol = item.get("protocol")
        port = item.get("server_port")
        label = item.get("label")
        flow_config = item.get("generator_config_id", config_id)
        min_packets = item.get("min_packets", 1)
        min_bytes = item.get("min_bytes", 1)

        if (
            not isinstance(min_packets, int)
            or isinstance(min_packets, bool)
            or min_packets < 1
        ):
            raise ValueError("min_packets must be a positive integer")

        if (
            not isinstance(min_bytes, int)
            or isinstance(min_bytes, bool)
            or min_bytes < 1
        ):
            raise ValueError("min_bytes must be a positive integer")

        if device not in INTERFACES.values():
            raise ValueError(f"Unknown device_id: {device}")

        if protocol not in ("tcp", "udp"):
            raise ValueError(f"Unsupported protocol: {protocol}")

        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise ValueError(f"Invalid server_port: {port}")

        if not isinstance(label, str) or not label:
            raise ValueError("Each flow needs a non-empty label")

        if not isinstance(flow_config, str) or not flow_config:
            raise ValueError("Invalid generator_config_id")

        entries.append(
            ManifestEntry(
                device_id=device,
                protocol=protocol,
                server_port=port,
                label=label,
                generator_config_id=flow_config,
                min_packets=min_packets,
                min_bytes=min_bytes,
            )
        )

    return entries


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = list(METADATA_FIELDS + FEATURE_FIELDS)
    write_header = not path.exists() or path.stat().st_size == 0

    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)

        if write_header:
            writer.writeheader()

        writer.writerows(records)


def capture(
    collector: FlowCollector,
    duration_s: float,
    output: Path,
    stats_output: Path | None = None,
) -> dict[str, Any]:
    """Capture packet headers on all device-facing interfaces until duration or signal."""
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")

    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    selector = selectors.DefaultSelector()
    sockets: list[socket.socket] = []
    eth_p_all = socket.htons(0x0003)

    try:
        for iface in INTERFACES:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, eth_p_all)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            sock.bind((iface, 0x0003))
            sock.setblocking(False)
            sockets.append(sock)
            selector.register(sock, selectors.EVENT_READ, data=iface)

        deadline = time.monotonic() + duration_s
        next_expire = time.monotonic() + 0.5

        while not stop_requested.is_set():
            now_mono = time.monotonic()
            if now_mono >= deadline:
                break

            timeout = min(0.25, max(0.0, deadline - now_mono))
            events = selector.select(timeout=timeout)
            for key, _ in events:
                sock = key.fileobj
                iface = key.data
                while True:
                    try:
                        # Truncate to 128 bytes: Ethernet + IP + TCP/UDP headers only.
                        frame = sock.recv(128)
                    except BlockingIOError:
                        break
                    collector.process_raw_ethernet(frame, iface, time.time())

            now_mono = time.monotonic()
            if now_mono >= next_expire:
                collector.expire(time.time())
                next_expire = now_mono + 0.5

    finally:
        selector.close()
        for sock in sockets:
            sock.close()

        write_csv(output, collector.flush())

    summary = collector.stats()
    if stats_output is not None:
        stats_output.parent.mkdir(parents=True, exist_ok=True)
        stats_output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    print(
        f"[FLOW COLLECTOR] exported={summary['exported_flows']} "
        f"filtered={summary['filtered_flows']} "
        f"unmatched_packets={summary['unmatched_packets']} "
        f"non_device_packets={summary['non_device_packets']} "
        f"invalid_packets={summary['invalid_packets']} "
        f"by_label={summary['exported_by_label']}",
        flush=True,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture labeled network flow metadata."
    )
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--idle-timeout", type=float, default=5.0)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stats-output", type=Path, default=None)
    parser.add_argument(
        "--topology",
        type=Path,
        default=ROOT / "testbed" / "topology.env",
    )
    args = parser.parse_args()

    if args.duration <= 0:
        parser.error("--duration must be positive")

    collector = FlowCollector(
        device_addresses=load_device_addresses(args.topology),
        manifest=load_manifest(args.manifest),
        idle_timeout_s=args.idle_timeout,
    )

    capture(collector, args.duration, args.output, stats_output=args.stats_output)


if __name__ == "__main__":
    main()
