#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx_hierarchy yes
scx_setup "$child"
scx_set_loader "$provider"

systemctl start scxtest-work.service
test -f "$work/admitted"
systemctl is-active --quiet "$root"
assert_eq "$(systemctl show -P EffectiveScheduler scxtest.slice)" "$child"
cgid=$(stat -c %i /sys/fs/cgroup/scxtest.slice)
test -d "/sys/kernel/sched_ext/root/sub/sub-$cgid"
systemctl stop "$root"
test ! -e /sys/kernel/sched_ext/root
assert_eq "$(systemctl show -P ActiveState "$child")" inactive
