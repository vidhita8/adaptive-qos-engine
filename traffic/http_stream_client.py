
#!/usr/bin/env python3

import argparse
import csv
import time
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(
        description="HTTP chunk streaming and playback-buffer simulator"
    )

    parser.add_argument("--server", default="10.30.0.2")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--chunks", type=int, default=30)
    parser.add_argument("--size-mb", type=float, default=1.0)
    parser.add_argument("--chunk-duration", type=float, default=2.0)
    parser.add_argument("--initial-buffer", type=float, default=4.0)
    parser.add_argument("--max-buffer", type=float, default=8.0)
    parser.add_argument("--target-buffer", type=float, default=6.0)
    parser.add_argument("--output", required=True)

    args = parser.parse_args()

    if args.chunks <= 0 or args.chunk_duration <= 0:
        parser.error("chunks and chunk-duration must be positive")

    if args.size_mb <= 0 or args.size_mb > 16:
        parser.error("size-mb must be greater than 0 and at most 16")

    if not (
        0 <= args.initial_buffer <= args.max_buffer
        and 0 <= args.target_buffer <= args.max_buffer
    ):
        parser.error("buffer values must be between 0 and max-buffer")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    buffer_seconds = args.initial_buffer
    total_stall_seconds = 0.0
    stall_count = 0

    start_ns = time.monotonic_ns()

    with output.open("w", newline="") as file:
        writer = csv.writer(file)

        writer.writerow([
            "chunk",
            "elapsed_seconds",
            "download_ms",
            "bytes_received",
            "buffer_before_seconds",
            "buffer_after_seconds",
            "stall_seconds",
            "stalled",
        ])

        for chunk_number in range(1, args.chunks + 1):

            # Allow playback to consume excess buffered content.
            if buffer_seconds > args.target_buffer:
                wait_seconds = buffer_seconds - args.target_buffer
                time.sleep(wait_seconds)
                buffer_seconds -= wait_seconds

            buffer_before = buffer_seconds
            request_start_ns = time.monotonic_ns()

            url = (
                f"http://{args.server}:{args.port}/chunk"
                f"?size_mb={args.size_mb}"
            )

            with urlopen(url, timeout=30) as response:
                data = response.read()

            request_end_ns = time.monotonic_ns()

            download_seconds = (
                request_end_ns - request_start_ns
            ) / 1e9

            download_ms = download_seconds * 1000

            # Playback continues while the next chunk downloads.
            stall_seconds = max(
                0.0,
                download_seconds - buffer_before,
            )

            buffer_after = (
                max(0.0, buffer_before - download_seconds)
                + args.chunk_duration
            )

            buffer_seconds = min(
                buffer_after,
                args.max_buffer,
            )

            if stall_seconds > 0:
                stall_count += 1
                total_stall_seconds += stall_seconds

            elapsed_seconds = (
                request_end_ns - start_ns
            ) / 1e9

            writer.writerow([
                chunk_number,
                round(elapsed_seconds, 3),
                round(download_ms, 3),
                len(data),
                round(buffer_before, 3),
                round(buffer_seconds, 3),
                round(stall_seconds, 3),
                int(stall_seconds > 0),
            ])

            print(
                f"Chunk {chunk_number:02d}/{args.chunks}: "
                f"{download_ms:.1f} ms, "
                f"buffer={buffer_seconds:.2f}s, "
                f"stall={stall_seconds:.3f}s",
                flush=True,
            )

    print("\n===== STREAMING SUMMARY =====")
    print(f"Chunks downloaded: {args.chunks}")
    print(f"Stall events:      {stall_count}")
    print(f"Total stall time:  {total_stall_seconds:.3f} seconds")
    print(f"CSV:               {output}")


if __name__ == "__main__":
    main()
