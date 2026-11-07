#!/bin/sh
# Printer Buddy AI for a Klipper printer. Run on the printer:  sh install.sh
# Installs into /usr/data/printer-buddy-ai (or $PB_AI_DIR) and starts at boot.
set -e
DIR="${PB_AI_DIR:-/usr/data/printer-buddy-ai}"
BRANCH="${PB_AI_BRANCH:-nightly}"
RAW="https://raw.githubusercontent.com/printer-buddy/printer-buddy/$BRANCH"
REL="https://github.com/printer-buddy/printer-buddy/releases/download/ai-lite-v1.5"

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$2" "$1"
  else wget -q -O "$2" "$1"; fi
}

command -v python3 >/dev/null 2>&1 || { echo "python3 is needed."; exit 1; }
python3 -c "import numpy, PIL" 2>/dev/null || { echo "This needs the Python packages numpy and Pillow (pip3 install numpy Pillow)."; exit 1; }

mkdir -p "$DIR"
echo "Downloading to $DIR ..."
fetch "$RAW/k1-ai/pb_ai.py" "$DIR/pb_ai.py"
fetch "$RAW/app/ai/lite_numpy.py" "$DIR/lite_numpy.py"
fetch "$REL/model.json" "$DIR/model.json"
fetch "$REL/printer-buddy-lite.npz" "$DIR/printer-buddy-lite.npz"
[ -f "$DIR/config.json" ] || cat > "$DIR/config.json" <<'JSON'
{
  "moonraker": "http://127.0.0.1:7125",
  "camera_url": "",
  "interval": 30,
  "threshold": 0.6,
  "checks_in_a_row": 3,
  "pause": false,
  "card": true,
  "card_dir": ""
}
JSON

if [ -d /etc/init.d ]; then
  cat > /etc/init.d/S99printer_buddy_ai <<INIT
#!/bin/sh
case "\$1" in
  start) cd $DIR && nohup python3 pb_ai.py >> $DIR/log.txt 2>&1 & ;;
  stop) pkill -f "$DIR/pb_ai.py" 2>/dev/null; pkill -f "python3 pb_ai.py" 2>/dev/null ;;
  restart) \$0 stop; sleep 1; \$0 start ;;
esac
INIT
  chmod +x /etc/init.d/S99printer_buddy_ai
  /etc/init.d/S99printer_buddy_ai restart
  echo "Started, and it will start at boot. Log: $DIR/log.txt"
else
  echo "Installed. Start it with: cd $DIR && python3 pb_ai.py"
fi
echo
echo "To see the status card in Fluidd or Mainsail, add a camera (Settings > Cameras / Webcams):"
echo "  Name:         Printer Buddy AI"
echo "  Service:      MJPEG Adaptive"
echo "  Snapshot URL: /server/files/config/.printer-buddy-ai/status.svg"
echo "  Target FPS:   1"
echo
echo "Settings: $DIR/config.json  (set \"pause\": true to pause the print on an alert)"
