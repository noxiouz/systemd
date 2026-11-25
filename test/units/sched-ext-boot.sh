#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

mode=${1:?}
test_unit=${2:?}
workload=scxboot-workload.service
root=scxboot@-.scheduler
units=/etc/systemd/system

if (( REBOOT_COUNT == 0 )); then
    if [[ "$mode" == attached ]]; then
        require_scx
    fi

    # Prepare the next boot without reloading the running manager. The first transaction in the
    # new manager must load the root declaration and mount the filesystem supplying its loader.
    mkdir -p "$units/-.slice.d"
    cat >"$units/-.slice.d/90-scxboot.conf" <<'EOF'
[Slice]
Scheduler=scxboot@.scheduler
EOF
    cat >"$units/run-scxboot.mount" <<EOF
[Unit]
DefaultDependencies=no
[Mount]
What=${provider%/*}
Where=/run/scxboot
Type=none
Options=bind
EOF
    loader=/run/scxboot/${provider##*/}
    if [[ "$mode" == missing ]]; then
        loader=/run/scxboot/nonexistent-sched-ext-provider
    fi
    cat >"$units/scxboot@.scheduler" <<EOF
[Unit]
RequiresMountsFor=/run/scxboot
[Scheduler]
ExecStart=$loader
TimeoutStartSec=15s
EOF

    # Pull the workload into the initial transaction. Its start must not wait on basic.target or
    # any other service that would itself need the root scheduler.
    mkdir -p "$units/$test_unit.d"
    cat >"$units/$test_unit.d/90-scxboot.conf" <<EOF
[Unit]
Wants=$workload
After=$workload
EOF
    cat >"$units/$workload" <<'EOF'
[Unit]
DefaultDependencies=no
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=touch /run/scxboot-workload-ran
EOF
    if [[ "$mode" == missing ]]; then
        cat >>"$units/$workload" <<'EOF'
[Unit]
OnFailure=scxboot-rejected.service
EOF
        # With a broken root no new service can spawn a process. A commandless service can still
        # report the dependency failure to the host; admitting any command invalidates the verdict.
        cat >"$units/scxboot-rejected.service" <<'EOF'
[Unit]
DefaultDependencies=no
ConditionPathIsMountPoint=/run/scxboot
ConditionPathExists=!/run/scxboot-workload-ran
SuccessAction=exit
SuccessActionExitStatus=187
[Service]
Type=oneshot
EOF
    fi
    SYSTEMCTL_SKIP_AUTO_SOFT_REBOOT=1 systemctl_final reboot
    exec sleep infinity
fi

assert_eq "$REBOOT_COUNT" 1
assert_eq "$mode" attached
mountpoint /run/scxboot
test -f /run/scxboot-workload-ran
systemctl is-active --quiet "$workload"
systemctl is-active --quiet "$root"
assert_eq "$(systemctl show -P EffectiveScheduler -- -.slice)" "$root"
assert_eq "$(< /sys/kernel/sched_ext/state)" enabled
attached=$(systemctl show -P ActiveEnterTimestampMonotonic "$root")
started=$(systemctl show -P ExecMainStartTimestampMonotonic "$workload")
(( attached > 0 && attached <= started ))
touch /testok
