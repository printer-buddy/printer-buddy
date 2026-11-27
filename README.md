<p align="center"><img src="assets/logo.png" alt="Printer Buddy" width="220"></p>

# Printer Buddy (Lite, remote mode)

**Website:** [printerbuddy.ca](https://printerbuddy.ca) · **Docs:** [docs.printerbuddy.ca](https://docs.printerbuddy.ca)

A self-hosted printer monitor for Klipper printers. Run it on a Pi, a NAS, a home server or on a printer itself,
and pick which parts to install and how heavy each one is. It finds Moonraker printers on the network, you choose
which ones to monitor, and it subscribes to only those over Moonraker's WebSocket API.

## Install (Pi / any Debian box on the same network as your printers)

```bash
git clone https://github.com/printer-buddy/printer-buddy.git
cd printer-buddy
./install.sh            # makes a venv, installs deps, sets up the printer-buddy systemd service
```

Then open `http://<box-ip>:8080`. Settings (notifications, subnets) live in `printer-buddy.env`.

**Port:** Printer Buddy uses 8080. If something else already has it (often the webcam streamer on
Klipper boxes), the installer says what's using it and offers the next free port. Change it later
with `./scripts/port.sh 8081` (the port must be free).

## Update

```bash
cd printer-buddy && ./update.sh     # updates Printer Buddy and its parts, then restarts it
```

Logs: `journalctl -u printer-buddy -f`.
Docker is still supported if you want it: `docker compose -f docker/docker-compose.yml up -d --build`.
**Docker is broken right now:** it doesn't get a box ID, so share links from outside your network and AI photo sharing don't work.

## AI failure detection

Printer Buddy's own model scores camera snapshots for spaghetti, warping, first-layer problems,
stringing, blobs and cracks. See **[How the AI was made](https://docs.printerbuddy.ca/developers/ai-model/)** for how it works, test results, limits and
dataset credits. Models: [releases tagged `ai-`](https://github.com/printer-buddy/printer-buddy/releases?q=ai-&expanded=true).

## How it works

1. **Scan network** sweeps the box's /24 on ports 7125 and 80 and listens for Moonraker's mDNS
   (`_moonraker._tcp`). Each hit is confirmed with `/server/info`. **It only lists printers; it doesn't connect to any.**
2. You **tick the printers you want** (adding an API key if Moonraker has auth turned on) and click *Add selected*.
   Printers the scan missed can be added with *Add by IP*.
3. For each printer you picked, the box keeps one WebSocket open to `/websocket`, calls `server.connection.identify`,
   then `printer.objects.subscribe` (print_stats, extruder, heater_bed, display_status, virtual_sdcard, webhooks).
   It reconnects with backoff and re-subscribes on `notify_klippy_ready`.
4. The dashboard updates live via Server-Sent Events. Webcam snapshots come from `server.webcams.list`
   and are proxied through the box.
5. Print started, paused, resumed, finished, failed and cancelled events go to ntfy and Discord, and with advanced notifications also to Telegram, Pushover and email.

## Config (env vars)

| Var | Default | |
|---|---|---|
| `DB_PATH` | `data/monitor.db` | SQLite file holding the printers you picked |
| `SCAN_SUBNETS` | auto (/24 of this box) | comma list, e.g. `192.168.1.0/24,192.168.2.0/24` |
| `PROBE_PORTS` | `7125,80` | ports tried during the sweep |
| `PB_ALLOWED_HOSTS` | — | extra names you open Printer Buddy by, e.g. `printers.home`. IPs, `localhost`, `*.local` and this box's hostname always work; other names are refused |
| `NTFY_URL` / `NTFY_TOKEN` | — | e.g. `https://ntfy.sh/my-printers` |
| `DISCORD_WEBHOOK` | — | Discord webhook URL |
| `NOTIFY_EVENTS` | all | `started,paused,resumed,finished,failed,cancelled` |

## Moonraker side

Nothing needs adding to Moonraker to monitor a printer. If Moonraker restricts access, either add this box's IP to
`[authorization] trusted_clients` in `moonraker.conf` or paste an API key when adding the printer.
For mDNS discovery, enable `[zeroconf]` in `moonraker.conf`; the subnet sweep works either way.

## Layout

```
app/main.py              HTTP API + SSE
app/core/                SQLite store, stdlib async HTTP + WebSocket client, ntfy / Discord alerts
app/printers/            Moonraker client, printer manager (events, fan-out), mDNS + subnet discovery
app/ai/                  failure detection, photo collection, model downloads
app/media/               timelapse recording
ui/                      web UI (downloaded by the installer, served at /static/): core/ builder, panels/ one folder per panel
docs/                    AI.md
docker/                  Dockerfile + docker-compose.yml
```
Each folder has a README.txt saying what's in it.

## API

`POST /api/scan` · `POST /api/probe {host,port?,api_key?}` · `GET/POST /api/printers` ·
`PATCH/DELETE /api/printers/{id}` · `GET /api/printers/{id}/snapshot` · `GET /api/events` (SSE) ·
`POST /api/notify/test`

## Licenses

### Printer Buddy

Copyright (C) 2026 sterling motuz. Licensed under the GNU Affero General Public License v3.0 only (AGPL-3.0-only), with extra terms: keep the licence header in each file, and include a CHANGELOG of any changes. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The Printer Buddy name and logo are not covered by the licence.

### Bundled libraries (`ui/vendor/`)

- [Apache ECharts](https://echarts.apache.org) - Apache-2.0, © The Apache Software Foundation
- [ECharts GL](https://github.com/ecomfe/echarts-gl) - BSD-3-Clause, © 2017 Baidu Inc.
- [three.js](https://threejs.org) - MIT, © 2010-2022 three.js authors

### Python packages

- [Starlette](https://www.starlette.io) - BSD-3-Clause
- [Uvicorn](https://www.uvicorn.org) - BSD-3-Clause
- [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) - LGPL-2.1
- [ONNX Runtime](https://github.com/microsoft/onnxruntime) - MIT
- [NumPy](https://numpy.org) - BSD-3-Clause
- [Pillow](https://python-pillow.org) - MIT-CMU
