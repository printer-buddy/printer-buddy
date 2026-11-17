#LICENSE
#
#Copyright (C) 2026  sterling motuz <Sterling@dexabyte.ca>
#
#for https://github.com/printer-buddy/printer-buddy
#
#SPDX-License-Identifier: AGPL-3.0-only
#
#Additional term (AGPL-3.0 section 7b, keep author credit): keep this header
#
#Additional term (AGPL-3.0 section 7c, mark changed versions): if you make any changes, you must include a CHANGELOG with all changes and their dates
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import time
from urllib.parse import urlsplit

UA = "printer-monitor/0.1"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class HandshakeError(Exception):
    def __init__(self, status: int, msg: str = ""):
        super().__init__(msg or f"HTTP {status}")
        self.status = status


def _split(url: str, schemes: tuple[str, ...]):
    u = urlsplit(url)
    if u.scheme not in schemes:
        raise ValueError(f"unsupported scheme in {url!r}")
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    host_hdr = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
    return u.hostname, u.port or 80, path, host_hdr


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("empty response")
    parts = status_line.decode("latin-1").split(" ", 2)
    status = int(parts[1])
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers


async def _request(method: str, url: str, headers: dict[str, str], max_bytes: int, body: bytes | None = None,
                   chunks=None, length: int = 0):
    host, port, path, host_hdr = _split(url, ("http",))
    reader, writer = await asyncio.open_connection(host, port)
    try:
        hdrs = {"Host": host_hdr, "Connection": "close", "User-Agent": UA, "Accept": "*/*", **headers}
        if body is not None:
            hdrs["Content-Length"] = str(len(body))
        elif chunks is not None:
            hdrs["Content-Length"] = str(length)
        req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        writer.write(req.encode("latin-1"))
        if body:
            writer.write(body)
        elif chunks is not None:
            async for chunk in chunks:
                writer.write(chunk)
                await writer.drain()
        await writer.drain()
        status, rh = await _read_headers(reader)
        body = bytearray()
        if rh.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    break
                body += await reader.readexactly(size)
                await reader.readexactly(2)
                if len(body) > max_bytes:
                    raise ValueError("response too large")
        elif "content-length" in rh:
            n = int(rh["content-length"])
            if n > max_bytes:
                raise ValueError("response too large")
            body += await reader.readexactly(n)
        else:
            while chunk := await reader.read(65536):
                body += chunk
                if len(body) > max_bytes:
                    raise ValueError("response too large")
        return status, rh, bytes(body)
    finally:
        writer.close()


async def http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0,
                   max_bytes: int = 10_000_000) -> tuple[int, dict[str, str], bytes]:
    return await asyncio.wait_for(_request("GET", url, headers or {}, max_bytes), timeout)


async def http_post(url: str, body: bytes, headers: dict[str, str] | None = None, timeout: float = 300.0,
                    max_bytes: int = 2_000_000) -> tuple[int, dict[str, str], bytes]:
    return await asyncio.wait_for(_request("POST", url, headers or {}, max_bytes, body), timeout)


async def http_post_stream(url: str, chunks, length: int, headers: dict[str, str] | None = None, timeout: float = 3600.0,
                           max_bytes: int = 2_000_000) -> tuple[int, dict[str, str], bytes]:
    return await asyncio.wait_for(_request("POST", url, headers or {}, max_bytes, chunks=chunks, length=length), timeout)


class HTTPStream:

    def __init__(self, status: int, headers: dict[str, str], reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.status, self.headers, self._r, self._w = status, headers, reader, writer

    async def chunks(self, size: int = 65536):
        chunked = self.headers.get("transfer-encoding", "").lower() == "chunked"
        while True:
            if chunked:
                n = int((await self._r.readline()).split(b";")[0].strip() or b"0", 16)
                if n == 0:
                    return
                data = await self._r.readexactly(n)
                await self._r.readexactly(2)
            else:
                data = await self._r.read(size)
                if not data:
                    return
            yield data

    def close(self):
        self._w.close()


async def http_stream(url: str, headers: dict[str, str] | None = None, timeout: float = 8.0) -> HTTPStream:
    host, port, path, host_hdr = _split(url, ("http",))
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        hdrs = {"Host": host_hdr, "Connection": "close", "User-Agent": UA, "Accept": "*/*", **(headers or {})}
        writer.write((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n").encode("latin-1"))
        await writer.drain()
        status, rh = await asyncio.wait_for(_read_headers(reader), timeout)
    except Exception:
        writer.close()
        raise
    return HTTPStream(status, rh, reader, writer)


async def get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0):
    status, _, body = await http_get(url, headers, timeout, max_bytes=2_000_000)
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def _mask(data: bytes, key: bytes) -> bytes:
    n = len(data)
    if not n:
        return data
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    head = bytearray([0x80 | opcode])
    mbit = 0x80 if mask else 0
    n = len(payload)
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if mask:
        key = os.urandom(4)
        return bytes(head) + key + _mask(payload, key)
    return bytes(head) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    b1, b2 = await reader.readexactly(2)
    fin, opcode = bool(b1 & 0x80), b1 & 0x0F
    masked, n = bool(b2 & 0x80), b2 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    key = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(n)
    return fin, opcode, (_mask(payload, key) if masked else payload)


class WebSocket:
    """Minimal RFC 6455 client: text messages, fragmentation, ping/pong, close."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, masked: bool = True):
        self._r, self._w = reader, writer
        self._masked = masked
        self._lock = asyncio.Lock()
        self.closed = False
        self.last_rx = time.monotonic()

    @classmethod
    async def connect(cls, url: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> "WebSocket":
        return await asyncio.wait_for(cls._connect(url, headers or {}), timeout)

    @classmethod
    async def _connect(cls, url: str, headers: dict[str, str]) -> "WebSocket":
        host, port, path, host_hdr = _split(url, ("ws",))
        reader, writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        hdrs = {
            "Host": host_hdr, "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13", "User-Agent": UA, **headers,
        }
        req = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        writer.write(req.encode("latin-1"))
        await writer.drain()
        status, rh = await _read_headers(reader)
        if status != 101:
            writer.close()
            raise HandshakeError(status)
        expect = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if rh.get("sec-websocket-accept") != expect:
            writer.close()
            raise HandshakeError(status, "bad Sec-WebSocket-Accept")
        return cls(reader, writer)

    async def _send(self, opcode: int, payload: bytes):
        async with self._lock:
            self._w.write(encode_frame(opcode, payload, self._masked))
            await self._w.drain()

    async def send_text(self, text: str):
        await self._send(0x1, text.encode())

    async def ping(self):
        await self._send(0x9, b"")

    async def recv(self) -> str | None:
        buf = bytearray()
        try:
            while True:
                fin, op, payload = await read_frame(self._r)
                self.last_rx = time.monotonic()
                if op == 0x9:
                    await self._send(0xA, payload)
                elif op == 0xA:
                    pass
                elif op == 0x8:
                    try:
                        await self._send(0x8, payload[:2])
                    except Exception:
                        pass
                    self.closed = True
                    return None
                elif op in (0x0, 0x1, 0x2):
                    buf += payload
                    if fin:
                        return buf.decode("utf-8", "replace")
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed = True
            return None

    async def close(self):
        if not self.closed:
            self.closed = True
            try:
                await asyncio.wait_for(self._send(0x8, struct.pack("!H", 1000)), 2)
            except Exception:
                pass
        self._w.close()

    def abort(self):
        self.closed = True
        self._w.close()
