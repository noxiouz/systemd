#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euo pipefail

systemd-detect-virt --vm --quiet
[[ $EUID == 0 && $1 =~ ^[a-f0-9]{40}$ ]]
dst=/opt/scx-replay/$1
mkdir -p "$dst"
tar xf /var/tmp/scx/package.tar -C "$dst" --no-same-owner
"$dst/systemd" --version
for name in systemd-executor systemd; do
    target=/usr/lib/systemd/$name
    if [[ -e /sys/fs/selinux/enforce ]]; then
        chcon --reference="$target" "$dst/$name"
    fi
    ln -s "$dst/$name" "$target.scx-next"
    mv -Tf "$target.scx-next" "$target"
done
for name in systemctl systemd-analyze bpftool; do
    ln -sfn "$dst/$name" "/usr/local/bin/$name"
done
provider=/usr/lib/systemd/tests/unit-tests/manual/test-sched-ext-provider
mkdir -p "${provider%/*}"
ln -sfn "$dst/test-sched-ext-provider" "$provider"
/usr/bin/systemctl daemon-reexec
[[ $(readlink /proc/1/exe) == "$dst/systemd" ]]
systemd-run --unit=scx-replay-smoke --collect --wait /usr/bin/true
