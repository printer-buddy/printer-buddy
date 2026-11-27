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
#   ./install.sh                         first time: setup questions, then install; after that: reinstall with the current setup
#   ./install.sh --setup                 change the setup and apply it
#   ./install.sh -y                      install/update with no questions (keeps current setup)
#   ./install.sh --ui advanced --ai off  set parts and install with no questions
#   ./install.sh -y --port 8081          install on a set port (must be free)
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"

if { [ $# -eq 0 ] || [ "$*" = --setup ]; } && { [ -t 0 ] || [ -r /dev/tty ]; }; then
  INTERACTIVE=1
  ensure_env
  for part in "${PARTS[@]}"; do set_env "$(cut -d'|' -f2 <<<"$part")" "$(part_value "$part")"; done
  if [ "$*" = --setup ]; then setup_menu; exit 0; fi
  if ! installed && [ "$(get_env PB_GUIDED)" != done ]; then guided_first_run; exit 0; fi
  do_install; exit $?
fi

declare -A GIVEN=()
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) shift ;;
    -h|--help) echo "Usage: ./install.sh [--setup] [-y] [--port N] [--ui basic|advanced] [--ai off|lite|basic|heavy] [--video snapshots|live|timelapse] [--notify basic|advanced] [--remote off|on] [--stream off|light|basic|advanced] [--channel stable|nightly] [--parts] [--streamer]"; exit 0 ;;
    --parts) ensure_env; fetch_parts; exit $? ;;
    --streamer) ensure_env; ensure_streamer; exit $? ;;
    --port)
      [ $# -ge 2 ] || { echo "--port needs a number"; exit 1; }
      valid_port "$2" || { echo "--port must be a number from 1024 to 65535"; exit 1; }
      PORT_GIVEN="$2"; shift 2 ;;
    --ui|--ai|--video|--notify|--remote|--stream|--channel)
      [ $# -ge 2 ] || { echo "$1 needs a value"; exit 1; }
      GIVEN[${1#--}]="${2,,}"; shift 2 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done
ensure_env
for part in "${PARTS[@]}"; do
  IFS='|' read -r key var name choices <<<"$part"
  if [ -n "${GIVEN[$key]:-}" ]; then
    case " $choices " in *" ${GIVEN[$key]} "*) built "$key.${GIVEN[$key]}" || { echo "--$key ${GIVEN[$key]} is coming soon"; exit 1; }; set_env "$var" "${GIVEN[$key]}" ;; *) echo "--$key must be one of: $choices"; exit 1 ;; esac
  else
    set_env "$var" "$(part_value "$part")"
  fi
done
# flags or -y: also make the share-link tunnel match --remote, without questions (the menu does this in Install and Change setup)
do_install && NOASK=1 sync_tunnel
