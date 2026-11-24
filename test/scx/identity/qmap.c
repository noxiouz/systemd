/* SPDX-License-Identifier: GPL-2.0 */

#include <scx/common.h>
#include "scx_qmap.h"
#include "scx_qmap.bpf.skel.h"
#include "provider.h"

int run_provider(const char *generation, ino_t target, const char *role) {
        struct scx_qmap *skel = SCX_OPS_CID_OPEN(qmap_ops, scx_qmap);
        skel->rodata->slice_ns = __COMPAT_ENUM_OR_ZERO("scx_public_consts", "SCX_SLICE_DFL");
        skel->rodata->max_tasks = 16384;
        skel->rodata->round_robin_ns = 200000000;
        if (strcmp(role, "child") == 0) {
                skel->struct_ops.qmap_ops->sub_cgroup_id = target;
                skel->rodata->sub_cgroup_id = target;
        } else if (strcmp(role, "flat") == 0) {
                skel->struct_ops.qmap_ops->sub_attach = NULL;
                skel->struct_ops.qmap_ops->sub_detach = NULL;
        } else if (strcmp(role, "partial") == 0)
                skel->struct_ops.qmap_ops->flags |= SCX_OPS_SWITCH_PARTIAL;
        else if (strcmp(role, "root") != 0)
                return EXIT_FAILURE;

        SCX_OPS_LOAD(skel, qmap_ops, scx_qmap, uei);
        __SCX_OPS_DISABLE_AUTOATTACH(skel, qmap_ops);
        if (scx_qmap__attach(skel) < 0)
                return EXIT_FAILURE;

        struct bpf_link *link = bpf_map__attach_struct_ops(skel->maps.qmap_ops);
        int attach_errno = link ? 0 : errno;
        int r = serve_provider(generation, target, &skel->data->uei, link, attach_errno);
        bpf_link__destroy(link);
        scx_qmap__destroy(skel);
        return r;
}
