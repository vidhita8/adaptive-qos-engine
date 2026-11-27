
import random
import struct
import unittest

from traffic.dataset_udp_client import (
    HEADER_SIZE,
    MAX_UDP_PAYLOAD,
    build_parser,
    build_payload,
    choose_payload_size,
    drain_sockets,
    scheduled_send_ns,
    validate_args,
)


class DatasetUDPClientTests(unittest.TestCase):

    def test_payload_size_is_reproducible(self):
        first = random.Random(42)
        second = random.Random(42)

        sizes_a = [
            choose_payload_size(first, 64, 512)
            for _ in range(100)
        ]
        sizes_b = [
            choose_payload_size(second, 64, 512)
            for _ in range(100)
        ]

        self.assertEqual(sizes_a, sizes_b)
        self.assertTrue(all(64 <= size <= 512 for size in sizes_a))

    def test_payload_contains_sequence_and_timestamp(self):
        rng = random.Random(42)

        payload = build_payload(
            sequence=7,
            send_ns=123456789,
            payload_size=160,
            rng=rng,
        )

        sequence, timestamp = struct.unpack(
            "!QQ", payload[:HEADER_SIZE]
        )

        self.assertEqual(sequence, 7)
        self.assertEqual(timestamp, 123456789)
        self.assertEqual(len(payload), 160)

    def test_payload_content_is_reproducible(self):
        first = build_payload(
            1, 100, 200, random.Random(42)
        )
        second = build_payload(
            1, 100, 200, random.Random(42)
        )

        self.assertEqual(first, second)

    def test_payload_rejects_invalid_sizes(self):
        for size in (HEADER_SIZE - 1, MAX_UDP_PAYLOAD + 1):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    build_payload(
                        1, 100, size, random.Random(1)
                    )

    def test_regular_schedule_uses_fixed_intervals(self):
        start = 1_000_000_000
        rate = 50

        times = [
            scheduled_send_ns(
                start, i, "regular", rate, 5, 2
            )
            for i in range(5)
        ]

        expected_interval = int(1e9 / rate)

        self.assertEqual(times[0], start)
        self.assertEqual(
            [times[i + 1] - times[i] for i in range(4)],
            [expected_interval] * 4,
        )

    def test_bursty_schedule_groups_packets(self):
        start = 1_000_000_000
        rate = 50
        burst_size = 5
        gap_ms = 2

        times = [
            scheduled_send_ns(
                start, i, "bursty", rate, burst_size, gap_ms
            )
            for i in range(10)
        ]

        gap_ns = 2_000_000
        burst_period_ns = 100_000_000

        self.assertEqual(times[0], start)
        self.assertEqual(times[1] - times[0], gap_ns)
        self.assertEqual(times[4] - times[3], gap_ns)
        self.assertEqual(
            times[5] - times[4],
            burst_period_ns - 4 * gap_ns,
        )
        self.assertEqual(
            times[6] - times[5],
            gap_ns,
        )

    def test_bursty_schedule_is_reproducible(self):
        first = [
            scheduled_send_ns(
                1000, i, "bursty", 60, 4, 3
            )
            for i in range(20)
        ]
        second = [
            scheduled_send_ns(
                1000, i, "bursty", 60, 4, 3
            )
            for i in range(20)
        ]

        self.assertEqual(first, second)

    def test_sessions_must_be_positive(self):
        parser = build_parser()

        for sessions in (0, -1):
            with self.subTest(sessions=sessions):
                args = parser.parse_args([
                    "--port", "43127",
                    "--sessions", str(sessions),
                ])

                with self.assertRaises(SystemExit):
                    validate_args(parser, args)

    def test_rounds_and_drain_ms_validation(self):
        parser = build_parser()

        for bad_args in (
            ["--port", "43127", "--rounds", "0"],
            ["--port", "43127", "--drain-ms", "-1"],
        ):
            with self.subTest(bad_args=bad_args):
                args = parser.parse_args(bad_args)
                with self.assertRaises(SystemExit):
                    validate_args(parser, args)

    def test_drain_sockets_empties_queued_datagrams(self):
        import socket

        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            receiver.bind(("127.0.0.1", 0))
            receiver.setblocking(False)
            addr = receiver.getsockname()
            for idx in range(3):
                sender.sendto(f"msg-{idx}".encode(), addr)

            # Small yield to ensure loopback delivery before draining.
            import time
            time.sleep(0.01)

            self.assertEqual(drain_sockets([receiver]), 3)
            self.assertEqual(drain_sockets([receiver]), 0)
        finally:
            receiver.close()
            sender.close()


if __name__ == "__main__":
    unittest.main()
