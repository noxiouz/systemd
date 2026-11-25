#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euo pipefail

systemd-detect-virt --vm --quiet
[[ $(uname -r) == 7.3.5+deb14-cloud-arm64 ]]
cd /var/tmp/scx
cat >snapshot.list <<'EOF'
deb [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://snapshot.debian.org/archive/debian/20261124T180000Z sid main
EOF
apt=(sudo env DEBIAN_FRONTEND=noninteractive apt-get -o Dir::Etc::sourcelist=/var/tmp/scx/snapshot.list -o Dir::Etc::sourceparts=-)
"${apt[@]}" update
"${apt[@]}" install -y --no-install-recommends git gcc make clang-23 llvm bpftool meson pkgconf libcap-dev \
    ca-certificates libelf-dev zlib1g-dev libssl-dev
git clone --filter=blob:none --depth=1 --sparse --branch v7.3 https://github.com/torvalds/linux.git linux
git -C linux sparse-checkout set tools/sched_ext tools/build tools/scripts tools/lib tools/include \
    tools/bpf/bpftool tools/arch include scripts arch/arm64/include kernel/bpf
[[ $(git -C linux rev-parse HEAD) == "$1" ]]
targets=(scx_qmap)
options=()
case "$2" in
    full) targets+=(scx_simple); options+=(-Dlegacy-root=true -Dmixed=true) ;;
    mixed) options+=(-Dmixed=true) ;;
esac
make -C linux/tools/sched_ext -j4 CLANG=clang-23 "${targets[@]}"
if [[ "$2" == kernel ]]; then exit 0; fi
meson setup mixed src/test/scx/identity -Dlinux-source="$PWD/linux" -Dqmap-build="$PWD/linux/tools/sched_ext/build" \
    "${options[@]}"
meson compile -C mixed
if [[ "$2" != full ]]; then exit 0; fi

"${apt[@]}" install -y --no-install-recommends g++ ninja-build gperf python3-jinja2 python3-pyelftools \
    python3-packaging libmount-dev libblkid-dev libselinux-dev libseccomp-dev libacl1-dev libaudit-dev \
    libpam0g-dev liblz4-dev liblzma-dev libzstd-dev libkmod-dev libpcre2-dev libcryptsetup-dev
meson setup build src --prefix=/usr --sysconfdir=/etc --libdir=lib/aarch64-linux-gnu
meson compile -C build systemd:executable systemd-executor:executable systemd-analyze:executable systemctl:executable

# Preserve the in-tree RUNPATH layout used by this particular build on both guests.
bundle=$PWD/package
mkdir -p "$bundle"
for exe in systemd systemd-executor systemd-analyze systemctl; do
    install -m755 "build/$exe" "$bundle/$exe"
done
for lib in build/src/{core/libsystemd-core,shared/libsystemd-shared}-*.so; do
    install -Dm755 "$lib" "$bundle/${lib#build/}"
done
install -m755 mixed/simple-provider linux/tools/sched_ext/build/sbin/bpftool "$bundle/"
tar cf package.tar -C "$bundle" .
