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
set -euo pipefail
case "${1:-}" in
  restart)  systemctl restart printer-buddy ;;
  reboot)   systemctl reboot ;;
  shutdown) systemctl poweroff ;;
  *) echo "usage: $0 restart | reboot | shutdown"; exit 2 ;;
esac
