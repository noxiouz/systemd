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
    systemctl stop 'scxtest*.socket' 'scxtest*.service' 'scxtest*.scheduler' 'scxtest*.slice'
    rm -f "$units"/scxtest*.{socket,service,scheduler,slice}
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

scx_wait_for() {
    local deadline=$((SECONDS + 30))
    until "$@"; do
        if ((SECONDS >= deadline)); then
            echo "Timed out waiting for $*" >&2
            return 1
        fi
        sleep .1
    done
}

scx_property_is() {
    [[ "$(systemctl show -P "$2" -- "$1")" == "$3" ]]
}

scx_job() {
    systemctl list-jobs --no-legend --plain "$1" | awk -v unit="$1" '$2 == unit { print $1 }'
}

scx_has_job() {
    [[ -n "$(scx_job "$1")" ]]
}

scx_controlled_loader() {
    scx_set_loader "$(realpath "$(dirname "$0")"/sched-ext-loader.sh) $work $provider %n"
}

scx_tree() {
    scx_setup "$child"
    leaf=scxtest@scxtest-build.scheduler
    sibling=scxtest@scxtestsibling.scheduler
    for slice in scxtest scxtest-build scxtestsibling; do
        cp "$root_dropin" "$units/$slice.slice"
    done
    scx_controlled_loader
    systemd-run --unit=scxtest-main --slice=scxtest-build.slice -p Type=exec -p StandardOutput=null yes
    systemd-run --unit=scxtest-side --slice=scxtestsibling.slice -p Type=exec -p StandardOutput=null yes
    assert_eq "$(systemctl show -P EffectiveScheduler scxtest-build.slice)" "$leaf"
    assert_eq "$(systemctl show -P EffectiveScheduler scxtestsibling.slice)" "$sibling"
}
