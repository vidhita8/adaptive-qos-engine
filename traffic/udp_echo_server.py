
#!/usr/bin/env python3

import argparse
import socket
import struct
import time

REQUEST = struct.Struct("!QQ")
RESPONSE = struct.Struct("!QQQQ")


def serve(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))

    print(f"[UDP ECHO] Listening on UDP port {port}", flush=True)

    while True:
        packet, address = sock.recvfrom(2048)

        if len(packet) != REQUEST.size:
            continue

        sequence, client_send_ns = REQUEST.unpack(packet)
        server_receive_ns = time.monotonic_ns()

        server_send_ns = time.monotonic_ns()

        response = RESPONSE.pack(
            sequence,
            client_send_ns,
            server_receive_ns,
            server_send_ns,
        )

        sock.sendto(response, address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", nargs="+", type=int,
                        default=[6001, 6002])
    args = parser.parse_args()

    import threading

    threads = []

    for port in args.ports:
        thread = threading.Thread(
            target=serve,
            args=(port,),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[UDP ECHO] Shutting down")


if __name__ == "__main__":
    main()
