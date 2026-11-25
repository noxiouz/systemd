#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx_hierarchy yes
scx_tree
side_link=$(systemctl show -P LinkID "$sibling")
leaf_link=$(systemctl show -P LinkID "$leaf")
main_pid=$(systemctl show -P MainPID scxtest-main.service)

touch "$work/$child.hold"
systemctl restart --no-block "$child"
scx_wait_for scx_property_is "$child" ActiveState activating
assert_eq "$(systemctl show -P EffectiveScheduler scxtest-build.slice)" "$root"
assert_eq "$(systemctl show -P LinkID "$sibling")" "$side_link"
rm "$work/$child.hold"
scx_wait_for scx_property_is "$leaf" ActiveState active
assert_neq "$(systemctl show -P LinkID "$leaf")" "$leaf_link"
assert_eq "$(systemctl show -P ParentScheduler "$leaf")" "$child"
assert_eq "$(systemctl show -P MainPID scxtest-main.service)" "$main_pid"
assert_eq "$(systemctl show -P LinkID "$sibling")" "$side_link"

# Reload without the declarations, then recreate them on this PID 1. A new workload must still
# pull in the configured intermediate even after the previous branch was collected.
systemctl stop scxtest-main.service scxtest-build.slice scxtest.slice "$leaf" "$child"
mv "$units/scxtest.slice" "$work/middle"
mv "$units/scxtest-build.slice" "$work/leaf"
systemctl daemon-reload
mv "$work/middle" "$units/scxtest.slice"
mv "$work/leaf" "$units/scxtest-build.slice"
systemctl daemon-reload
systemd-run --wait --collect --unit=scxtest-gc --slice=scxtest-build.slice true
assert_eq "$(systemctl show -P ParentScheduler "$leaf")" "$child"
assert_eq "$(systemctl show -P EffectiveScheduler scxtest-build.slice)" "$leaf"
assert_eq "$(systemctl show -P LinkID "$sibling")" "$side_link"
