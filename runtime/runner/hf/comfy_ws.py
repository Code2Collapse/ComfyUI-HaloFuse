"""Minimal RFC 6455 websocket client (stdlib only) for ComfyUI's /ws endpoint on container loopback.

Receive-only: JSON text messages (status, executing, progress, executed, execution_*) and binary preview frames
(4-byte big-endian event type: 1 = PREVIEW_IMAGE [4-byte image type 1 JPEG / 2 PNG + bytes],
4 = PREVIEW_IMAGE_WITH_METADATA [4-byte metadata length + JSON + image bytes]; verified in ComfyUI 0.30.1 and 0.39.0
server.py / protocol.py). Reconnects after a ComfyUI restart. Never raises into the caller's thread.
"""
import base64
import json
import os
import socket
import struct
import threading
import time

PREVIEW_IMAGE = 1
PREVIEW_IMAGE_WITH_METADATA = 4
_MAX_MESSAGE = 64 * 2**20


class WSClosed(Exception):
    pass


def _recv_exact(sock, n, stop):
    buf = bytearray()
    while len(buf) < n:
        if stop.is_set():
            raise WSClosed("stopped")
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            continue
        if not chunk:
            raise WSClosed("connection closed")
        buf += chunk
    return bytes(buf)


def _send_frame(sock, opcode, payload=b""):
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def connect(host, port, path, timeout=5.0):
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(request.encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise WSClosed("handshake failed")
        response += chunk
        if len(response) > 65536:
            sock.close()
            raise WSClosed("handshake too large")
    head, _, rest = response.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0]
    if b" 101" not in status:
        sock.close()
        raise WSClosed(f"unexpected handshake status {status[:60]!r}")
    return sock, rest


def read_messages(sock, leftover, stop):
    """Yield (opcode, payload) for complete text (1) / binary (2) messages; answers pings."""
    pending = bytearray(leftover)

    def take(n):
        nonlocal pending
        if len(pending) >= n:
            out = bytes(pending[:n])
            pending = pending[n:]
            return out
        need = n - len(pending)
        out = bytes(pending) + _recv_exact(sock, need, stop)
        pending = bytearray()
        return out

    message, message_op = bytearray(), None
    while not stop.is_set():
        b0, b1 = take(2)
        fin, opcode = b0 & 0x80, b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", take(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", take(8))[0]
        mask = take(4) if b1 & 0x80 else None
        if length > _MAX_MESSAGE:
            raise WSClosed("frame too large")
        payload = take(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            try:
                _send_frame(sock, 0x8, payload[:2])
            except OSError:
                pass
            raise WSClosed("closed by server")
        if opcode == 0x9:
            _send_frame(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x1, 0x2):
            message, message_op = bytearray(payload), opcode
        elif opcode == 0x0 and message_op is not None:
            message += payload
        else:
            continue
        if len(message) > _MAX_MESSAGE:
            raise WSClosed("message too large")
        if fin:
            yield message_op, bytes(message)
            message, message_op = bytearray(), None


def parse_binary(payload: bytes):
    """-> (mime, image_bytes, metadata dict) or None for frames that are not previews."""
    if len(payload) < 8:
        return None
    event = struct.unpack(">I", payload[:4])[0]
    if event == PREVIEW_IMAGE:
        image_type = struct.unpack(">I", payload[4:8])[0]
        mime = "image/png" if image_type == 2 else "image/jpeg"
        return mime, payload[8:], {}
    if event == PREVIEW_IMAGE_WITH_METADATA:
        meta_len = struct.unpack(">I", payload[4:8])[0]
        if 8 + meta_len > len(payload):
            return None
        try:
            meta = json.loads(payload[8:8 + meta_len].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            meta = {}
        mime = meta.get("image_type") if meta.get("image_type") in ("image/png", "image/jpeg") else "image/jpeg"
        return mime, payload[8 + meta_len:], meta if isinstance(meta, dict) else {}
    return None


class ComfyWS:
    """Background reader: on_json(dict) and on_preview(mime, bytes, meta) are called from its thread."""

    def __init__(self, host, port, client_id, on_json, on_preview, on_state=None, retry_seconds=1.0):
        self.host, self.port = host, port
        self.path = f"/ws?clientId={client_id}"
        self.on_json, self.on_preview = on_json, on_preview
        self.on_state = on_state or (lambda state: None)
        self.retry_seconds = retry_seconds
        self._stop = threading.Event()
        self._sock = None
        self.connected = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="hf-comfy-ws", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                sock, rest = connect(self.host, self.port, self.path)
            except (OSError, WSClosed):
                self._stop.wait(self.retry_seconds)
                continue
            sock.settimeout(0.5)
            self._sock = sock
            self.connected.set()
            self.on_state("connected")
            try:
                for opcode, payload in read_messages(sock, rest, self._stop):
                    try:
                        if opcode == 0x1:
                            msg = json.loads(payload.decode("utf-8", "replace"))
                            if isinstance(msg, dict):
                                self.on_json(msg)
                        else:
                            parsed = parse_binary(payload)
                            if parsed is not None:
                                self.on_preview(*parsed)
                    except Exception:  # noqa: BLE001 - a bad message must not stop the reader
                        pass
            except (OSError, WSClosed, ValueError):
                pass
            finally:
                self.connected.clear()
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
                if not self._stop.is_set():
                    self.on_state("disconnected")
            self._stop.wait(self.retry_seconds)
