/* SPDX-License-Identifier: GPL-2.0 */
#include "vmlinux.h"
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char _license[] SEC("license") = "GPL";

struct execution {
        u64 first_ns;
        u64 count;
        u32 pid;
        s32 level;
        u64 nodes[3];
};

struct {
        __uint(type, BPF_MAP_TYPE_HASH);
        __uint(max_entries, 128);
        __type(key, u64);
        __type(value, struct execution);
} executions SEC(".maps");

/* Restrict collection to this fixture's command copies. At exec the task already belongs to its
 * workload cgroup. Record the kernel's actual scheduler ancestry for this particular execution. */
SEC("tp_btf/sched_process_exec")
int BPF_PROG(command_executed, struct task_struct *task, pid_t old_pid, struct linux_binprm *bprm) {
        char name[16];
        struct dentry *dentry = BPF_CORE_READ(bprm, file, f_path.dentry);
        const unsigned char *filename = BPF_CORE_READ(dentry, d_name.name);
        if (bpf_probe_read_kernel_str(name, sizeof(name), filename) <= 0 ||
            __builtin_memcmp(name, "scx07-", sizeof("scx07-") - 1) != 0)
                return 0;

        u64 inode = BPF_CORE_READ(dentry, d_inode, i_ino);
        struct execution *old = bpf_map_lookup_elem(&executions, &inode);
        if (old) {
                __sync_fetch_and_add(&old->count, 1);
                return 0;
        }

        struct scx_sched *sch = BPF_CORE_READ(task, scx.sched);
        struct execution exec = {
                .first_ns = bpf_ktime_get_ns(),
                .count = 1,
                .pid = BPF_CORE_READ(task, pid),
                .level = sch ? BPF_CORE_READ(sch, level) : -1,
        };
        if (exec.level >= 0)
                exec.nodes[0] = BPF_CORE_READ(sch, ancestors[0], kobj.sd, id);
        if (exec.level >= 1)
                exec.nodes[1] = BPF_CORE_READ(sch, ancestors[1], kobj.sd, id);
        if (exec.level >= 2)
                exec.nodes[2] = BPF_CORE_READ(sch, ancestors[2], kobj.sd, id);
        bpf_map_update_elem(&executions, &inode, &exec, BPF_NOEXIST);
        return 0;
}
