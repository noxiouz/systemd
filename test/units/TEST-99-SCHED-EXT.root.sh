#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx
scx_setup "$root"
scx_set_loader "$provider"

systemctl start scxtest-work.service
test -f "$work/admitted"
test -d /sys/kernel/sched_ext/root
assert_eq "$(systemctl show -P EffectiveScheduler scxtest.slice)" "$root"
systemctl stop "$root"
test ! -e /sys/kernel/sched_ext/root
