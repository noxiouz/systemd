#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx_hierarchy yes
scx_setup "$child"
scx_controlled_loader
cp "$units/scxtest@.scheduler" "$units/$child"
echo 'TimeoutStartSec=3s' >>"$units/$child"
systemctl daemon-reload
systemctl start "$root"
root_link=$(systemctl show -P LinkID "$root")
touch "$work/$child.hold"
assert_fail systemctl start scxtest-work.service
systemctl is-failed "$child"
assert_eq "$(systemctl show -P Result "$child")" timeout
test ! -e "$work/workload-started"
assert_eq "$(systemctl show -P EffectiveScheduler scxtest.slice)" "$root"
assert_eq "$(systemctl show -P LinkID "$root")" "$root_link"

rm "$work/$child.hold"
systemctl start scxtest-work.service
test -e "$work/admitted"
assert_eq "$(systemctl show -P Result "$child")" success
assert_eq "$(systemctl show -P ParentScheduler "$child")" "$root"
assert_gt "$(systemctl show -P LinkID "$child")" 0
