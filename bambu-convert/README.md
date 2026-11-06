# Bambu Convert

Makes Bambu Lab and Elegoo Centauri Carbon printers look like Klipper / Moonraker printers.

**Printer Buddy uses this code directly, inside its own process.** Add a printer with its **Add a printer by address** form
(pick the **Printer type**). There is no separate server and no extra port, and nothing to run.

Run it by hand (see the end of this page) only to use these printers from Fluidd or Mainsail. That way it serves one Moonraker
port per printer (7130, 7131, ...) from the printers listed in a `bambu.json` file.

## Elegoo Centauri Carbon

Two models, two protocols, both turned into the same Moonraker objects:

- **Centauri Carbon (CC1)** &mdash; SDCP over a WebSocket (port 3030). Only the IP is needed: the bridge asks the printer who it is (UDP port 3000). The camera is `http://<ip>:3031/video`.
- **Centauri Carbon 2 (CC2)** &mdash; MQTT (port 1883, user `elegoo`, password = the access code). Needs the serial number and access code.

What works: status (state, file, layers, progress, temperatures, fans, light), pause, resume, stop, start a print, upload, camera. The CC2 also takes temperatures, fans, light, speed, file list and delete.

On a CC1 with the stock firmware, only pause, resume, stop, start and status are sent, because a command the firmware doesn't know can crash its software and stop a print. Temperatures, fans, light, speed and the file list stay off. If the printer runs OpenCentauri firmware, add `"extra_commands": true` to its entry in `bambu.json` to turn them on.

> **Not tested on a real printer.** This was written from community protocol notes and tested against simulated printers only, not a real Centauri Carbon. The CC2 side has the least documentation.

Entries look like this (`brand` and `model` are set by the Add form):

```json
{"name": "Centauri", "host": "192.168.0.60", "serial": "<MainboardID>", "port": 7131, "brand": "elegoo", "model": "cc1"}
{"name": "CC2", "host": "192.168.0.61", "serial": "<serial>", "access_code": "<code>", "port": 7132, "brand": "elegoo", "model": "cc2"}
```

## Bambu: printer side

- LAN mode on, access code from Settings > Network, serial number from Settings > Device.
- Pause / resume / stop / start / G-code need **LAN only + Developer mode** on current firmware; status works without.
- X1 / H2 cameras: RTSP (needs `ffmpeg`, and LAN liveview on). P1 / A1 cameras: port 6000.

## What is translated (Bambu)

| Moonraker | Bambu |
|---|---|
| `print_stats`, `virtual_sdcard`, `display_status`, `idle_timeout`, `pause_resume` | MQTT report: `gcode_state`, `mc_percent`, `mc_remaining_time`, `layer_num` |
| `extruder` (+ `extruder1` on H2D / H2C / X2D), `heater_bed`, `temperature_sensor Chamber` | `nozzle_temper` or `device.extruder`, `bed_temper`, `chamber_temper` or `device.ctc` (+ targets) |
| `fan`, `fan_generic Aux / Chamber`, `heater_fan Hotend` | `fan_gear`, or `cooling_fan_speed`, `big_fan1_speed`, `big_fan2_speed`, `heatbreak_fan_speed` |
| `output_pin Chamber_light` | `lights_report` / `ledctrl` |
| `printer.print.pause / resume / cancel / start` | MQTT `pause` / `resume` / `stop` / `project_file` (3mf: plate and AMS slots from the file) or `gcode_file` |
| `mmu`, `mmu_machine` (Happy Hare), `temperature_sensor AMS_n` | AMS units and slots: material, colour, remaining, loaded slot, temperature / humidity |
| `printer.gcode.script` | `gcode_line`; `SET_PIN`, `SET_FAN_SPEED`, `SET_HEATER_TEMPERATURE`, `M220`, `PAUSE`, `RESUME`, `CANCEL_PRINT` are mapped; `MMU_CHANGE_TOOL GATE=n` / `Tn` and `MMU_EJECT` load / unload an AMS slot (`ams_change_filament`) |
| `server.files.*`, upload / download | FTPS (port 990), metadata + thumbnails from the 3mf |
| `server.webcams.list`, `/webcam/?action=stream|snapshot` | camera as MJPEG |
| `server.history.*`, `server.database.*`, `server.temperature_store`, `server.gcode_store` | kept by the bridge in `data/bambu/<serial>/` |

Updates, services, power devices, Spoolman, timelapse and `printer.cfg` don't exist on a Bambu printer and answer empty.

When a 3mf is started, each of its filaments goes to the AMS slot with the same material and the closest colour
(unused filaments get -1).

## Run by hand

```bash
cd bambu-convert
../.venv/bin/python -m bambu_convert -c ../data/bambu.json
```

See `bambu.json.example`.
