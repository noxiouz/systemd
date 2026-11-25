/* SPDX-License-Identifier: LGPL-2.1-or-later */
#pragma once

#include <linux/bpf.h>
#include <linux/errno.h>
#include <stdbool.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "sched-ext-test.h"

/* libbpf maps struct_ops members by name. Describe just the callbacks used by this test, so building the
 * fixture needs neither the running kernel's BTF nor the kernel sources. */
enum scx_exit_kind { SCX_EXIT_NONE };
struct scx_exit_info {
        enum scx_exit_kind kind;
        __s64 exit_code;
        char *msg;
};

struct {
        __uint(type, BPF_MAP_TYPE_ARRAY);
        __uint(max_entries, 1);
        __type(key, __u32);
        __type(value, struct scx_test_exit);
} exit_record SEC(".maps");

SEC("struct_ops/exit")
void BPF_PROG(on_exit, struct scx_exit_info *info) {
        struct scx_test_exit record = {
                .kind = BPF_CORE_READ(info, kind),
                .code = BPF_CORE_READ(info, exit_code),
        };
        __u32 key = 0;

        bpf_probe_read_kernel_str(record.message, sizeof(record.message), BPF_CORE_READ(info, msg));
        bpf_map_update_elem(&exit_record, &key, &record, BPF_ANY);
}

#if SCX_TEST_HIERARCHY
struct scx_sub_attach_args;
struct scx_sub_detach_args;
struct task_struct;

struct sched_ext_ops {
        __u64 sub_cgroup_id;
#if !SCX_TEST_CID
        void (*dispatch)(int, struct task_struct*);
        int (*sub_attach)(struct scx_sub_attach_args*);
        void (*sub_detach)(struct scx_sub_detach_args*);
        void (*exit)(struct scx_exit_info*);
        char name[128];
#endif
};

struct scx_sub_attach_args { struct sched_ext_ops *ops; };
struct scx_sub_detach_args { struct sched_ext_ops *ops; };

extern bool scx_bpf_sub_dispatch(__u64 cgroup_id) __ksym;

#if SCX_TEST_CID
struct scx_cmask {};
enum scx_cap_flags { SCX_CAP_ENQ_IMMED = 1, SCX_CAP_ENQ = 2 };
extern const void *scx_bpf_online_cmask(void) __ksym;
extern int scx_bpf_sub_grant(__u64 cgroup_id, __u64 caps, const struct scx_cmask *mask,
                             struct scx_cmask *denied) __ksym;

struct {
        __uint(type, BPF_MAP_TYPE_ARENA);
        __uint(map_flags, BPF_F_MMAPABLE);
        __uint(max_entries, 1 << 16);
} arena SEC(".maps");

struct sched_ext_ops_cid {
        __u64 sub_cgroup_id;
        void (*dispatch)(int, struct task_struct*);
        int (*sub_attach)(struct scx_sub_attach_args*);
        void (*sub_detach)(struct scx_sub_detach_args*);
        void (*exit)(struct scx_exit_info*);
        char name[128];
};
#endif

/* Direct children are registered under the kernel's scheduler enable/disable locks. Each instance uses
 * the built-in global FIFO for its tasks and visits its children's FIFOs from dispatch. */
static __u64 children[4];

SEC("struct_ops/sub_attach")
int BPF_PROG(attach_child, struct scx_sub_attach_args *args) {
        __u64 id = BPF_CORE_READ(args, ops, sub_cgroup_id);

        for (unsigned i = 0; i < 4; i++) {
                if (children[i] != 0)
                        continue;
#if SCX_TEST_CID
                /* CID-form schedulers need an arena and an explicit grant before child tasks can run.
                 * Share the online CIDs with every child, including a child accepting grandchildren. */
                asm volatile("" : : "r" (&arena));
                const void *online = scx_bpf_online_cmask();
                if (!online)
                        return -ENODEV;
                int r = scx_bpf_sub_grant(id, SCX_CAP_ENQ_IMMED | SCX_CAP_ENQ, online, NULL);
                if (r < 0)
                        return r;
#endif
                children[i] = id;
                return 0;
        }
        return -ENOSPC;
}

SEC("struct_ops/sub_detach")
void BPF_PROG(detach_child, struct scx_sub_detach_args *args) {
        __u64 id = BPF_CORE_READ(args, ops, sub_cgroup_id);

        for (unsigned i = 0; i < 4; i++)
                if (children[i] == id)
                        children[i] = 0;
}

SEC("struct_ops/dispatch")
void BPF_PROG(dispatch, int cid, struct task_struct *prev) {
        /* Vary the first child so a CPU-bound sibling does not monopolize dispatch. */
        unsigned first = bpf_get_prandom_u32();
        for (unsigned i = 0; i < 4; i++) {
                __u64 id = children[(first + i) % 4];
                if (id != 0 && scx_bpf_sub_dispatch(id))
                        break;
        }
}

#else
struct sched_ext_ops {
        void (*exit)(struct scx_exit_info*);
        char name[128];
};
#endif

SEC(".struct_ops.link")
#if SCX_TEST_CID
struct sched_ext_ops_cid ops = {
#else
struct sched_ext_ops ops = {
#endif
#if SCX_TEST_HIERARCHY
        .sub_attach = (void*) attach_child,
        .sub_detach = (void*) detach_child,
        .dispatch = (void*) dispatch,
#endif
        .exit = (void*) on_exit,
        .name = "systemd_test",
};

char _license[] SEC("license") = "GPL";
