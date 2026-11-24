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

## Inspecting schedulers and slices

Use `systemctl list-units --type=scheduler --all` to enumerate managed scheduler instances.
The scheduler's status shows its target slice, parent scheduler and the diagnostic from its
most recent failure. `systemctl show` also exposes its current owned struct-ops `LinkID`.
On a slice, `Scheduler` names the local declaration and `ConfiguredScheduler` resolves the
closest declaration, including inherited ones. `EffectiveScheduler` checks the current kernel
tree and reports an instance name for a confirmed manager-owned attachment, `kernel` when
sched_ext is disabled, `foreign` for an unowned attachment, `inactive` for an inactive slice
while sched_ext is enabled, or `unknown` while identity cannot be confirmed. A provider
algorithm name alone cannot establish ownership.

Run `python3 test/scx/observability.py /absolute/path/to/identity-provider` as root on the same
disposable guest to compare D-Bus and systemctl output with the kernel tree throughout managed
operation, forced eviction and attachment of a different instance of the same algorithm.

## Branch restart and recovery

On the reference Linux VM with the prototype installed as PID 1 and its matching
`systemctl` on PATH, run the following command from the source tree. Pass the
absolute mixed-provider build directory produced by the SCX test recipe.

```sh
python3 test/scx/branch.py /var/tmp/scx-mixed-build >scx-branch.json
```

Restarting a parent scheduler restarts its active descendants through the unit
transaction. The kernel's `sched_ext_exit` tracepoint records leaf-first detach.
While a fresh parent loader is held before attachment, the test reads the
independent sibling's BPF link ID and per-task dispatch count. It also exercises
leaf and root eviction, compares the effective unit property with the observed
fallback, and checks that new service commands wait for recovery. Installing an
intermediate slice declaration above a live attachment must return `EBUSY`
while the original child remains attached to its parent.

## Reload and manager reexecution

Run `python3 test/scx/reexecution.py /absolute/path/to/identity-provider` as root on the
reference guest. It records four independent link and cgroup identities, the providers' launch
counts, a running workload PID and journal results for delayed and evicted attachments across
three PID 1 reexecutions.

The system manager transfers the private provider socket, its link, the target cgroup and
scheduler directory descriptors, and outstanding process watches across reload and reexecution.
While restoring an invocation it checks fresh kernel and provider evidence before admission.
A changed slice declaration takes effect for future attachments. To apply it to a live branch,
stop the superseded instance, then start the newly selected instance and its required children.
