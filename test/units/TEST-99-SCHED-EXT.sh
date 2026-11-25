#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

units=/run/systemd/system
root='scxtest@-.scheduler'
child='scxtest@scxtest.scheduler'
root_dropin="$units/-.slice.d/90-scxtest.conf"
work=$(mktemp -d /run/scxtest.XXXXXX)

cleanup() {
    # Drop the root declaration first so subsequent test and shutdown jobs can run.
    rm -f "$root_dropin"
    systemctl daemon-reload
    systemctl stop "$child" "$root" scxtest.slice
    rm -f "$units"/{scxtest@.scheduler,scxtest.slice,scxtest-work.service}
    rm -rf "$work"
    systemctl daemon-reload
}
trap cleanup EXIT

mkdir -p "$units/-.slice.d"
cat >"$root_dropin" <<'UNIT'
[Slice]
Scheduler=scxtest@.scheduler
UNIT
cp "$root_dropin" "$units/scxtest.slice"
cat >"$units/scxtest-work.service" <<UNIT
[Service]
Type=oneshot
Slice=scxtest.slice
ExecStartPre=touch $work/workload-started
ExecStartPre=systemctl is-active --quiet $root
ExecStartPre=systemctl is-active --quiet $child
ExecStart=touch $work/admitted
UNIT

# This path also exercises a guest without sched_ext: the native root loader
# must be invoked, and no workload command may run after it fails.
cat >"$units/scxtest@.scheduler" <<UNIT
[Scheduler]
ExecStart=sh -c 'touch $work/attempted; exit 1'
UNIT
systemctl daemon-reload
if systemctl start scxtest-work.service; then
    exit 1
fi
test -f "$work/attempted"
test ! -e "$work/workload-started"
systemctl is-failed "$root"
test "$(systemctl show -p ConfiguredScheduler --value scxtest.slice)" = "$child"

# Preinstall the cooperative qmap fixture to require the positive path.
provider=/usr/local/libexec/scx-test-provider
if [[ ! -e "$provider" ]]; then
    echo "Root failure enforced. Install $provider for the live hierarchy test." >/skipped
    exit 77
fi
test -x "$provider"
cat >"$units/scxtest@.scheduler" <<UNIT
[Scheduler]
ExecStart=$provider
UNIT
systemctl daemon-reload
systemctl reset-failed "$root"
systemctl start scxtest-work.service
test -f "$work/admitted"
test "$(systemctl show -p EffectiveScheduler --value scxtest.slice)" = "$child"
cgid=$(stat -c %i /sys/fs/cgroup/scxtest.slice)
test -d "/sys/kernel/sched_ext/root/sub/sub-$cgid"
systemctl stop "$root"
test ! -e /sys/kernel/sched_ext/root
test "$(systemctl show -p ActiveState --value "$child")" = inactive
touch /testok
