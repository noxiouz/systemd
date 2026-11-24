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
#include "provider.h"

/* Each connection and BPF object belong to exactly one start. Keep servicing queries after ops.exit
 * so the manager can distinguish a disabled scheduler from a hung or dead loader. */
static int report(const char *event, const char *generation, ino_t ino, int attach_errno,
                  struct user_exit_info *uei, int link_fd) {
        int kind = __atomic_load_n(&uei->kind, __ATOMIC_ACQUIRE);
        char buf[1536];
        int n = snprintf(buf, sizeof(buf), "%s %s %ju %d %d %lld %s", event, generation, (uintmax_t) ino,
                         attach_errno, kind, (long long) uei->exit_code,
                         kind ? uei->msg : "");
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

        return run_provider(argv[1], st.st_ino, argv[3]);
}

int serve_provider(const char *generation, ino_t target, struct user_exit_info *uei,
                   struct bpf_link *link, int attach_errno) {
        int r = report("attach", generation, target, attach_errno, uei, link ? bpf_link__fd(link) : -1);
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
                        r = report("status", generation, target, attach_errno, uei, -1);
                }
                if (r >= 0 && !notified && __atomic_load_n(&uei->kind, __ATOMIC_ACQUIRE)) {
                        r = report("exit", generation, target, attach_errno, uei, -1);
                        notified = true;
                }
        }
        return r < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
}
