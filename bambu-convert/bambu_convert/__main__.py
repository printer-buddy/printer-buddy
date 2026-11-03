import argparse
import json
import logging
import socket
import sys
from pathlib import Path

import uvicorn

from .server import build


def main():
    ap = argparse.ArgumentParser(prog="bambu_convert", description="Bambu Lab printers as Moonraker")
    ap.add_argument("-c", "--config", default="bambu.json")
    ap.add_argument("-d", "--data", default=None, help="where history / database / thumbnails are kept")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = json.loads(Path(a.config).read_text())
    printers = [p for p in cfg.get("printers", [])
                if p.get("host") and p.get("serial") and p.get("access_code") and p.get("port")]
    if not printers:
        print("no Bambu printers in", a.config)
        return 0
    socks = []
    for p in printers:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", int(p["port"])))
        socks.append(s)
        logging.info("%s (%s) -> Moonraker on port %s", p.get("name") or p["serial"], p["host"], p["port"])
    data = Path(a.data) if a.data else Path(a.config).resolve().parent / "bambu"
    app = build(printers, data)
    uvicorn.Server(uvicorn.Config(app, log_level="warning", ws_ping_interval=20)).run(sockets=socks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
