
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
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--output", required=True)

    args = parser.parse_args()

    if args.rate <= 0 or args.duration <= 0 or args.timeout <= 0:
        parser.error("rate, duration and timeout must be positive")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    interval_ns = int(1_000_000_000 / args.rate)
    total_packets = math.ceil(args.rate * args.duration)

    sent = {}
    received = {}
    highest_sequence = -1
    previous_transit = None
    jitter_ns = 0.0
    reordered_count = 0

    start_ns = time.monotonic_ns()
    next_send_ns = start_ns
    final_deadline_ns = start_ns + int(
        (args.duration + args.timeout) * 1e9
    )

    while time.monotonic_ns() < final_deadline_ns:
        now_ns = time.monotonic_ns()

        # Send one packet when its scheduled time arrives.
        if len(sent) < total_packets and now_ns >= next_send_ns:
            sequence = len(sent)
            send_ns = time.monotonic_ns()

            try:
                sock.sendto(
                    REQUEST.pack(sequence, send_ns),
                    (args.server, args.port),
                )
                sent[sequence] = send_ns
            except OSError:
                pass

            next_send_ns = start_ns + (sequence + 1) * interval_ns

        # Wait for replies without interrupting packet pacing.
        now_ns = time.monotonic_ns()

        if len(sent) < total_packets:
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

            # Inter-arrival jitter using sender-to-receiver transit variation.
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

    # Write every sent packet, including packets that were lost.
    with output.open("w", newline="") as file:
        writer = csv.writer(file)

        writer.writerow([
            "sequence",
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
            result = received.get(sequence)

            if result is None:
                writer.writerow([
                    sequence,
                    sent[sequence],
                    "", "", "", "", "", "", "", "", 1
                ])
            else:
                writer.writerow([
                    sequence,
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

    print(f"Sent:       {len(sent)}")
    print(f"Received:   {len(received)}")
    print(f"Lost:       {lost}")
    print(f"Loss rate:  {lost / max(len(sent), 1) * 100:.2f}%")
    print(f"Reordered:  {reordered_count}")
    print(f"CSV:        {output}")


if __name__ == "__main__":
    main()
