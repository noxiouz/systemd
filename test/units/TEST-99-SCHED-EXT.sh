#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -eu
set -o pipefail

for tool in python3 bpftool scx_qmap; do
    if ! command -v "$tool" >/dev/null; then
        echo "Missing $tool" >/skipped
        exit 77
    fi
done
if ! scx_qmap -h 2>&1 | grep -F -- '-c CG_PATH' >/dev/null; then
    echo 'scx_qmap lacks subtree attachment support' >/skipped
    exit 77
fi

baseline="$(dirname "$0")/../scx/baseline.py"
status=0
python3 "$baseline" --loader "$(command -v scx_qmap)" || status=$?
if (( status == 77 )); then
    echo 'The running kernel lacks sched_ext hierarchy support' >/skipped
fi
(( status == 0 )) || exit "$status"
touch /testok
