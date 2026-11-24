/* SPDX-License-Identifier: GPL-2.0 */
#include <scx/common.h>
#include "fifo.skel.h"
#include "provider.h"

int run_provider(const char *generation, ino_t target, const char *role) {
        if (strcmp(role, "child") != 0)
                return EXIT_FAILURE;

        struct fifo *skel = SCX_OPS_CID_OPEN(fifo_ops, fifo);
        skel->struct_ops.fifo_ops->sub_cgroup_id = target;
        SCX_OPS_LOAD(skel, fifo_ops, fifo, uei);
        __SCX_OPS_DISABLE_AUTOATTACH(skel, fifo_ops);
        if (fifo__attach(skel) < 0)
                return EXIT_FAILURE;

        struct bpf_link *link = bpf_map__attach_struct_ops(skel->maps.fifo_ops);
        int attach_errno = link ? 0 : errno;
        int r = serve_provider(generation, target, &skel->data->uei, link, attach_errno);
        bpf_link__destroy(link);
        fifo__destroy(skel);
        return r;
}
