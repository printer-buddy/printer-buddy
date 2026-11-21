# Changelog

All changes to Printer Buddy, newest first.

################################2026-11-21 sterling m mmu/loaded################################

## MMU: detect the loaded filament on AFC Lite and H2-series Bambu

The "Loaded" line and active slot now work on two setups that showed "Loaded: Nothing" even mid-print: paxx12's AFC-Lite stub (Snapmaker U1) and Bambu H2-series printers (H2C/H2D/H2S/H3C). *Why:* AFC Lite hardcodes `current_load` to None and reports the active lane in `current_lane`; and the H2-series moved the loaded tray out of `ams.tray_now` into the per-nozzle `device.extruder.info[].snow` (unit `snow>>8`, slot `snow&0xFF`; active nozzle `(extruder.state>>4)&0xF`).

Lines changed:
- `app/printers/moonraker.py` — 860 `afc_view` falls back to `current_lane` when `current_load` is empty
- `bambu-convert/bambu_convert/printer.py` — 354–369 add `_active_tray` (H2: active tray from the active nozzle's `snow`, else `ams.tray_now`); 376, 506 read the loaded tray through it

################################end of entry################################

################################2026-11-21 sterling m cameras/webrtc-cors################################

## WebRTC: play it live by proxying the handshake through Printer Buddy

A WebRTC camera now connects instead of dropping to snapshots: the signalling runs browser → Printer Buddy → camera (same-origin), because the camera sends no CORS header and Printer Buddy runs on a different host than the printer. The video still flows browser ↔ camera directly on the LAN. *Why:* the browser blocked the cross-origin offer (`Access-Control-Allow-Origin` missing, preflight 501), so the handshake never completed.

Lines changed:
- `app/core/net.py` — 120–124 add `http_request` (forward any method)
- `app/main.py` — 28 import `urlencode`/`urljoin`/`urlsplit`; 33–34 import `WebSocketRoute`/`WebSocket`; 39 import `http_request` and the client `WebSocket`; 373–381 `_webrtc_base` resolves the camera's signalling URL server-side; 383–417 `webrtc_signal` proxies camera-streamer POST + MediaMTX WHEP and rewrites the WHEP session `Location`; 419–463 `webrtc_ws` proxies the go2rtc signalling WebSocket; 1837–1839 register the three routes

################################end of entry################################

################################2026-11-21 sterling m cameras################################

## Live video for every camera type over a share link

RTSP, HLS and snapshot cameras now go live over a share link, not just MJPEG: Printer Buddy carries its own go2rtc and a static ffmpeg (fetched into `data/streamer-bin` on first use, no root) and converts them to MJPEG it can relay. *Why:* only MJPEG could cross the tunnel before, and the converter was tied to the "advanced" streamer tier.

Lines changed:
- `app/core/camera.py` — 1–172 new: a go2rtc supervisor that downloads go2rtc + a static ffmpeg, runs (or reuses) go2rtc on 127.0.0.1:1984, and exposes `available`/`ensure`/`stream_name`/`mjpeg_url`/`frame_url`
- `app/core/net.py` — 115–119 add `http_put` (go2rtc's stream API needs PUT)
- `app/printers/moonraker.py` — 618 `_snap_cams` also keeps stream-only cameras; 642–643 `snapshot_url` goes through `_abs`; 646, 648 `_abs` allows `rtsp://`; 660–664, 667–671 `cam_mode` classifies mjpeg/hls/convert/snapshot; 680–685 `source_url`; 687–691 `cam_mode_at`; 700, 703 `cams_info` reports `mode`/`live`; 768 `has_webcam` from `_snap_cams`
- `app/main.py` — 45 import the go2rtc helper; 296–297, 335–336 take the client once; 313–321 snapshot falls back to go2rtc `frame.jpeg` for convert/hls; 343–352 `live_stream` converts convert/hls to MJPEG via go2rtc; 1652 stop go2rtc on shutdown

## Snapmaker U1 stock-firmware camera

A stock U1 lists no webcam; Printer Buddy now shows one, serving `/server/files/camera/monitor.jpg` and keeping it fresh with a `camera.start_monitor` websocket call while something is viewing it. *Why:* stock firmware exposes no MJPEG/RTSP, only that still, which goes stale without a periodic wake.

Lines changed:
- `app/printers/moonraker.py` — 117 add `_cam_woke`; 619–623 inject a "Camera" at `monitor.jpg` when a U1 (`has_materials`) lists none; 626–636 `wake_camera` sends `camera.start_monitor` over the websocket, capped to once per 30s
- `app/main.py` — 301–302 wake before fetching a snapshot
- `app/printers/manager.py` — 343–344 wake before a notify/AI snapshot

## Live WebRTC flag for the UI

A WebRTC camera is marked so the web UI can play it live on the LAN. *Why:* the UI needs the camera's service and source URL to run the handshake (the player itself is in the web UI repo).

Lines changed:
- `app/printers/moonraker.py` — 665–666 `cam_mode` adds the `webrtc` case; 704 `cams_info` adds `service` and `src`

################################end of entry################################

################################2026-11-20 sterling m security/streamer/ai################################

## Security: origin/host guard, mask notify secrets, keep share chat/flags on the box

Stops other websites from driving the API through your browser, stops saved secrets going to the browser, keeps share chat and flags on the box, locks down on-disk secrets, and closes a chat-spam bypass. *Why:* a page you opened elsewhere could send commands or read your settings; the notify page returned your SMTP password and tokens; a viewer's chat/flag text (e.g. `@everyone`) reached your Discord; `printer-buddy.env`/`data/` were world-readable; and the chat rate limit keyed on a browser-chosen id.

Lines changed:
- `app/main.py` — 16, 24 add `ipaddress`/`socket`; 948 (×33) remove `chat_alert`/`CHAT_ALERTS`; 974, 990 remove its calls; 996 (×4) remove the `alert` setting; 1001 change `chat_settings` return; 1522–1525 add `NOTIFY_SECRETS`/`HIDDEN`; 1527–1530 change `get_notify_settings` to mask; 1536–1538 add the save-time skip; 1652–1706 add `SiteGuard`/`_host_name`; 1786 change app to `TunnelGuard(SiteGuard(_app))`. (44, 77, 789–795, 805–814, 1628, 1630 are the streamer hooks below.)
- `app/core/chat.py` — 187 (×6) remove old order; 193–197 add the per-printer cap before the block check in `post`; 213–218 same in `flag`
- `printer-buddy.env.example` — 15–18 add the `PB_ALLOWED_HOSTS` note
- `README.md` — 58 add the `PB_ALLOWED_HOSTS` row

## Streamer: run under Printer Buddy, replace root helper with restart.sh

The camera streamer now runs as a Printer Buddy subprocess instead of a root systemd service, and the root helper is a tiny `restart.sh`. *Why:* the camera no longer needs a root service, and what runs as root is now three fixed commands that never need updating.

Lines changed:
- `app/core/streamer.py` — 1–104 (new) the streamer supervisor
- `tools/restart.sh` — 1–19 (new) the only root script: restart / reboot / shutdown
- `tools/root.sh` — removed (76 lines)
- `app/main.py` — 44, 77 add the streamer import and instance; 789–795 change `system_list` to show it; 805–814 add start/stop/restart; 1628, 1630 add start/stop in the lifespan
- `app/core/updater.py` — 21, 43 change the note and `HELPERS`; 213 (×8) remove `root()`; 247–294 add `fetch_stream_bins`; 297 (×2), 309 (×4) remove the old root/asvc lines; 307, 313 (×1), 315–316 change `ensure_streamer`/`ensure_packages` off root
- `app/core/components.py` — 222–224 trim `SERVICES`/`SYSTEM_JOBS` to reboot/shutdown
- `install.sh` — 25 `HELPER`→restart.sh; 74 chmod 600 env; 265–266 install restart.sh + remove root.sh; 276–296 add `camera_setup`; 323 chmod 700 data; 330, 596 run `camera_setup`; 345 add it to update; 371–372 uninstall cleanup; 380 chmod 700 backups

## AI: scene baseline, early-layer rules, heavy tiling, auto-pause after repeat high checks

Each failure is scored against what the scene scored at the start of the print, early-layer rules decide which failures count when, heavy mode scores overlapping tiles, and an alert only auto-pauses if it keeps showing. *Why:* it alerted on things always in view (lights, reflections), paused on single false spikes, and scored the whole picture so a small print read weakly.

Lines changed:
- `app/ai/detector.py` — 36–41 add the thresholds; 159–160 add `TILES`; 224 add layer tracking; 267 add `pause_pending`; 272 record the layer; 291 clear the pending pause on pause/stop; 306–340 add `_score` and `_from_start`; 416–438 score through them; 443, 445, 448 (×4), 450–451 rework the alert decision; 457–471 add the 3-of-5 auto-pause; 479–484 add early-layer gating
- `app/ai/collect.py` — 29 remove "crack" from the labels; 138–139 clearer too-big-photo error
- `app/printers/manager.py` — 263–265 add an `again` flag for the post-alert pause; 277 don't save that photo twice

################################end of entry################################