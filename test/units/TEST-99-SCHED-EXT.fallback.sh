#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx_hierarchy yes
scx_tree
cat >"$units/scxtest-gate.service" <<UNIT
[Service]
Type=oneshot
Slice=scxtest-build.slice
ExecCondition=touch $work/condition
ExecStartPre=touch $work/pre
ExecStart=touch $work/main
UNIT
systemctl daemon-reload

cpu_ticks() {
    # Both workload processes are yes(1), with no spaces in /proc/PID/stat's comm field.
    awk '{print $14 + $15}' "/proc/$1/stat"
}

made_progress() {
    (($(cpu_ticks "$1") > $2))
}

node_count_is() {
    local count
    count=$(find /sys/kernel/sched_ext -name ops -print | wc -l) || return
    [[ "$count" -eq "$1" ]]
}

declare -A pid start
for service in scxtest-main.service scxtest-side.service; do
    pid[$service]=$(systemctl show -P MainPID "$service")
    start[$service]=$(awk '{print $22}' "/proc/${pid[$service]}/stat")
done
for failed in "$leaf" "$child" "$root"; do
    case "$failed" in
        "$leaf") effective=$child nodes=3 ;;
        "$child") effective=$root nodes=2 ;;
        "$root") effective=kernel nodes=0 ;;
    esac
    scx_wait_for node_count_is 4
    side_link=$(systemctl show -P LinkID "$sibling")
    loader_pid=$(tail -n 1 "$work/$failed.starts")
    test "/proc/$loader_pid/exe" -ef "$provider"
    # Keep subsequent invocations unavailable while killing the currently attached provider.
    touch "$work/$failed.fail"
    kill -KILL "$loader_pid"
    scx_wait_for scx_property_is "$failed" ActiveState failed
    scx_wait_for scx_property_is scxtest-build.slice EffectiveScheduler "$effective"
    scx_wait_for node_count_is "$nodes"
    assert_eq "$(systemctl show -P ConfiguredScheduler scxtest-build.slice)" "$leaf"
    for service in scxtest-main.service scxtest-side.service; do
        assert_eq "$(systemctl show -P MainPID "$service")" "${pid[$service]}"
        assert_eq "$(awk '{print $22}' "/proc/${pid[$service]}/stat")" "${start[$service]}"
        scx_wait_for made_progress "${pid[$service]}" "$(cpu_ticks "${pid[$service]}")"
    done
    if [[ "$failed" == "$root" ]]; then
        assert_eq "$(</sys/kernel/sched_ext/state)" disabled
        assert_eq "$(systemctl show -P EffectiveScheduler scxtestsibling.slice)" kernel
    else
        assert_eq "$(systemctl show -P EffectiveScheduler scxtestsibling.slice)" "$sibling"
        assert_eq "$(systemctl show -P LinkID "$sibling")" "$side_link"
    fi

    assert_fail systemctl start scxtest-gate.service
    for stage in condition pre main; do
        test ! -e "$work/$stage"
    done
    rm "$work/$failed.fail"
    systemctl start scxtest-gate.service
    for stage in condition pre main; do
        test -e "$work/$stage"
        rm "$work/$stage"
    done
    assert_eq "$(systemctl show -P EffectiveScheduler scxtest-build.slice)" "$leaf"
done

# The first new workload rebuilt the failed root's path to the leaf. A new sibling workload
# restores that branch on demand as well.
systemd-run --unit=scxtest-side-probe --wait --collect --slice=scxtestsibling.slice true
assert_eq "$(systemctl show -P EffectiveScheduler scxtestsibling.slice)" "$sibling"
scx_wait_for node_count_is 4
for service in scxtest-main.service scxtest-side.service; do
    assert_eq "$(systemctl show -P MainPID "$service")" "${pid[$service]}"
    assert_eq "$(awk '{print $22}' "/proc/${pid[$service]}/stat")" "${start[$service]}"
done
