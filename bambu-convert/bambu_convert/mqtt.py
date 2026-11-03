import asyncio
import ssl
import struct


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _packet(kind: int, body: bytes) -> bytes:
    n, out = len(body), bytearray([kind])
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + body


class MQTT:
    """Just enough MQTT 3.1.1 (QoS 0) for a Bambu printer's local broker."""

    def __init__(self, host: str, user: str, password: str, client_id: str, port: int = 8883):
        self.host, self.port, self.user, self.password, self.client_id = host, port, user, password, client_id
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._ids = 0

    async def connect(self, keepalive: int = 60):
        self._r, self._w = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=_ctx(), server_hostname=None), 10)
        body = _str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", keepalive)
        body += _str(self.client_id) + _str(self.user) + _str(self.password)
        self._w.write(_packet(0x10, body))
        await self._w.drain()
        kind, data = await asyncio.wait_for(self._read(), 10)
        if kind >> 4 != 2 or len(data) < 2 or data[1] != 0:
            raise ConnectionError("access code rejected" if len(data) > 1 and data[1] in (4, 5) else "MQTT connect refused")

    async def subscribe(self, topic: str):
        self._ids = self._ids % 65535 + 1
        self._w.write(_packet(0x82, struct.pack("!H", self._ids) + _str(topic) + b"\x00"))
        await self._w.drain()

    async def publish(self, topic: str, payload: bytes):
        self._w.write(_packet(0x30, _str(topic) + payload))
        await self._w.drain()

    async def ping(self):
        self._w.write(b"\xc0\x00")
        await self._w.drain()

    async def _read(self) -> tuple[int, bytes]:
        head = await self._r.readexactly(1)
        n, mult = 0, 1
        while True:
            b = (await self._r.readexactly(1))[0]
            n += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return head[0], await self._r.readexactly(n) if n else b""

    async def messages(self):
        while True:
            kind, data = await self._read()
            if kind >> 4 != 3:
                continue
            tlen = struct.unpack("!H", data[:2])[0]
            topic = data[2:2 + tlen].decode(errors="replace")
            start = 2 + tlen + (2 if (kind >> 1) & 3 else 0)
            yield topic, data[start:]

    async def close(self):
        if self._w:
            try:
                self._w.write(b"\xe0\x00")
                self._w.close()
                await asyncio.wait_for(self._w.wait_closed(), 2)
            except Exception:
                pass
            self._w = None
