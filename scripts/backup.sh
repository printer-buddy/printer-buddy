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
#   ./scripts/backup.sh            back up your printers and settings (data/ and printer-buddy.env) into backups/
#   ./scripts/backup.sh restore    pick a backup to restore
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
case "${1:-}" in
  "") make_backup ;;
  restore) restore_backup ;;
  *) echo "Usage: ./scripts/backup.sh [restore]"; exit 1 ;;
esac
