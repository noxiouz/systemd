/* SPDX-License-Identifier: GPL-2.0 */
#include "vmlinux.h"
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char _license[] SEC("license") = "GPL";

struct sample_key {
        u32 pid;
        u32 padding;
        u64 node;
};

struct {
        __uint(type, BPF_MAP_TYPE_HASH);
        __uint(max_entries, 32);
        __type(key, struct sample_key);
        __type(value, u64);
} dispatches SEC(".maps");

/* Count only real sched_ext dispatches of the named test tasks. The kernfs ID
 * joins a task to its particular live sysfs directory even when two providers
 * publish the same ops name. This observer is specific to the reference kernel. */
SEC("fentry/set_next_task_scx")
int BPF_PROG(record_dispatch, struct rq *rq, struct task_struct *p, bool first)
{
        char comm[16];
        BPF_CORE_READ_STR_INTO(&comm, p, comm);
        if (__builtin_memcmp(comm, "scx05-probe", sizeof("scx05-probe")) != 0)
                return 0;

        struct sample_key key = {
                .pid = BPF_CORE_READ(p, pid),
                .node = BPF_CORE_READ(p, scx.sched, kobj.sd, id),
        };
        u64 *count = bpf_map_lookup_elem(&dispatches, &key), one = 1;
        if (count)
                __sync_fetch_and_add(count, 1);
        else
                bpf_map_update_elem(&dispatches, &key, &one, BPF_NOEXIST);
        return 0;
}
