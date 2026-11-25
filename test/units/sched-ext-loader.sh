#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -eu

work=$1 provider=$2 unit=$3
shift 3
echo "$$" >>"$work/$unit.starts"
# Allow stop/cancellation while admission is held. PID 1 sends single-byte commands on stdin.
while [[ -e "$work/$unit.hold" ]]; do
    rc=0
    IFS= read -r -n 1 -t .1 command || rc=$?
    if [[ $rc == 1 || $command == Q ]]; then
        exit 0
    fi
done
test ! -e "$work/$unit.fail"
exec "$provider" "$@"
