# Service admission with sched_ext

This test runs on the isolated Linux 7.3.5 reference guest with the prototype installed as PID 1,
its matching systemd-executor, and its systemctl on PATH. Build the mixed providers against the
pinned kernel sources and generated qmap skeleton as described in the
[integration test guide](../../integration-tests/README.md). The mixed Meson build also produces
the `admission-exec.bpf.o` observer. From the repository root, run the following as root
with the absolute path to that build.

```sh
python3 test/scx/admission/run.py /var/tmp/scx-07-providers >scx-admission.json
```

The test creates transient fixtures under `/run/` and verifies that all schedulers detach on
teardown. The root and the intermediate slice use the upstream five-queue qmap policy. The build
slice uses the single-queue FIFO leaf from the compatibility matrix. The independent interactive
slice uses qmap. On the eight-vCPU guest the intermediate, build and interactive slice CPU weights
are 300, 100 and 100. The veto case temporarily reduces the intermediate weight to 1. It then
lacks an exclusively granted CPU and its real `sub_attach` callback rejects the child with
`ENOSPC`. These placements matter because qmap partitions available CPU time among its children.

The read-only BPF observer records `sched_process_exec` for private copies of the workload's
three executables. For each executable inode the report includes its first monotonic timestamp,
PID, count and the scheduler node identifiers read from the kernel's task ancestry. The test
compares these with the sysfs tree and the manager's active timestamps. Holding the build loader
before its attach call lets the interactive service start while both concurrent builds have zero
command markers.

The matrix exercises ordinary, transient and socket-activated services with both absent and
precreated workload slices. Failure cases capture the journal for each scheduler invocation,
confirm zero condition, pre-start and main commands, then verify a fresh start after clearing each
fault. The final case detaches the already-admitted workload's scheduler, verifies that its PID
survives, and traces a new workload executing through the restored chain.

[Reference run](reference.json)
