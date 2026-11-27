
#!/usr/bin/env python3

"""Generate reproducible UDP traffic for flow-classification datasets."""

import argparse
import random
import socket
import struct
import time

REQUEST = struct.Struct("!QQ")
HEADER_SIZE = REQUEST.size
MAX_UDP_PAYLOAD = 1472


def choose_payload_size(rng, minimum, maximum):
    """Choose a reproducible UDP payload size."""
    return rng.randint(minimum, maximum)


def build_payload(sequence, send_ns, payload_size, rng):
    """Build a timestamped UDP payload without storing traffic data."""
    if not HEADER_SIZE <= payload_size <= MAX_UDP_PAYLOAD:
        raise ValueError("Invalid UDP payload size")

    header = REQUEST.pack(sequence, send_ns)
    return header + rng.randbytes(payload_size - HEADER_SIZE)


def scheduled_send_ns(
    start_ns,
    sequence,
    pattern,
    rate,
    burst_size,
    intra_burst_gap_ms,
):
    """Return the scheduled send timestamp for a packet."""
    if pattern == "regular":
        interval_ns = int(1e9 / rate)
        return start_ns + sequence * interval_ns

    burst_period_ns = int(burst_size / rate * 1e9)
    intra_gap_ns = int(intra_burst_gap_ms * 1e6)

    burst_index = sequence // burst_size
    position_in_burst = sequence % burst_size

    return (
        start_ns
        + burst_index * burst_period_ns
        + position_in_burst * intra_gap_ns
    )


def drain_sockets(sockets):
    """Drain any buffered UDP responses from non-blocking sockets."""
    drained = 0
    for sock in sockets:
        while True:
            try:
                sock.recvfrom(2048)
                drained += 1
            except (BlockingIOError, OSError):
                break
    return drained


def build_parser():
    parser = argparse.ArgumentParser(
        description="Dataset-specific UDP traffic generator"
    )

    parser.add_argument("--server", default="10.30.0.2")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--rate", type=float, default=50.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument(
        "--sessions",
        type=int,
        default=1,
        help="Number of independent UDP flows per round",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Number of sequential rounds with fresh ephemeral source ports",
    )
    parser.add_argument(
        "--drain-ms",
        type=float,
        default=150.0,
        help="Post-round socket drain window in milliseconds",
    )
    parser.add_argument(
        "--pattern",
        choices=["regular", "bursty"],
        default="regular",
    )

    parser.add_argument("--burst-size", type=int, default=5)
    parser.add_argument("--intra-burst-gap-ms", type=float, default=2.0)

    parser.add_argument("--payload-bytes", type=int, default=160)
    parser.add_argument("--min-payload-bytes", type=int)
    parser.add_argument("--max-payload-bytes", type=int)

    parser.add_argument("--seed", type=int, default=1)

    return parser


def validate_args(parser, args):
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    if args.rate <= 0 or args.duration <= 0:
        parser.error("rate and duration must be positive")

    if args.sessions < 1:
        parser.error("sessions must be at least 1")

    if args.rounds < 1:
        parser.error("rounds must be at least 1")

    if args.drain_ms < 0:
        parser.error("drain-ms cannot be negative")

    if args.burst_size < 1:
        parser.error("burst-size must be at least 1")

    if args.intra_burst_gap_ms < 0:
        parser.error("intra-burst-gap-ms cannot be negative")

    minimum = (
        args.min_payload_bytes
        if args.min_payload_bytes is not None
        else args.payload_bytes
    )

    maximum = (
        args.max_payload_bytes
        if args.max_payload_bytes is not None
        else args.payload_bytes
    )

    if not HEADER_SIZE <= minimum <= maximum <= MAX_UDP_PAYLOAD:
        parser.error(
            f"payload sizes must be between {HEADER_SIZE} "
            f"and {MAX_UDP_PAYLOAD} bytes"
        )

    if args.pattern == "bursty":
        burst_period = args.burst_size / args.rate
        intra_gap = args.intra_burst_gap_ms / 1000

        if (args.burst_size - 1) * intra_gap >= burst_period:
            parser.error(
                "burst size and intra-burst gap are too large "
                "for the requested average rate"
            )

    return minimum, maximum



def run_round(
    args,
    family,
    socktype,
    protocol,
    destination,
    minimum,
    maximum,
    round_index,
):
    """Run one round of independent UDP sessions and drain late replies."""
    sockets = []
    random_generators = []
    sent = 0
    errors = 0
    drained = 0

    try:
        for session in range(args.sessions):
            sock = socket.socket(family, socktype, protocol)
            sock.setblocking(False)
            sockets.append(sock)

            # Independent but reproducible payload sequence per session and round.
            random_generators.append(
                random.Random(args.seed + round_index * args.sessions + session)
            )

        duration_ns = int(args.duration * 1e9)
        start_ns = time.monotonic_ns()
        deadline_ns = start_ns + duration_ns
        sequence = 0

        while True:
            scheduled_ns = scheduled_send_ns(
                start_ns,
                sequence,
                args.pattern,
                args.rate,
                args.burst_size,
                args.intra_burst_gap_ms,
            )

            if scheduled_ns >= deadline_ns:
                break

            now_ns = time.monotonic_ns()

            if now_ns < scheduled_ns:
                drained += drain_sockets(sockets)
                time.sleep(
                    min((scheduled_ns - now_ns) / 1e9, 0.01)
                )
                continue

            send_ns = time.monotonic_ns()

            for session, sock in enumerate(sockets):
                rng = random_generators[session]

                payload_size = choose_payload_size(
                    rng, minimum, maximum
                )
                payload = build_payload(
                    sequence, send_ns, payload_size, rng
                )

                try:
                    sock.sendto(payload, destination)
                    sent += 1
                except OSError:
                    errors += 1

            drained += drain_sockets(sockets)
            sequence += 1

        if args.drain_ms > 0:
            drain_deadline_ns = time.monotonic_ns() + int(args.drain_ms * 1e6)
            while time.monotonic_ns() < drain_deadline_ns:
                drained += drain_sockets(sockets)
                time.sleep(0.01)
            drained += drain_sockets(sockets)

    finally:
        for sock in sockets:
            sock.close()

    return sent, errors, drained


def main():
    parser = build_parser()
    args = parser.parse_args()
    minimum, maximum = validate_args(parser, args)

    address_info = socket.getaddrinfo(
        args.server,
        args.port,
        type=socket.SOCK_DGRAM,
    )[0]

    family, socktype, protocol, _, destination = address_info

    start_ns = time.monotonic_ns()
    sent = 0
    errors = 0
    drained = 0

    print(
        f"[DATASET UDP] {args.server}:{args.port} "
        f"sessions={args.sessions} rounds={args.rounds} "
        f"pattern={args.pattern} rate={args.rate}pps/session "
        f"duration={args.duration}s seed={args.seed}",
        flush=True,
    )

    try:
        for round_index in range(args.rounds):
            round_sent, round_errors, round_drained = run_round(
                args,
                family,
                socktype,
                protocol,
                destination,
                minimum,
                maximum,
                round_index,
            )
            sent += round_sent
            errors += round_errors
            drained += round_drained

    except KeyboardInterrupt:
        print("\n[DATASET UDP] Interrupted", flush=True)

    elapsed = (time.monotonic_ns() - start_ns) / 1e9

    print(f"Sessions:     {args.sessions * args.rounds}")
    print(f"Packets sent: {sent}")
    print(f"Drained:      {drained}")
    print(f"Send errors:  {errors}")
    print(f"Elapsed:      {elapsed:.3f}s")
    print(f"Actual rate:  {sent / max(elapsed, 1e-9):.2f}pps")
    print(
        f"Per-session:  "
        f"{sent / max(args.sessions * args.rounds, 1) / max(elapsed, 1e-9):.2f}pps"
    )


if __name__ == "__main__":
    main()
