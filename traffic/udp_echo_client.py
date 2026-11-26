
#!/usr/bin/env python3

import argparse
import csv
import math
import select
import socket
import struct
import time
from pathlib import Path

REQUEST = struct.Struct("!QQ")
RESPONSE = struct.Struct("!QQQQ")


def main():
    parser = argparse.ArgumentParser(
        description="Timestamped UDP latency measurement client"
    )
    parser.add_argument("--server", default="10.30.0.2")
    parser.add_argument(
        "--port", type=int, choices=[6001, 6002], required=True
    )
    parser.add_argument("--rate", type=float, default=50.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument(
        "--measurement-start-ns",
        type=int,
        default=None,
        help="Shared monotonic timestamp marking experiment measurement time zero",
    )
    parser.add_argument(
        "--start-s",
        type=float,
        default=0.0,
        help="Flow start time relative to experiment measurement time zero",
    )
    parser.add_argument(
        "--stop-s",
        type=float,
        default=None,
        help="Flow stop time relative to experiment measurement time zero",
    )
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument(
        "--payload-bytes",
        type=int,
        default=REQUEST.size,
        help="Total UDP application payload size, including the 16-byte header",
    )
    parser.add_argument("--output", required=True)

    args = parser.parse_args()

    stop_s = (
        args.stop_s
        if args.stop_s is not None
        else args.start_s + args.duration
    )

    if (
        args.rate <= 0
        or args.timeout <= 0
        or stop_s <= args.start_s
        or args.payload_bytes < REQUEST.size
        or args.payload_bytes > 1472
    ):
        parser.error(
            "rate and timeout must be positive; stop-s must exceed start-s; "
            f"payload-bytes must be between {REQUEST.size} and 1472"
        )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    interval_ns = int(1_000_000_000 / args.rate)
    duration_s = stop_s - args.start_s
    total_packets = math.ceil(args.rate * duration_s)

    sent = {}
    received = {}
    highest_sequence = -1
    previous_transit = None
    jitter_ns = 0.0
    reordered_count = 0

    measurement_start_ns = (
        args.measurement_start_ns
        if args.measurement_start_ns is not None
        else time.monotonic_ns()
    )

    start_ns = measurement_start_ns + int(args.start_s * 1e9)
    final_deadline_ns = (
        measurement_start_ns
        + int((stop_s + args.timeout) * 1e9)
    )

    next_sequence = 0
    next_send_ns = start_ns

    while time.monotonic_ns() < final_deadline_ns:
        now_ns = time.monotonic_ns()

        if next_sequence < total_packets and now_ns >= next_send_ns:
            sequence = next_sequence
            send_ns = time.monotonic_ns()

            payload = REQUEST.pack(sequence, send_ns)
            payload += bytes(args.payload_bytes - REQUEST.size)

            try:
                sock.sendto(payload, (args.server, args.port))
                sent[sequence] = send_ns
            except OSError:
                pass

            next_sequence += 1
            next_send_ns = start_ns + next_sequence * interval_ns

        now_ns = time.monotonic_ns()

        if next_sequence < total_packets:
            wait_ns = max(0, next_send_ns - now_ns)
        else:
            wait_ns = max(0, final_deadline_ns - now_ns)

        wait_seconds = min(wait_ns / 1e9, 0.01)

        readable, _, _ = select.select(
            [sock], [], [], wait_seconds
        )

        if not readable:
            continue

        while True:
            try:
                response, _ = sock.recvfrom(2048)
            except BlockingIOError:
                break

            receive_ns = time.monotonic_ns()

            if len(response) != RESPONSE.size:
                continue

            sequence, echoed_send, server_receive, server_send = (
                RESPONSE.unpack(response)
            )

            if sequence not in sent or sequence in received:
                continue

            is_reordered = sequence < highest_sequence

            if is_reordered:
                reordered_count += 1
            else:
                highest_sequence = sequence

            forward_ms = (server_receive - echoed_send) / 1e6
            reverse_ms = (receive_ns - server_send) / 1e6
            rtt_ms = (receive_ns - echoed_send) / 1e6

            transit_ns = receive_ns - echoed_send

            if previous_transit is not None:
                variation = abs(transit_ns - previous_transit)
                jitter_ns += (variation - jitter_ns) / 16

            previous_transit = transit_ns

            received[sequence] = {
                "server_receive_ns": server_receive,
                "server_send_ns": server_send,
                "client_receive_ns": receive_ns,
                "forward_ms": round(forward_ms, 3),
                "reverse_ms": round(reverse_ms, 3),
                "rtt_ms": round(rtt_ms, 3),
                "jitter_ms": round(jitter_ns / 1e6, 3),
                "reordered": int(is_reordered),
            }

    sock.close()

    with output.open("w", newline="") as file:
        writer = csv.writer(file)

        writer.writerow([
            "sequence",
            "measurement_seconds",
            "client_send_ns",
            "server_receive_ns",
            "server_send_ns",
            "client_receive_ns",
            "forward_delay_ms",
            "reverse_delay_ms",
            "rtt_ms",
            "jitter_ms",
            "reordered",
            "lost",
        ])

        for sequence in sorted(sent):
            measurement_seconds = round(
                (sent[sequence] - measurement_start_ns) / 1e9,
                6,
            )
            result = received.get(sequence)

            if result is None:
                writer.writerow([
                    sequence,
                    measurement_seconds,
                    sent[sequence],
                    "", "", "", "", "", "", "", "", 1
                ])
            else:
                writer.writerow([
                    sequence,
                    measurement_seconds,
                    sent[sequence],
                    result["server_receive_ns"],
                    result["server_send_ns"],
                    result["client_receive_ns"],
                    result["forward_ms"],
                    result["reverse_ms"],
                    result["rtt_ms"],
                    result["jitter_ms"],
                    result["reordered"],
                    0,
                ])

    lost = len(sent) - len(received)

    print(f"Payload:    {args.payload_bytes} bytes")
    print(f"Scheduled:  {total_packets}")
    print(f"Sent:       {len(sent)}")
    print(f"Received:   {len(received)}")
    print(f"Lost:       {lost}")
    print(f"Loss rate:  {lost / max(len(sent), 1) * 100:.2f}%")
    print(f"Reordered:  {reordered_count}")
    print(f"CSV:        {output}")


if __name__ == "__main__":
    main()
