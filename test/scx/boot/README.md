# Root scheduler boot and recovery

Run these fixtures on disposable arm64 VMs. Both the Debian positive image and the
Fedora root-only control boot their distribution kernels. Each image digest,
kernel digest, configuration digest, and observed BTF capability is in the
[reference report](reference.json).

## Debian cold boot

Use the Debian image pinned in the [reference configuration](../reference.yaml).
Install the prototype as the host system manager with its matching
systemd-executor and systemctl. Build the cooperative qmap loader using the
[identity fixture](../identity/README.md). Keep the distribution initrd's
manager and unit configuration. From the checkout inside the guest, run:

```sh
sudo python3 test/scx/boot/guest.py install /absolute/path/to/identity-provider
sudo python3 test/scx/boot/guest.py metadata 2203f7dab8af7c7e82ff7a3b2e6c3a2ce7a48444db3d56679965fde7c303cdf5 >guest.json
cat /proc/cmdline
```

The fixture is inert unless the host kernel command line selects a test case.
Copy `guest.json`, `/boot/vmlinuz-$(uname -r)`, `/boot/initrd.img-$(uname -r)`
and `/usr/bin/bash` to the host. Shut the guest down cleanly. On an arm64 host
with Python 3, cpio, qemu-img and qemu-system-aarch64, run:

```sh
python3 test/scx/boot/cold.py --disk /path/to/offline-disk --format raw \
    --kernel ./vmlinuz --initrd ./initrd --bash ./bash --metadata guest.json \
    --root PARTUUID=original-partition-uuid --logs ./serial >cold.json
```

Use the partition identifier printed by the guest and its actual disk format
(`raw` or `qcow2`). QEMU uses a fresh writable overlay for each run.
The harness captures the full UART transcript including PID 1's job messages.
It boots the following four configurations:

* The root slice selects a scheduler on the host after switch-root. Its
  `RequiresMountsFor=/scxloader` brings up a real bind mount that supplies the
  executable. The loader delays attachment for 1.5 seconds. A successful boot
  reaches `scxboot.target`, with the first workload marker after the root's
  confirmed active timestamp and with the kernel running qmap.
* A missing root executable fails the dependent workload and target within
  the scheduler's eight-second startup budget. The console records the missing
  file and the workload produces zero command markers.
* The same missing-executable configuration boots with
  `rd.systemd.break=pre-switch-root`. The harness types commands into the
  initrd's real console shell, checks the mounted host filesystem, writes a
  late empty `Scheduler=` override, then exits. The ordinary control workload
  reaches its target under kernel-default scheduling.
* A boot with no scheduler declarations reaches the same target.

Debian's pinned initrd includes systemd-debug-generator, the breakpoint unit,
bash's dynamic loader and libraries. It omits the `/usr/bin/bash` executable
that the breakpoint unit invokes. The runner appends that distribution
executable in a userspace cpio archive only for the recovery boot. The report
includes the hashes of the distribution initrd, recovery initrd and shell.
The recovery path therefore requires a working shell in the deployed initrd.
Loaders backed by storage prepared by a service which itself needs the root
scheduler would introduce a dependency cycle. This fixture exercises a
mount from files already present after switch-root.

## Fedora root-only control

Use the [Fedora 44 Cloud image](https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/aarch64/images/Fedora-Cloud-Base-Generic-44-1.7.aarch64.qcow2)
with SHA-256 `55c60a3b80d3616a08705afd0459e75fe9f03c54aba7a46e4002a41a72fa0d5b`.
It provides kernel 6.19.10-300.fc44.aarch64 with root sched_ext and readable
BTF. Install the prototype as host PID 1 with its matching executor and
systemctl. On this SELinux-enforcing image, apply the distribution manager's
SELinux file context to the installed manager and executor before reexecution.

Build the CPU-based upstream `scx_simple` skeleton alongside qmap on the same
reference build guest, using the pinned sources and toolchain from the
[identity fixture](../identity/README.md):

```sh
make -C "$linux/tools/sched_ext" CLANG=clang-23 scx_simple
meson setup /tmp/scx-boot-providers test/scx/identity \
    -Dlinux-source="$linux" -Dqmap-build="$linux/tools/sched_ext/build" -Dlegacy-root=true
meson compile -C /tmp/scx-boot-providers simple-provider
```

Copy `simple-provider` and bpftool to the Fedora guest. The reference build
uses the Debian guest's compatible glibc 2.43 and the pinned static libbpf.
On Fedora, run the following from the checkout with the patched client on PATH:

```sh
sudo env PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    python3 test/scx/boot/unsupported.py /absolute/path/to/simple-provider \
    --image-sha256 55c60a3b80d3616a08705afd0459e75fe9f03c54aba7a46e4002a41a72fa0d5b
```

The test confirms an active upstream `simple` root, checks the absence of its
child attachment directory, then asks PID 1 to start a service in a slice with
a child scheduler declaration. It checks the actual failed start, PID 1's
unsupported-feature diagnostic, zero workload markers and a still-healthy
root. It removes its runtime units and verifies final kernel detach.
