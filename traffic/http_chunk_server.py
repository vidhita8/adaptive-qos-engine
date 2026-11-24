
#!/usr/bin/env python3

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


CHUNK_SIZE_LIMIT = 16 * 1024 * 1024


class ChunkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path != "/chunk":
            self.send_error(404, "Unknown endpoint")
            return

        params = parse_qs(parsed.query)

        try:
            size_mb = float(params.get("size_mb", ["1"])[0])
        except ValueError:
            self.send_error(400, "Invalid chunk size")
            return

        if not 0 < size_mb <= 16:
            self.send_error(400, "Chunk size must be between 0 and 16 MB")
            return

        size_bytes = int(size_mb * 1024 * 1024)
        block = b"\0" * (64 * 1024)

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size_bytes))
        self.end_headers()

        remaining = size_bytes

        try:
            while remaining:
                amount = min(remaining, len(block))
                self.wfile.write(block[:amount])
                remaining -= amount
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        print(f"[HTTP] {self.address_string()} - {format % args}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), ChunkHandler)

    print(
        f"[HTTP STREAM] Listening on {args.host}:{args.port}",
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[HTTP STREAM] Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
