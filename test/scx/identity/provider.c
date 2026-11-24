/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <inttypes.h>
#include <linux/magic.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <scx/common.h>
#include "scx_qmap.h"
#include "scx_qmap.bpf.skel.h"

/* Each connection and BPF object belong to exactly one start. Keep servicing queries after ops.exit
 * so the manager can distinguish a disabled scheduler from a hung or dead loader. */
static int report(const char *event, const char *generation, ino_t ino, int attach_errno,
                  struct scx_qmap *skel, int link_fd) {
        int kind = UEI_EXITED(skel, uei);
        char buf[1536];
        int n = snprintf(buf, sizeof(buf), "%s %s %ju %d %d %lld %s", event, generation, (uintmax_t) ino,
                         attach_errno, kind, (long long) skel->data->uei.exit_code,
                         kind ? skel->data->uei.msg : "");
        if (n < 0 || (size_t) n >= sizeof(buf))
                return -1;

        struct iovec iov = { .iov_base = buf, .iov_len = (size_t) n };
        struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1 };
        union {
                struct cmsghdr align;
                char bytes[CMSG_SPACE(sizeof(int))];
        } control = {};
        if (link_fd >= 0) {
                msg.msg_control = control.bytes;
                msg.msg_controllen = sizeof(control.bytes);
                struct cmsghdr *c = CMSG_FIRSTHDR(&msg);
                c->cmsg_level = SOL_SOCKET;
                c->cmsg_type = SCM_RIGHTS;
                c->cmsg_len = CMSG_LEN(sizeof(int));
                memcpy(CMSG_DATA(c), &link_fd, sizeof(int));
        }
        return sendmsg(STDIN_FILENO, &msg, MSG_NOSIGNAL) == n ? 0 : -1;
}

int main(int argc, char **argv) {
        struct stat st;
        struct statfs fs;
        int cgfd, type;
        socklen_t type_size = sizeof(type);
        char extra;
        if (argc != 4 || strlen(argv[1]) != 32 || strspn(argv[1], "0123456789abcdef") != 32 ||
            sscanf(argv[2], "%d%c", &cgfd, &extra) != 1 || cgfd < 0 ||
            fstat(cgfd, &st) < 0 || !S_ISDIR(st.st_mode) || fstatfs(cgfd, &fs) < 0 ||
            fs.f_type != CGROUP2_SUPER_MAGIC ||
            getsockopt(STDIN_FILENO, SOL_SOCKET, SO_TYPE, &type, &type_size) < 0 || type != SOCK_SEQPACKET)
                return EXIT_FAILURE;

        struct scx_qmap *skel = SCX_OPS_CID_OPEN(qmap_ops, scx_qmap);
        skel->rodata->slice_ns = __COMPAT_ENUM_OR_ZERO("scx_public_consts", "SCX_SLICE_DFL");
        skel->rodata->max_tasks = 16384;
        skel->rodata->round_robin_ns = 200000000;
        if (strcmp(argv[3], "child") == 0) {
                skel->struct_ops.qmap_ops->sub_cgroup_id = st.st_ino;
                skel->rodata->sub_cgroup_id = st.st_ino;
        } else if (strcmp(argv[3], "flat") == 0) {
                skel->struct_ops.qmap_ops->sub_attach = NULL;
                skel->struct_ops.qmap_ops->sub_detach = NULL;
        } else if (strcmp(argv[3], "root") != 0)
                return EXIT_FAILURE;

        SCX_OPS_LOAD(skel, qmap_ops, scx_qmap, uei);
        __SCX_OPS_DISABLE_AUTOATTACH(skel, qmap_ops);
        if (scx_qmap__attach(skel) < 0)
                return EXIT_FAILURE;

        struct bpf_link *link = bpf_map__attach_struct_ops(skel->maps.qmap_ops);
        int attach_errno = link ? 0 : errno;
        int r = report("attach", argv[1], st.st_ino, attach_errno, skel, link ? bpf_link__fd(link) : -1);
        bool notified = false;
        while (r >= 0) {
                struct pollfd pfd = { .fd = STDIN_FILENO, .events = POLLIN };
                r = poll(&pfd, 1, 50);
                if (r < 0)
                        break;
                if (r > 0) {
                        char cmd;
                        if (recv(STDIN_FILENO, &cmd, 1, 0) != 1 || cmd == 'Q')
                                break;
                        if (cmd != 'S')
                                return EXIT_FAILURE;
                        r = report("status", argv[1], st.st_ino, attach_errno, skel, -1);
                }
                if (r >= 0 && !notified && UEI_EXITED(skel, uei)) {
                        r = report("exit", argv[1], st.st_ino, attach_errno, skel, -1);
                        notified = true;
                }
        }
        bpf_link__destroy(link);
        scx_qmap__destroy(skel);
        return r < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
}
