
import csv
import json
import tempfile
import unittest
from pathlib import Path

from scapy.all import Ether, IP, IPv6, TCP, UDP, Raw

from traffic.flow_collector import (
    FEATURE_FIELDS,
    METADATA_FIELDS,
    FlowCollector,
    ManifestEntry,
    load_device_addresses,
    load_manifest,
    write_csv,
)


LAPTOP_IPV4 = "10.10.0.17"
LAPTOP_IPV6 = "fd10:10::17"
SERVER_IPV4 = "10.30.0.2"
SERVER_IPV6 = "fd10:30::2"


def make_collector(idle_timeout_s=5.0):
    return FlowCollector(
        device_addresses={
            "laptop": {LAPTOP_IPV4, LAPTOP_IPV6},
        },
        manifest=[
            ManifestEntry(
                device_id="laptop",
                protocol="udp",
                server_port=43127,
                label="interactive",
                generator_config_id="laptop-mixed-v1",
            ),
            ManifestEntry(
                device_id="laptop",
                protocol="tcp",
                server_port=51842,
                label="bulk",
                generator_config_id="laptop-mixed-v1",
            ),
        ],
        idle_timeout_s=idle_timeout_s,
    )


class FlowCollectorTests(unittest.TestCase):

    def test_bidirectional_udp_packets_form_one_flow(self):
        collector = make_collector()

        outbound = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"a" * 20)
        )
        inbound = (
            IP(src=SERVER_IPV4, dst=LAPTOP_IPV4)
            / UDP(sport=43127, dport=53000)
            / Raw(load=b"b" * 40)
        )

        collector.process_packet(outbound, "r-laptop", 1.0)
        collector.process_packet(inbound, "r-laptop", 1.5)

        records = collector.flush()

        self.assertEqual(len(records), 1)

        record = records[0]
        self.assertEqual(record["device_id"], "laptop")
        self.assertEqual(record["label"], "interactive")
        self.assertEqual(record["protocol"], "udp")
        self.assertEqual(record["generator_config_id"], "laptop-mixed-v1")

        self.assertEqual(record["duration_s"], 0.5)
        self.assertAlmostEqual(record["inter_arrival_mean"], 0.5)
        self.assertEqual(record["inter_arrival_std"], 0.0)
        self.assertAlmostEqual(record["direction_ratio"], 0.5)

        expected_sizes = [len(outbound[IP]), len(inbound[IP])]
        self.assertAlmostEqual(
            record["packet_size_mean"],
            sum(expected_sizes) / 2,
        )

    def test_laptop_can_have_multiple_classes_simultaneously(self):
        collector = make_collector()

        interactive = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"voice" * 10)
        )
        bulk = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / TCP(sport=53001, dport=51842)
            / Raw(load=b"download" * 100)
        )

        collector.process_packet(interactive, "r-laptop", 1.0)
        collector.process_packet(bulk, "r-laptop", 1.1)

        records = collector.flush()

        self.assertEqual(len(records), 2)

        labels = {
            record["protocol"]: record["label"]
            for record in records
        }

        self.assertEqual(labels["udp"], "interactive")
        self.assertEqual(labels["tcp"], "bulk")

    def test_ipv6_flow_is_collected(self):
        collector = make_collector()

        packet = (
            IPv6(src=LAPTOP_IPV6, dst=SERVER_IPV6)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"ipv6-test")
        )

        collector.process_packet(packet, "r-laptop", 2.0)

        records = collector.flush()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["protocol"], "udp")
        self.assertEqual(records[0]["label"], "interactive")
        self.assertGreater(records[0]["packet_size_mean"], 40)

    def test_idle_timeout_splits_reused_five_tuple(self):
        collector = make_collector(idle_timeout_s=5.0)

        packet = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"test")
        )

        collector.process_packet(packet, "r-laptop", 1.0)
        collector.process_packet(packet, "r-laptop", 2.0)

        # Eight seconds after the previous packet: new flow.
        collector.process_packet(packet, "r-laptop", 10.0)

        records = collector.flush()

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["duration_s"], 1.0)
        self.assertEqual(records[1]["duration_s"], 0.0)
        self.assertNotEqual(
            records[0]["flow_id"],
            records[1]["flow_id"],
        )

    def test_expire_exports_idle_flows(self):
        collector = make_collector(idle_timeout_s=5.0)

        packet = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"test")
        )

        collector.process_packet(packet, "r-laptop", 10.0)
        collector.expire(now=16.0)

        self.assertEqual(len(collector.flows), 0)
        self.assertEqual(len(collector.completed), 1)

        records = collector.flush()
        self.assertEqual(len(records), 1)

    def test_unmatched_packets_are_not_given_guessed_labels(self):
        collector = make_collector()

        packet = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=49999)
            / Raw(load=b"unknown")
        )

        collector.process_packet(packet, "r-laptop", 1.0)

        self.assertEqual(collector.flows, {})
        self.assertEqual(collector.flush(), [])

    def test_exported_records_do_not_contain_addresses_or_ports(self):
        collector = make_collector()

        packet = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"test")
        )

        collector.process_packet(packet, "r-laptop", 1.0)
        record = collector.flush()[0]

        expected_fields = set(METADATA_FIELDS + FEATURE_FIELDS)

        self.assertEqual(set(record), expected_fields)

        forbidden = {
            "src_ip",
            "dst_ip",
            "src_port",
            "dst_port",
            "server_port",
            "source_address",
            "destination_address",
        }

        self.assertTrue(forbidden.isdisjoint(record))

    def test_csv_contains_only_declared_metadata_and_features(self):
        collector = make_collector()

        packet = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"test")
        )

        collector.process_packet(packet, "r-laptop", 1.0)
        records = collector.flush()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flows.csv"
            write_csv(path, records)

            with path.open(newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

            self.assertEqual(len(rows), 1)
            self.assertEqual(
                set(reader.fieldnames),
                set(METADATA_FIELDS + FEATURE_FIELDS),
            )
            self.assertEqual(rows[0]["label"], "interactive")

    def test_topology_addresses_load_for_all_devices(self):
        topology = (
            Path(__file__).resolve().parents[1]
            / "testbed"
            / "topology.env"
        )

        addresses = load_device_addresses(topology)

        self.assertEqual(len(addresses), 7)
        self.assertIn(LAPTOP_IPV4, addresses["laptop"])
        self.assertIn(LAPTOP_IPV6, addresses["laptop"])

    def test_manifest_loader_supports_per_flow_labels(self):
        manifest_data = {
            "generator_config_id": "laptop-mixed-v1",
            "flows": [
                {
                    "device_id": "laptop",
                    "protocol": "udp",
                    "server_port": 43127,
                    "label": "interactive",
                },
                {
                    "device_id": "laptop",
                    "protocol": "tcp",
                    "server_port": 51842,
                    "label": "bulk",
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest_data))

            entries = load_manifest(path)

        self.assertEqual(len(entries), 2)
        self.assertEqual(
            {entry.label for entry in entries},
            {"interactive", "bulk"},
        )

    def test_bulk_thresholds_filter_control_connection_without_affecting_short_flows(self):
        collector = FlowCollector(
            device_addresses={
                "bulk": {"10.10.0.16"},
                "game": {"10.10.0.12"},
                "tv1": {"10.10.0.13"},
            },
            manifest=[
                ManifestEntry(
                    device_id="bulk",
                    protocol="tcp",
                    server_port=51842,
                    label="bulk",
                    generator_config_id="bulk-v1",
                    min_packets=20,
                    min_bytes=10_000,
                ),
                ManifestEntry(
                    device_id="game",
                    protocol="udp",
                    server_port=44111,
                    label="gaming",
                    generator_config_id="game-v1",
                ),
                ManifestEntry(
                    device_id="tv1",
                    protocol="tcp",
                    server_port=48080,
                    label="streaming",
                    generator_config_id="stream-v1",
                ),
            ],
        )

        # Simulated iperf3 control connection: 12 small packets (<20 packets and <10KB)
        for index in range(12):
            ctrl = (
                IP(src="10.10.0.16", dst=SERVER_IPV4)
                / TCP(sport=41000, dport=51842)
                / Raw(load=b"ctrl" * 10)
            )
            collector.process_packet(ctrl, "r-bulk", 1.0 + index * 0.01)

        # Simulated iperf3 data stream: 25 large packets (>=20 packets and >=10KB)
        for index in range(25):
            data = (
                IP(src=SERVER_IPV4, dst="10.10.0.16")
                / TCP(sport=51842, dport=41001)
                / Raw(load=b"x" * 1000)
            )
            collector.process_packet(data, "r-bulk", 1.0 + index * 0.01)

        # Short legitimate gaming flow (3 packets) and short streaming flow (5 packets)
        for index in range(3):
            game_pkt = (
                IP(src="10.10.0.12", dst=SERVER_IPV4)
                / UDP(sport=42000, dport=44111)
                / Raw(load=b"g" * 32)
            )
            collector.process_packet(game_pkt, "r-game", 2.0 + index * 0.02)

        for index in range(5):
            stream_pkt = (
                IP(src=SERVER_IPV4, dst="10.10.0.13")
                / TCP(sport=48080, dport=43000)
                / Raw(load=b"s" * 500)
            )
            collector.process_packet(stream_pkt, "r-tv1", 3.0 + index * 0.01)

        records = collector.flush()
        labels = [record["label"] for record in records]

        self.assertEqual(sorted(labels), ["bulk", "gaming", "streaming"])

        stats = collector.stats()
        self.assertEqual(stats["exported_flows"], 3)
        self.assertEqual(stats["filtered_flows"], 1)
        self.assertEqual(stats["filtered_by_label"], {"bulk": 1})

    def test_manifest_loader_parses_and_validates_per_entry_thresholds(self):
        manifest_data = {
            "generator_config_id": "cfg-1",
            "flows": [
                {
                    "device_id": "laptop",
                    "protocol": "udp",
                    "server_port": 43127,
                    "label": "interactive",
                },
                {
                    "device_id": "bulk",
                    "protocol": "tcp",
                    "server_port": 51842,
                    "label": "bulk",
                    "min_packets": 30,
                    "min_bytes": 32768,
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest_data))
            entries = load_manifest(path)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].min_packets, 1)
        self.assertEqual(entries[0].min_bytes, 1)
        self.assertEqual(entries[1].min_packets, 30)
        self.assertEqual(entries[1].min_bytes, 32768)

        bad_manifest = {
            "generator_config_id": "cfg-1",
            "flows": [
                {
                    "device_id": "bulk",
                    "protocol": "tcp",
                    "server_port": 51842,
                    "label": "bulk",
                    "min_packets": True,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(bad_manifest))
            with self.assertRaises(ValueError):
                load_manifest(path)

    def test_collector_stats_track_unmatched_and_invalid_packets(self):
        collector = make_collector()

        unmatched = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=49999)
            / Raw(load=b"unknown")
        )
        non_device = (
            IP(src="10.10.0.99", dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"other")
        )
        valid = (
            IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"ok")
        )

        collector.process_packet(unmatched, "r-laptop", 1.0)
        collector.process_packet(non_device, "r-laptop", 1.1)
        collector.process_packet(valid, "r-unknown", 1.2)
        collector.process_packet(b"not-a-packet", "r-laptop", 1.3)
        collector.process_packet(valid, "r-laptop", 1.4)
        collector.flush()

        stats = collector.stats()
        self.assertEqual(stats["exported_flows"], 1)
        self.assertEqual(stats["unmatched_packets"], 1)
        self.assertEqual(stats["unmatched_endpoints"], {"laptop:udp:49999": 1})
        self.assertEqual(stats["non_device_packets"], 1)
        self.assertEqual(stats["ignored_interface_packets"], 1)
        self.assertEqual(stats["invalid_packets"], 1)

    def test_raw_ethernet_capture_matches_full_wire_packet_sizes(self):
        collector = make_collector()

        udp_frame = bytes(
            Ether()
            / IP(src=LAPTOP_IPV4, dst=SERVER_IPV4)
            / UDP(sport=53000, dport=43127)
            / Raw(load=b"u" * 200)
        )[:128]
        tcp_v6_frame = bytes(
            Ether()
            / IPv6(src=SERVER_IPV6, dst=LAPTOP_IPV6)
            / TCP(sport=51842, dport=54000)
            / Raw(load=b"t" * 1000)
        )[:128]

        collector.process_raw_ethernet(udp_frame, "r-laptop", 1.0)
        collector.process_raw_ethernet(tcp_v6_frame, "r-laptop", 1.2)
        records = collector.flush()

        by_proto = {rec["protocol"]: rec for rec in records}
        self.assertEqual(set(by_proto), {"udp", "tcp"})
        # 20 IPv4 + 8 UDP + 200 payload = 228 bytes despite 128-byte truncation
        self.assertEqual(by_proto["udp"]["packet_size_mean"], 228.0)
        # 40 IPv6 + 20 TCP + 1000 payload = 1060 bytes despite 128-byte truncation
        self.assertEqual(by_proto["tcp"]["packet_size_mean"], 1060.0)


if __name__ == "__main__":
    unittest.main()
