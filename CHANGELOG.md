# Changelog

All changes to Printer Buddy, newest first.

################################2026-11-27 sterling m layout/requirements-and-helper################################

## Requirements files in one folder, the root helper next to the scripts

The four requirements files are now `requirements/core.txt`, `requirements/ai.txt`, `requirements/ai-lite.txt` and `requirements/ai-gpu.txt`, and the root helper's source moved from `tools/restart.sh` to `scripts/restart.sh` (`tools/` is gone). The installed helper is still `/usr/local/lib/printer-buddy/restart.sh`. *Why:* fewer loose files in the Printer Buddy folder.

Lines changed:
- `requirements.txt`, `requirements-ai.txt`, `requirements-ai-lite.txt`, `requirements-ai-gpu.txt` — moved to `requirements/`
- `tools/restart.sh` — moved to `scripts/restart.sh`
- `app/core/updater.py` — 252, 274–275 install from `requirements/`
- `scripts/common.sh` — 262 copies the helper from `scripts/`; 322 installs `requirements/core.txt`
- `docker/Dockerfile` — 3 copies `requirements/core.txt`

################################end of entry################################

################################2026-11-27 sterling m docs/ai-page################################

## The AI write-up moved to the docs site

`docs/AI.md` is gone. Its model details, limits, how it was made and every credit (datasets, ideas, software) are now the docs site's [How the AI was made](https://docs.printerbuddy.ca/developers/ai-model/) page, and what's sent with Help improve the AI is on its AI failure detection page. The README links there. *Why:* the docs site is where the rest of Printer Buddy's documentation lives, and `docs/AI.md` had fallen behind it (only the basic model, old menu names).

Lines changed:
- `docs/AI.md` — removed
- `README.md` — 38 links to the docs site page

################################end of entry################################

################################2026-11-27 sterling m installer/split-scripts################################

## The installer menu is split into one script per job

`./install.sh` no longer opens a menu. On a new box it still walks through the setup questions and installs; after that it reinstalls with the current setup, and `./install.sh --setup` reopens the setup questions and applies the changes. Its flags (`-y`, `--port`, `--ui`, `--ai`, `--video`, `--notify`, `--remote`, `--stream`, `--channel`, `--parts`, `--streamer`) work as before. `./update.sh` is now the full update the menu's Update did: the code, its parts, the root helper, camera access, the service file and a restart. The menu's other choices are their own scripts in the `scripts` folder: `./scripts/uninstall.sh`, `./scripts/backup.sh` and `./scripts/backup.sh restore`, `./scripts/port.sh [number]`, `./scripts/remote.sh on|off` and `./scripts/printer-streamer.sh light|basic|advanced`. The installer's shared code is `scripts/common.sh`. Messages that pointed at menu items now name the script to run, and "Press Enter to go back" now says "Press Enter to continue". *Why:* one script per job is quicker to run and easier to follow than a menu.

Lines changed:
- `install.sh` — whole file: 13–17 usage; 19 loads `scripts/common.sh`; 21–28 setup questions on a first run or with `--setup`, otherwise a reinstall; 30–57 the flags, as before (34 adds `--setup` to the usage)
- `scripts/common.sh` — new: the installer's settings and functions (from `install.sh` 19–571 and 605–732, without the menu and the backup menu); 65 "continue"; 160–176 port messages, and `change_port` takes a port number; 179, 181, 548 remote messages; 603, 613, 629 setup-question wording
- `update.sh` — whole file: runs the full update (15 loads `scripts/common.sh`)
- `scripts/uninstall.sh`, `scripts/backup.sh`, `scripts/port.sh`, `scripts/remote.sh`, `scripts/printer-streamer.sh` — new
- `app/core/components.py` — 248, 254 say to run `./update.sh`
- `app/core/tunnel.py` — 155, `app/main.py` — 1036, 1038 say to run `./scripts/remote.sh on`
- `README.md` — 23 `./scripts/port.sh`; 28 what `./update.sh` does

################################end of entry################################

################################2026-11-26 sterling m themes/list################################

## Themes from the theme list replace copying a Fluidd or Mainsail theme

`GET /api/ui/themes` lists the themes in the `printer-buddy/printer-buddy-themes` repo (read from GitHub, kept for an hour), and `POST /api/ui/theme/pick {"id"}` downloads one and saves it as the box's theme for every device: dark and light colours, a text and a monospace font, background, logo and side-bar pictures, and a `theme.css`. Colours and font names are checked, file names may only use letters, numbers, `.`, `_` and `-`, pictures and fonts are up to 15 MB each (20 files at most), and every `url()` in `theme.css` must point at the theme's own pictures or fonts; a `theme.css` with `@import`, `image-set()`, `expression()`, `javascript:`, bindings or backslashes is refused. A theme that fails to download or check leaves the current theme as it was. `PB_THEMES_URL` in `printer-buddy.env` points the box at another copy of the list, to try a theme before submitting it. The Fluidd / Mainsail copy (`/api/printers/<id>/theme`) is removed; a theme copied before keeps showing until another is picked. *Why:* anyone can now add a theme to the list with a pull request, and a theme can restyle all of Printer Buddy instead of the few parts a Fluidd or Mainsail theme had that fit.

Lines changed:
- `app/main.py` — 512–635 replace the Fluidd / Mainsail copy with the theme list (`ui_themes`, `_theme_css`, `ui_theme_pick`); 1882–1883 add the two routes and remove `/api/printers/<id>/theme`
- `app/main.py`, review fixes — 517 `url()` is read by its own quote style; 549–551 a list entry whose `swatch` isn't a list gets no swatch instead of failing the whole list; 566–567 a `theme.css` with any `url(` that can't be read as a plain file name is refused; 652–653 theme files are sent with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` and `X-Content-Type-Options: nosniff`, so an SVG opened on its own can't run script (pictures, fonts and `theme.css` used by the page are unaffected)

################################end of entry################################

################################2026-11-26 sterling m security/update-output################################

## Updating shows every step and what it found

An update now says what it's doing and what it found at each step: the new changes it pulled (up to 10, then how many more) or that it was already up to date; the restart helper and sudo rule (with a note that sudo may ask for a password, and without visudo's "parsed OK"); camera access; the old share tunnel service, old camera streamer files, old go2rtc and old system packages (none, moved, removed, or kept and why); the box ID; each part (updated, or already up to date); the Python libraries; the camera streamer; the service file; the restart; and where Printer Buddy is running. *Why:* most steps ran without a word, so a long update looked stuck and the sudo password prompt came with no reason.

Lines changed:
- `app/core/updater.py` — 118, 125–134 list the new changes after pulling, or say it's up to date; 183, 203–204 say when a part is already up to date (not a second time for the streamer, 386); 227, 232 a "Parts:" heading and "updated to"; 252–254 the Python libraries; 397 the camera streamer is ready
- `install.sh` — 212 "Restarting Printer Buddy..."; 246 the service file; 260, 266, 269 the restart helper and sudo rule; 283–288 camera access; 340 where Printer Buddy is running; 348 the box ID; 419–425, 432–434, 440–442, 451–455, 457–472 the old share tunnel service, camera streamer files, go2rtc and system packages

################################end of entry################################

################################2026-11-25 sterling m security/box-key################################

## A new box makes its own key before asking for its box ID

When the installer asks the relay for a new box ID, it now makes the box key itself, saves it as `PB_BOX_KEY` first and sends it (`{"key": "..."}`). Asking again, after a timeout or on the next install run, sends the same key and gets the same ID. A box whose old ID was refused makes a new key the same way. *Why:* the relay can be slow to answer ("busy, try again"); before, every new try made a new ID the box never got, and used up the network's 5 new IDs a day.

Lines changed:
- `install.sh` — 483–489 a new box makes and saves its key, then sends it; 495–496 the same when its old ID is refused; 508 add `new_box_key`

################################end of entry################################

################################2026-11-24 sterling m security/own-copies################################

## Printer Buddy keeps its own ffmpeg, ustreamer and go2rtc; no more apt installs

Printer Buddy no longer installs system packages, and checks every download before using it: files from GitHub (go2rtc, cloudflared, mjpg-streamer and Printer Buddy's own parts) against the sha256 GitHub lists for them, ffmpeg against the .md5 its site publishes; a download that doesn't match is refused. A download is written to a temporary file and only put in place once complete, so a failed one never leaves a broken program behind. ffmpeg (timelapses, converted cameras, the Bambu X1/H2 camera) and go2rtc are always Printer Buddy's own copies in `data/streamer-bin`, downloaded on first use, with the system ones only as a fallback if the download fails. ustreamer (basic and advanced streamer) has no ready-made download, so Printer Buddy takes the box's own distro package with `apt-get download` (no root), and unpacks the program into `data/streamer-bin` and any libraries the box lacks into `data/streamer-bin/lib`; a retry after a failed library download replaces what the first try copied. A new install makes its Python environment without the python3-venv package when that's missing. On the next installer run from the menu, the installer asks before removing each of ffmpeg, ustreamer, v4l-utils and python3-venv, and only offers it when Printer Buddy's own install command for it is in apt's history and nothing else installed depends on it. It asks before removing the old `/usr/local/bin/go2rtc` too, and only offers it when the box uses the advanced streamer (the only reason Printer Buddy ever put it there), no package owns it, no service file mentions it and nothing is running it. The Bambu camera's ffmpeg install also works again: it called `updater.root`, which no longer exists. *Why:* those packages were installed as root and Printer Buddy couldn't control or remove them, and a damaged or swapped download would have been run as-is.

Lines changed:
- `app/core/camera.py` — 15, 23, 28 imports; 59–68 add `_publish` (temporary file, then swapped into place); 71–84 `_ensure_go2rtc` uses Printer Buddy's own copy first, the system one only as a fallback, and checks the download's sha256; 88–111 same for `_ensure_ffmpeg`, checked against its .md5
- `app/main.py` — 68 put `data/streamer-bin` first on `PATH`, so timelapses and the Bambu camera find Printer Buddy's own ffmpeg; 1774–1775 fetch ffmpeg at start when timelapses are on
- `app/printers/native.py` — 22 drop the unused `shutil` import; 43, 46–48 get ffmpeg through `_ensure_ffmpeg` for the Bambu camera
- `app/core/updater.py` — 26 import `hashlib`; 59–75 add `check_sha256` and `github_asset`; 195–196 check each part's sha256; 335, 357 mjpg-streamer and go2rtc through `github_asset`; 274–323 add `_deb_extract`, `_installed` and `fetch_ustreamer` (317 replaces a library copied by an earlier try); 346–350 the basic and advanced tiers fetch ustreamer, go2rtc is always Printer Buddy's own; 385 (×2) drop the "install ffmpeg" message
- `app/core/streamer.py` — 61–62 give the streamer `data/streamer-bin/lib`; 65–67 fetch ffmpeg for the advanced tier
- `install.sh` — 246 (×6) remove `ensure_ffmpeg`; 272 (×10) `camera_setup` no longer installs packages; 301–311 make the venv without python3-venv when it's missing; 423–430 `legacy_cleanup` asks before removing the old `/usr/local/bin/go2rtc` (427–428); 431–441 and before removing each package Printer Buddy installed (439); 627 drop the `ensure_ffmpeg` call from the setup menu

################################end of entry################################

################################2026-11-25 sterling m security/tunnel################################

## Box ID and key from the relay; share tunnel runs under Printer Buddy, only while a share link is active

Printer Buddy now runs the share tunnel itself: it downloads its own `cloudflared` into `data/streamer-bin` and starts it as its own process, with no root, only while a share link is active. After 3 days with no active share link it stops the tunnel and tells the relay to hold its ID; the next share link gets the same ID and address back. The installer gets the box's ID and key from the relay at install (and on the next update for boxes installed before this; a box keeps its own ID, sending its tunnel key if it has one) and saves them as `PB_USER_ID` and `PB_BOX_KEY`. If the relay won't give back the box's old ID (another box has it, or its key is lost), the installer gets a new one. Printer Buddy no longer makes up an ID itself, and photos shared to improve the AI are sent with the key. When the tunnel can't run, the reason (from the relay, that Remote access isn't finished, or that cloudflared won't start) is passed to the Share window, and it waits 10 minutes before trying again. The tunnel reads Remote access and the box ID the same way the app does, so quotes or upper case in `printer-buddy.env` don't stop it. A share link from outside uses only the box ID the relay registered; until there is one, making a share link says Remote access isn't finished instead of giving a link that won't work. The installer no longer installs cloudflared or a root `cloudflared` service, and the register code prompt is gone: turning Remote access on just registers the box's tunnel with its key. On the next installer run, the old root `cloudflared` service is removed, but only if its token is this box's own tunnel token (checked against the relay with the box's key), and the old root camera streamer files in `/usr/local/lib/print-buddy-streamer` are deleted. *Why:* the share tunnel was a root service Printer Buddy couldn't control or remove, every box held a relay slot forever, and anyone could share tunnels or photos under any made-up ID.

Lines changed:
- `app/core/tunnel.py` — 1–218 new: the tunnel supervisor (downloads cloudflared and checks its sha256, gets the token from the relay, runs and restarts it, releases it after 3 days without a share link, keeps the reason when it can't run)
- `app/core/store.py` — 135–138 add `active_share_count`
- `app/main.py` — 46 import `Tunnel`; 82 create it; 1008 `share_user_id` no longer makes up an ID; 1011–1028 add `registered_id` and `share_domain`, outside share links use only the registered ID; 1047–1048 making a share link fails while Remote access isn't finished; 1059 start the tunnel straight away when a share link is made; 1068 the share list says why the tunnel can't run; 1773, 1777 start and stop it with the app
- `app/ai/collect.py` — 293 send the box key with each photo
- `README.md` — 33 note that Docker is broken: it gets no box ID
- `install.sh` — 316 get the box ID on install; 320 run `legacy_cleanup` on install; 336 and on update; 337 get the box ID on update; 395–422 `tunnel_state` follows Printer Buddy's tunnel, add `root_tunnel_token` and `legacy_cleanup` (root cloudflared service, old streamer files); 443–471 `user_id` gets the ID and key from the relay (a new one if the old ID is refused), add `box_api`; 469 (×21) remove `install_cloudflared` and `start_tunnel`; 477–478 send the box key; 488–494, 496 `auto_tunnel` registers the tunnel with the box key, no register code; 500, 502 `remove_tunnel` stops Printer Buddy's tunnel instead of the root service

################################end of entry################################

################################2026-11-24 sterling m ai/photos################################

## AI photos: only kept while sharing is on, with a set number to keep

No AI photos are saved while "Send my labelled photos to the Printer Buddy project" is off: no print snapshots (10/50/90% or failed), no alert photos and no "Save for AI…" (which now says to turn sharing on). While it's on, the newest `ai_keep` photos are kept (a new AI setting, a whole number from 10 to 500, default 500) instead of a fixed 500. Only records that still hold a photo count toward `ai_keep`, so records already sent (photo deleted after upload) can't push out unsent photos; sent records are capped separately at the newest `ai_keep`, trimmed after each photo is sent. Saving a new `ai_keep` trims to it straight away, even with sharing off; otherwise photos saved before are left until they're deleted. *Why:* photos piled up on the server even with sharing off, when nothing used them.

Lines changed:
- `app/ai/collect.py` — 147–149 `add()` saves nothing while sharing is off and says why; 164, 169–175 public `trim()` keeps the newest `ai_keep` records with a photo and, separately, the newest `ai_keep` records without one (falls back to 500); 217 `wants_print_snapshot()` takes no print snapshots while sharing is off; 304 trim after each photo is sent
- `app/ai/detector.py` — 26 add `ai_keep: 500` to the AI settings defaults
- `app/main.py` — 485–488 `ai_settings` accepts `ai_keep` as a whole number from 10 to 500; 490–491 trim the AI photos when `ai_keep` is saved

################################end of entry################################

################################2026-11-24 sterling m mmu/lane-actions################################

## MMU: pass through the lane and gate state the per-lane actions need

The MMU view now includes, for AFC lanes, the separate `load` and `prep` flags, the mapped tool (`map`) and the runout lane (`runout`); for Happy Hare, `filament_pos` and each unit's `crossload` flag. *Why:* the web UI's new per-lane menu greys actions out the way Fluidd and Mainsail do, which needs these fields.

Lines changed:
- `app/printers/moonraker.py` — 50 subscribe to `filament_pos` in `MMU_FIELDS`; 844 add each unit's `crossload`; 846 add `filament_pos` to the Happy Hare view; 873–874 add `load`, `prep`, `map` and `runout` to each AFC lane

################################end of entry################################

################################2026-11-24 sterling m docs/readme-website################################

## README: link the website and docs

The README now links the new website and the docs site under its title. *Why:* neither printerbuddy.ca nor docs.printerbuddy.ca was linked from the repo.

Lines changed:
- `README.md` — 5 add the Website and Docs links

################################end of entry################################

################################2026-11-23 sterling m cameras/warm-snapshot################################

## Faster snapshots for RTSP/HLS cameras on the overview

A snapshot from a convert/HLS camera (one with no direct snapshot URL) now comes from the already-running go2rtc stream instead of cold-connecting to the camera for every frame, so the multi-printer overview's thumbnails update quickly instead of choppily. *Why:* `frame.jpeg` was fetched against the raw source each poll, restarting the pull every time (seconds per frame); pulling from the warm stream returns the latest decoded frame at once.

Lines changed:
- `app/main.py` — 315–318 ensure the go2rtc stream for the camera and pull the frame from it (falls back to the raw source if the stream can't be started)

################################end of entry################################

################################2026-11-23 sterling m mmu/afc-lane-extruder################################

## AFC: report each lane's extruder and tool-loaded state

The AFC filament view now passes through each lane's `extruder` (which nozzle the lane feeds) and `tool_loaded` flag, so the web UI can tell which filament is loaded into which nozzle on a multi-nozzle toolchanger (e.g. to colour that nozzle's flame). *Why:* the view only reported one overall active slot, so per-nozzle state was invisible.

Lines changed:
- `app/printers/moonraker.py` — 870 add `extruder` and `tool_loaded` (from the AFC lane status) to each `afc_view` gate
################################2026-11-21 sterling m mmu/grams################################

## MMU: report the grams left per AFC lane

The AFC filament view now includes each lane's weight in grams, so the slot card can show grams next to the percent. *Why:* the UI only had a percent; the lane's `weight` was already read, just not passed through.

Lines changed:
- `app/printers/moonraker.py` — 870 add `grams` (rounded lane weight) to each AFC gate

################################end of entry################################

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