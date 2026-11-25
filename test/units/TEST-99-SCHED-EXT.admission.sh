#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/sched-ext-util.sh
. "$(dirname "$0")"/sched-ext-util.sh

require_scx_hierarchy yes
scx_setup "$child"
cat >"$units/scxtest-client@.service" <<UNIT
[Service]
Type=oneshot
RemainAfterExit=yes
Slice=scxtest.slice
ExecCondition=touch $work/%i.condition
ExecStartPre=touch $work/%i.pre
ExecStart=touch $work/%i.main
UNIT
cat >"$units/scxtest.socket" <<UNIT
[Socket]
ListenStream=$work/socket
Service=scxtest-client@socket.service
UNIT
scx_controlled_loader
touch "$work/$child.hold"
systemctl start scxtest.socket
systemctl start --no-block scxtest-client@regular.service
scx_wait_for test -s "$work/$child.starts"
systemd-run --no-block --unit=scxtest-transient --slice=scxtest.slice -p Type=oneshot -p RemainAfterExit=yes \
    -p "ExecCondition=touch $work/transient.condition" -p "ExecStartPre=touch $work/transient.pre" touch "$work/transient.main"
python3 -c 'import socket, sys; s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])' "$work/socket"

for unit in scxtest-client@regular.service scxtest-client@socket.service scxtest-transient.service; do
    scx_wait_for scx_has_job "$unit"
done
for stage in "$work"/{regular,socket,transient}.{condition,pre,main}; do
    test ! -e "$stage"
done
rm "$work/$child.hold"
for stage in "$work"/{regular,socket,transient}.{condition,pre,main}; do
    scx_wait_for test -e "$stage"
done
assert_wc_l "$work/$child.starts" 1
assert_eq "$(systemctl show -P EffectiveScheduler scxtest.slice)" "$child"
