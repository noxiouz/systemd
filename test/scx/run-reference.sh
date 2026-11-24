#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euo pipefail

# Run from a clean checkout on an arm64 host with Lima. Redirect stdout to keep the JSON report.
testdir=$(cd -- "$(dirname -- "$0")" && pwd)
revision=$(git -C "$testdir" rev-parse HEAD)
provider=c03c1a14507dfaf8b856c2608f9ac216021fc222
image=$(sed -n 's/^  digest: sha256://p' "$testdir/reference.yaml")
work=$(mktemp -d /tmp/scx-reference.XXXXXXXX)
export LIMA_HOME="$work/lima"
mkdir -p "$LIMA_HOME"

cleanup() {
    limactl delete --force scx-reference >&2 && rm -rf "$work"
}
trap cleanup EXIT
limactl start --name=scx-reference --tty=false "$testdir/reference.yaml" >&2
guest=(limactl shell --workdir=/tmp scx-reference)
limactl copy "$testdir/baseline.py" scx-reference:/tmp/scx-baseline.py >&2
"${guest[@]}" sh -se -- "$provider" <<'BUILD' >&2
test "$(uname -r)" = '7.3.5+deb14-cloud-arm64'
echo 'deb [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://snapshot.debian.org/archive/debian/20261124T000000Z sid main' > /tmp/scx.list
apt() {
    sudo env DEBIAN_FRONTEND=noninteractive apt-get \
        -o Dir::Etc::sourcelist=/tmp/scx.list -o Dir::Etc::sourceparts=- "$@"
}
apt update
apt install -y --no-install-recommends \
    git ca-certificates make gcc libc6-dev clang-23 llvm bpftool pkgconf libelf-dev zlib1g-dev libcap-dev libssl-dev
git clone --filter=blob:none --depth=1 --sparse --branch v7.3 https://github.com/torvalds/linux.git scx-linux
git -C scx-linux sparse-checkout set tools/sched_ext tools/build tools/scripts tools/lib tools/include \
    tools/bpf/bpftool tools/arch include scripts arch/arm64/include kernel/bpf
test "$(git -C scx-linux rev-parse HEAD)" = "$1"
make -C scx-linux/tools/sched_ext -j4 CLANG=clang-23 scx_qmap
BUILD
"${guest[@]}" sudo python3 /tmp/scx-baseline.py --loader /tmp/scx-linux/tools/sched_ext/build/bin/scx_qmap \
    --provider-revision "$provider" --image-sha256 "$image" --repo-revision "$revision"
