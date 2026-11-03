# Bambu Convert

Makes Bambu Lab printers look like Klipper / Moonraker printers, so Printer Buddy, Fluidd and Mainsail can use them.
One port per printer (7130, 7131, ...). Printer Buddy runs it while `data/bambu.json` lists printers; add them with
`./install.sh` > Change setup > Bambu printers.

## Printer side

- LAN mode on, access code from Settings > Network, serial number from Settings > Device.
- Pause / resume / stop / start / G-code need **LAN only + Developer mode** on current firmware; status works without.
- X1 / H2 cameras: RTSP (needs `ffmpeg`, and LAN liveview on). P1 / A1 cameras: port 6000.

## What is translated

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
