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
#   ./update.sh    update Printer Buddy and its parts, then restart it
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
# all in one function so bash has read it before the update rewrites this file
main() {
  { [ -t 0 ] || [ -r /dev/tty ]; } && INTERACTIVE=1
  ensure_env
  do_update
}
main "$@"; exit $?
