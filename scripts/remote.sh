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
#   ./scripts/remote.sh on     share links work from anywhere (sets up the share-link tunnel)
#   ./scripts/remote.sh off    share links only work on your home network (removes the tunnel)
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
case "${1:-}" in
  on|off) ensure_env; set_env PB_REMOTE "$1"; NOASK=1 sync_tunnel ;;
  *) echo "Usage: ./scripts/remote.sh on|off"; exit 1 ;;
esac
