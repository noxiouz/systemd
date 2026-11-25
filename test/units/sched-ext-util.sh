# SPDX-License-Identifier: LGPL-2.1-or-later
# shellcheck shell=bash

# shellcheck source=test/units/util.sh
. "$(dirname "$0")"/util.sh

units=/run/systemd/system
root='scxtest@-.scheduler'
child='scxtest@scxtest.scheduler'
root_dropin="$units/-.slice.d/90-scxtest.conf"
provider=/usr/lib/systemd/tests/unit-tests/manual/test-sched-ext-provider

require_scx() {
    test -x "$provider"
    "$provider" --probe
}

require_scx_hierarchy() {
    local expected=${1:?} rc=0
    require_scx
    "$provider" --probe-hierarchy || rc=$?
    case "$expected:$rc" in
        yes:0|no:77) ;;
        yes:77|no:0) echo "Skipping, this test requires hierarchy support to be $expected"; exit 77 ;;
        *) exit "$rc" ;;
    esac
}

scx_cleanup() {
    # Remove the root declaration first so subsequent test and shutdown jobs can run.
    rm -f "$root_dropin"
    systemctl daemon-reload
    systemctl stop "$child" "$root" scxtest.slice
    rm -f "$units"/{scxtest@.scheduler,scxtest.slice,scxtest-work.service}
    rm -rf "$work"
    systemctl daemon-reload
}

scx_setup() {
    local effective=${1:?}

    test ! -e /sys/kernel/sched_ext/root
    work=$(mktemp -d /run/scxtest.XXXXXX)
    trap scx_cleanup EXIT
    mkdir -p "$units/-.slice.d"
    cat >"$root_dropin" <<'UNIT'
[Slice]
Scheduler=scxtest@.scheduler
UNIT
    if [[ "$effective" == "$child" ]]; then
        cp "$root_dropin" "$units/scxtest.slice"
    else
        echo '[Slice]' >"$units/scxtest.slice"
    fi
    cat >"$units/scxtest-work.service" <<UNIT
[Service]
Type=oneshot
Slice=scxtest.slice
ExecStartPre=touch $work/workload-started
ExecStartPre=systemctl is-active --quiet $effective
ExecStart=touch $work/admitted
UNIT
}

scx_set_loader() {
    cat >"$units/scxtest@.scheduler" <<UNIT
[Scheduler]
ExecStart=${1:?}
UNIT
    systemctl daemon-reload
}
