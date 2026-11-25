#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx_hierarchy no
scx_setup "$child"
scx_set_loader "$provider"

systemctl start "$root"
test -d /sys/kernel/sched_ext/root
assert_fail systemctl start scxtest-work.service
test ! -e "$work/workload-started"
systemctl is-failed "$child"
assert_eq "$(systemctl show -P Result "$child")" unsupported
systemctl is-active --quiet "$root"
test -d /sys/kernel/sched_ext/root
