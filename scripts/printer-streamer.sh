#!/usr/bin/env bash
#LICENSE
#
#Copyright (C) 2026  sterling motuz <Sterling@dexabyte.ca>
#
#for https://github.com/printer-buddy/printer-buddy
#
#SPDX-License-Identifier: AGPL-3.0-only
#
#Additional term (AGPL-3.0 section 7b, keep author credit): keep this header
#
#Additional term (AGPL-3.0 section 7c, mark changed versions): if you make any changes, you must include a CHANGELOG with all changes and their dates
#   ./scripts/printer-streamer.sh light|basic|advanced    put Printer Buddy's camera streamer on the printer the camera is plugged into
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
case "${1:-}" in
  light|basic|advanced) printer_streamer "$1" ;;
  *) echo "Usage: ./scripts/printer-streamer.sh light|basic|advanced"; exit 1 ;;
esac
