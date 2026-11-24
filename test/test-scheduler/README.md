# Scheduler activation transaction

Run `meson test -C build -v test-scheduler` from a writable, delegated cgroup on Linux.
The test manager uses a link-time backend that can delay, acknowledge, or fail an attachment.
Its file descriptor identifies the actual target cgroup. Its service commands run through
systemd-executor. The verbose test log includes the target and parent of every attempt,
the service command markers, the failed jobs, and the names in the intentional ordering cycle.

An attachment pulls in its target slice. A child also pulls in the nearest scheduler declared
above that slice. A service pulls in the scheduler selected for its slice. The scheduler start
job is essential to the start transaction and ordered before its consumer. On dispatch the
consumer checks every configured ancestor. A failed or condition-skipped attachment therefore
blocks admission. A later explicit stop of the scheduler has no stop dependency on admitted
services. The manager opens the realized cgroup immediately before passing its descriptor to
the backend. The production backend supervises a cooperative provider and verifies its per-invocation evidence against the kernel scheduler tree.

## Root bootstrap and recovery

Configure the root slice on the host after switching out of the initrd. Keep the initrd free of
host scheduler declarations. Install the root loader, its interpreter, libraries and any BPF
objects on the root filesystem or on a filesystem already mounted by the initrd. Use an absolute
loader path in `ExecStart=`. The manager adds mount dependencies for that path. Declare any extra
paths (including a separately mounted real directory behind a symlink) with `RequiresMountsFor=`.
The root scheduler uses the perpetual root slice. A service needed to make its own root loader
available would introduce an ordering cycle. The cycle fixture exercises this case.

For recovery with a systemd-based initrd containing systemd-debug-generator and the breakpoint
units, add `rd.systemd.break=pre-switch-root` at the boot menu. The initrd opens its console shell
with the host root mounted at `/sysroot`. Restore the loader there, or override the root selection
by setting an empty `Scheduler=` in a late drop-in under
`/sysroot/etc/systemd/system/-.slice.d/`, then exit the shell to continue booting. If child slices
also declare schedulers, clear those declarations to restore kernel-default scheduling for those
workloads. End-to-end cold boot belongs to the separate VM integration test.

## Native lifecycle on the reference kernel

Boot a dedicated guest with the candidate system manager, its matching systemd-executor and
systemctl. Build the cooperative qmap provider with the SCX-02 procedure. With the patched
systemctl first on PATH, run `python3 test/scx/lifecycle.py /absolute/path/to/identity-provider`
as root. It creates runtime-only unit files and removes them after the run. Its JSON report
identifies confirmed kernel nodes, invocation-specific failure journal entries, the foreign
and owned link IDs, and the PIDs reaped after stop.

For this prototype sd-executor gives the loader a fresh SOCK_SEQPACKET channel on standard
input and the realized target cgroup at descriptor 3. It appends the invocation ID, descriptor
number and `root` or `child` role to the configured arguments. The provider returns its own
struct-ops link via SCM_RIGHTS and reports its durable exit record in response to status
queries. PID 1 checks the kernel tree, cgroup and instance identities and retains the link.
`TimeoutStartSec=` bounds confirmation. Stop explicitly detaches the owned link in a helper
process, even when another process holds its own descriptor.
