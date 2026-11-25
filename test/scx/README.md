# Reproducing the scheduler-unit prototype

Run from a clean checkout of the revision under test on an arm64 Linux host.
The full replay and the three preflight commands below use the fixtures in that checkout.

The host needs access to KVM, eight virtual CPUs, at least 8 GiB RAM and 40 GiB
free disk space in `/var/tmp`. Install Git, Python 3, OpenSSH, cpio, QEMU with
arm64 system emulation, qemu-img, arm64 EFI firmware and Lima. Minimal Debian 13
installations also need `ipxe-qemu` for the virtio network option ROM.
Allow HTTPS downloads from Debian, Fedora and GitHub.
Run the following as a normal user with access to `/dev/kvm`, from the Git
checkout. Choose an empty output directory outside the checkout.

```sh
python3 test/scx/run-prototype.py ../scheduler-evidence
```

To run only the kernel probe and hierarchy, the provider identity matrix, or the mixed scheduler
experiment in one guest, pass `--preflight kernel`, `--preflight identity` or `--preflight mixed`.
Each uses the same build recipe and writes its results in the output directory.

The full replay creates an isolated Lima home, builds systemd and the three
cooperative providers in the Debian guest, and installs the tested manager and
executor as guest PID 1. It runs TEST-99 with the native test provider, including successful root and child attachments.
It also runs the userspace tests, boots four two-vCPU QEMU disk overlays
and installs the same userspace build in the Fedora control guest. Each guest
uses its own distribution kernel and configuration. The recovery case adds its
distribution shell to the userspace initrd. The run removes its VMs and retains
the evidence directory.

To run TEST-99 separately, follow the [systemd integration-test instructions](../integration-tests/README.md).
Its test provider is part of the normal Meson test build.

| Input | Reference |
| --- | --- |
| Hierarchical guest | [Debian 7.3.5 image and SHA-256](reference.yaml) |
| Root-only control | [Fedora 6.19.10 image and SHA-256](replay/negative.yaml) |
| Provider source | [Linux v7.3, commit c03c1a14507dfaf8b856c2608f9ac216021fc222](https://github.com/torvalds/linux/tree/c03c1a14507dfaf8b856c2608f9ac216021fc222/tools/sched_ext) |
| Cooperative providers and per-task observers | [Build definitions](identity/meson.build), [protocol and dispatch evidence](identity/README.md) |
| Distribution build dependencies | [Pinned Debian package snapshot and build commands](replay/build.sh) |

## Historical prototype results

To reproduce the original matrix and observations, use a separate checkout of
[`e66be7c60a`](https://github.com/noxiouz/systemd/tree/e66be7c60adf44087faaaa7e81368845ce88df53/test/scx)
and its `python3 test/scx/run-prototype.py ../historical-evidence` command. Its standalone
preflight entrypoint was `test/scx/run-reference.sh` with no arguments, `--identity` or `--mixed`.

The clean Linux run before this report was added recorded the test matrix,
kernel attachments, per-task dispatch counts, fallback states and boot
observations. [Archived run](https://github.com/noxiouz/systemd/blob/e66be7c60adf44087faaaa7e81368845ce88df53/test/scx/replay/reference.json)

`manifest.json` gives the Git revision and tree, provider revision, named results,
and SHA-256 of every evidence file. `commands.log` contains the full build and
the commands run on both guests. `meson-verbose.log` includes the transaction
traces. The per-ticket JSON files include actual link and cgroup identities,
probe PIDs and dispatch counters, status properties, loss records, and boot
timestamps. `serial/` contains the four unabridged boot transcripts. A failed
command returns a nonzero exit status and leaves a partial manifest showing
which cases completed.

| Ticket | Assertions and retained evidence |
| --- | --- |
| SCX-01 | [Kernel capability, four concurrent attachments on three levels, inherited slice, teardown](baseline.py) |
| SCX-02 | [Seventeen target, parent, veto, replacement and terminal-loss protocol cases](identity/matrix.py) |
| SCX-03 | [Resolver identity across load orders](../../src/test/test-engine.c) and [valid and invalid unit files](../test-scheduler) |
| SCX-04 | [Cold and warm slice creation, shared activation, cancellation, failed and skipped starts, intentional cycle](../../src/test/test-scheduler.c) |
| SCX-05 | [Qmap and FIFO per-task dispatch, inheritance, partial mode and preservation of a sibling](identity/dispatch.py) |
| SCX-06 | [Supervised activation, timeout, failure, external occupancy, owned teardown and retry](lifecycle.py) |
| SCX-07 | [Normal, transient and socket-activated workloads, concurrent admission, veto, timeout and recovery](admission/run.py) |
| SCX-08 | [CLI and D-Bus against managed, evicted, kernel-default and foreign kernel states](observability.py) |
| SCX-09 | [Ordered branch restart, surviving PIDs and sibling dispatch, leaf and root loss, busy intermediate](branch.py) |
| SCX-10 | [Live and pending attachments across reexecution, changed binding after reload](reexecution.py) |
| SCX-11 | [Real cold boot, absent loader, console recovery, unconfigured boot and root-only control](boot/README.md) |

The demonstration covers the three-level arm64 hierarchy, cooperative
providers, cgroup v2, a system manager, and the stock kernels listed above.
The mixed-provider trace identifies the live instance that dispatched each
probe. The observability and branch-restart fixtures run consecutively on the
same PID 1. They remove and recreate the slice declarations and check that
the intermediate scheduler stays reachable through unit garbage collection.
The parser and transaction tests run independently of BPF privileges.
The complete archives and independent sign-offs for the published revision
are in the [ticket and replay record](https://chatgpt.com/space/page_7b70d4decde88191889b2642766a470b).

Two arm64 Linux hosts completed the reference run with Lima 2.2.1 and KVM.
One used Debian sid, Linux 7.3.5 and QEMU 11. The independent runner used Debian
13, Linux 6.12.111 and QEMU 10.0.13 with ten vCPUs and 10 GiB RAM. Both booted
the pinned Debian and Fedora guests above.

On the nested Debian sid host running on Apple silicon, preliminary runs also
saw intermittent stalls in stock guest programs before installing the candidate
manager. One SCX-07 recovery service reached startup after its scheduler became
ready, then its control process consumed 24.20 CPU seconds before the harness's
25-second timeout. Its executable and stack were unavailable after cleanup.
Three runs using Apple Virtualization.framework directly and the full run on
the Debian 13 host passed this case. Its cause remains open.
