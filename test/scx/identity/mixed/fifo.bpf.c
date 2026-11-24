/* SPDX-License-Identifier: GPL-2.0 */
#include <scx/common.bpf.h>

char _license[] SEC("license") = "GPL";
UEI_DEFINE(uei);

/* The kernel allocates its CID mask storage from the ops' arena. */
struct {
        __uint(type, BPF_MAP_TYPE_ARENA);
        __uint(map_flags, BPF_F_MMAPABLE);
        __uint(max_entries, 1 << 16);
} arena SEC(".maps");

/* One FIFO shared by the leaf's granted CIDs. Unlike qmap, every runnable task
 * enters the same queue, independently of its weight. */
#define FIFO_DSQ 0

s32 BPF_STRUCT_OPS_SLEEPABLE(fifo_init)
{
        asm volatile("" :: "r"(&arena));
        return scx_bpf_create_dsq(FIFO_DSQ, -1);
}

void BPF_STRUCT_OPS(fifo_enqueue, struct task_struct *p, u64 flags)
{
        scx_bpf_dsq_insert(p, FIFO_DSQ, SCX_SLICE_DFL, flags);
}

void BPF_STRUCT_OPS(fifo_dispatch, s32 cid, struct task_struct *prev)
{
        scx_bpf_dsq_move_to_local(FIFO_DSQ, 0);
}

void BPF_STRUCT_OPS(fifo_exit, struct scx_exit_info *ei)
{
        UEI_RECORD(uei, ei);
}

SCX_OPS_CID_DEFINE(fifo_ops,
                   .init = (void *) fifo_init,
                   .enqueue = (void *) fifo_enqueue,
                   .dispatch = (void *) fifo_dispatch,
                   .exit = (void *) fifo_exit,
                   .name = "fixture_fifo");
