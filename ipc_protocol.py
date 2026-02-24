"""IPC protocol constants and helpers for Claude Guard."""

import json
import os
import socket
import time

# Socket path
SOCKET_PATH = os.path.expanduser("~/.claude/tools/claude-guard/guard.sock")

# Message types
MSG_LOG = "log"
MSG_REQUEST_APPROVAL = "request_approval"

# Risk levels
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"


def create_message(msg_type: str, tool_name: str, summary_ja: str,
                   risk: str, tool_input: dict | None = None,
                   request_id: str | None = None) -> dict:
    """Create a protocol message."""
    msg = {
        "type": msg_type,
        "tool_name": tool_name,
        "summary_ja": summary_ja,
        "risk": risk,
        "timestamp": time.time(),
    }
    if tool_input is not None:
        msg["tool_input"] = tool_input
    if request_id is not None:
        msg["request_id"] = request_id
    return msg


def encode_message(msg: dict) -> bytes:
    """Encode a message to newline-delimited JSON bytes."""
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def decode_message(data: bytes) -> dict:
    """Decode a newline-delimited JSON message."""
    return json.loads(data.decode("utf-8").strip())


def send_to_daemon(msg: dict, timeout: float = 5.0) -> dict | None:
    """Send a message to the daemon and optionally receive a response.

    Returns the response dict, or None if the daemon is unreachable.
    """
    if not os.path.exists(SOCKET_PATH):
        return None

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(SOCKET_PATH)
        sock.sendall(encode_message(msg))

        if msg["type"] == MSG_LOG:
            return {"status": "logged"}

        # For approval requests, wait for response
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        if buf:
            return decode_message(buf)
        return None
    except (socket.error, socket.timeout, ConnectionRefusedError, OSError):
        return None
    finally:
        sock.close()


def is_daemon_running() -> bool:
    """Check if the daemon is reachable."""
    if not os.path.exists(SOCKET_PATH):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(SOCKET_PATH)
        sock.close()
        return True
    except (socket.error, ConnectionRefusedError, OSError):
        return False
