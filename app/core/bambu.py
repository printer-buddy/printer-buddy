from __future__ import annotations

import asyncio
import json


class _Reply(asyncio.DatagramProtocol):
    def __init__(self, fut: asyncio.Future):
        self.fut = fut

    def datagram_received(self, data, addr):
        if not self.fut.done():
            self.fut.set_result(data)


async def discover_elegoo(host: str, port: int = 3000) -> dict | None:
    """Ask a Centauri Carbon at this address who it is (UDP "M99999"); its MainboardID is how it is identified."""
    loop = asyncio.get_running_loop()
    for _ in range(3):
        fut = loop.create_future()
        try:
            transport, _ = await loop.create_datagram_endpoint(lambda: _Reply(fut), remote_addr=(host, port))
        except OSError:
            return None
        try:
            transport.sendto(b"M99999")
            data = json.loads(await asyncio.wait_for(fut, 2)).get("Data") or {}
            if data.get("MainboardID"):
                return data
        except (asyncio.TimeoutError, ValueError, AttributeError):
            pass
        finally:
            transport.close()
    return None
