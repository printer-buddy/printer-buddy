# Changelog

All changes to Printer Buddy, newest first.

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