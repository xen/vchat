"""Small shared helpers for the isolated load-test processes."""

import ipaddress
import json
import socket
import time
from pathlib import Path


def write_event(event_path: Path, event: str, **fields):
    with event_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"t": time.monotonic(), "event": event, **fields}, ensure_ascii=False
            )
            + "\n"
        )


def loopback_only():
    """Fail closed before any TCP connection to an external provider can occur."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            if not ipaddress.ip_address(address[0]).is_loopback:
                raise RuntimeError(
                    f"Load test prohibits external connection: {address}"
                )

    def connect(sock, address):
        check(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return original_connect_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
