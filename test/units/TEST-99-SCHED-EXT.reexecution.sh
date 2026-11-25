#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx_hierarchy yes
scx_setup "$child"
scx_controlled_loader
systemd-run --unit=scxtest-main --slice=scxtest.slice -p Type=exec sleep infinity
pid=$(systemctl show -P MainPID scxtest-main.service)
link=$(systemctl show -P LinkID "$child")
generation=$(systemctl show -P InvocationID "$child")
systemctl daemon-reexec
scx_wait_for scx_property_is "$child" ActiveState active
assert_eq "$(systemctl show -P LinkID "$child")" "$link"
assert_eq "$(systemctl show -P InvocationID "$child")" "$generation"
assert_eq "$(systemctl show -P MainPID scxtest-main.service)" "$pid"

systemctl stop "$child"
touch "$work/$child.hold"
systemctl start --no-block scxtest-work.service
scx_wait_for scx_has_job "$child"
scx_wait_for scx_has_job scxtest-work.service
scx_wait_for scx_property_is "$child" ActiveState activating
child_job=$(scx_job "$child")
job=$(scx_job scxtest-work.service)
generation=$(systemctl show -P InvocationID "$child")
systemctl daemon-reexec
scx_wait_for scx_has_job "$child"
assert_eq "$(scx_job "$child")" "$child_job"
assert_eq "$(scx_job scxtest-work.service)" "$job"
assert_eq "$(systemctl show -P InvocationID "$child")" "$generation"
test ! -e "$work/workload-started"
rm "$work/$child.hold"
scx_wait_for test -e "$work/admitted"
assert_eq "$(systemctl show -P MainPID scxtest-main.service)" "$pid"
assert_wc_l "$work/$child.starts" 2
