# Scheduler attachment identity

Run `python3 test/scx/run-prototype.py --preflight identity ../identity-evidence` on an arm64 host with Lima.
The launcher boots the pinned Linux 7.3.5 Debian image, builds the upstream qmap BPF
skeleton and a cooperative userspace loader, runs the matrix as root inside the VM, and deletes
the guest. [The archived fixture report](https://github.com/noxiouz/systemd/blob/e66be7c60adf44087faaaa7e81368845ce88df53/test/scx/identity/reference.json) records one successful run. Cgroup IDs, link IDs,
kernel directory inodes and invocation IDs are allocated for each run.

## Mixed providers and task dispatch

Run `python3 test/scx/run-prototype.py --preflight mixed ../mixed-evidence` to exercise qmap at the
root, intermediate slice and sibling, with a separate single-queue FIFO policy at
the grandchild. Both loaders use the same per-invocation protocol. The report
records each provider's source, algorithm, roles, required features and build
digests. A test-only tracing program counts calls to the installed kernel's
`set_next_task_scx` by probe PID and live scheduler sysfs inode. Probe processes
acknowledge wakeups over a pipe so ordinary tasks in partial mode also demonstrate
forward progress. The run checks the inheriting slice, a sibling's ongoing
dispatch across leaf removal, and explicit versus ordinary task policy at a
partial-mode root.

## What establishes readiness

1. The manager opens the target cgroup and gives the provider the FD, a fresh 128-bit invocation ID
   and a private Unix sequenced-packet socket. The provider takes the target ID from the FD and
   configures the upstream scheduler with that ID.
2. After the struct-ops attach call returns, the provider sends its link FD over the socket.
   The manager verifies the FD type, holds the FD and records the invocation, target and link ID.
   It also opens the observed sysfs node under the intended parent and retains its inode identity.
3. The manager queries the provider's exit record for this invocation and validates each ancestor.
   Readiness requires successful attach, zero exit kind, the original target, and the same live
   sysfs node below the intended parent. A terminal record remains readable until shutdown.
   A separate traversal of the kernel's entire tree records the actual parent, target and node for
   each case, including attachments that landed elsewhere.

The test trusts the provider to supply the link it obtained for this invocation, to configure the
target from the passed FD, and to report its own scheduler's `ops.exit` record. Kernel sysfs
exposes the scheduler name and topology. The name can be identical for multiple attachments.
The protocol samples liveness at the time of the check. Subsequent losses require notifications
and reconciliation. An administrator with privileges to replace BPF objects is outside this
experimental ownership model.

## Observed cases on the pinned kernel

| Case | Result |
| --- | --- |
| Missing parent, or root without `sub_attach` | The attach call returned `ENODEV` or `EOPNOTSUPP`, respectively. BTF still indicated kernel support. |
| Capable parent vetoed a child | `sub/` existed and a link FD was returned. The provider reported terminal kind 1024 and `parent rejected (-28)`. The loader remained alive and the kernel tree contained no child. |
| Wrong target or wrong configured parent | The independent tree located the real attachment. The manager rejected the requested binding. |
| Child removed while active | Removing its empty cgroup removed the node. The loader remained alive with its link open. After discarding the exit event, an explicit status query recovered kind 66 and the cgroup-offline reason. |
| Explicit stop with a third-party duplicate | `BPF_LINK_DETACH` on the received link removed the targeted node and produced exit kind 64. The third process kept its FD and the sibling remained ready. |
| Same-name replacement and old reply | The new attachment at the same target had fresh link, invocation and sysfs node identities. The old reply failed the new invocation check. |
| Recreated cgroup at the same path | The original FD retained the old target ID. The new cgroup and replacement scheduler had distinct IDs. The old live-looking reply failed validation. |

All 17 expected decisions matched. Testing only for an open link and a living loader would
have reported seven false positives in this matrix.

See `systemd.scheduler(5)` for the provider protocol.
