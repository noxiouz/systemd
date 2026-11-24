/* SPDX-License-Identifier: GPL-2.0 */

#include <scx/common.h>
#include "scx_simple.bpf.skel.h"
#include "provider.h"

/* The CPU-based upstream policy runs on the older root-only control kernel. */
int run_provider(const char *generation, ino_t target, const char *role) {
        if (strcmp(role, "root") != 0)
                return EXIT_FAILURE;

        struct scx_simple *skel = SCX_OPS_OPEN(simple_ops, scx_simple);
        SCX_OPS_LOAD(skel, simple_ops, scx_simple, uei);
        __SCX_OPS_DISABLE_AUTOATTACH(skel, simple_ops);
        if (scx_simple__attach(skel) < 0)
                return EXIT_FAILURE;

        struct bpf_link *link = bpf_map__attach_struct_ops(skel->maps.simple_ops);
        int attach_errno = link ? 0 : errno;
        int r = serve_provider(generation, target, &skel->data->uei, link, attach_errno);
        bpf_link__destroy(link);
        scx_simple__destroy(skel);
        return r;
}
