#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

# Even a kernel without sched_ext can exercise a failing userspace loader.
scx_setup "$child"
scx_set_loader "sh -c 'touch $work/attempted; exit 1'"

assert_fail systemctl start scxtest-work.service
test -f "$work/attempted"
test ! -e "$work/workload-started"
systemctl is-failed "$root"
assert_eq "$(systemctl show -P ConfiguredScheduler scxtest.slice)" "$child"
