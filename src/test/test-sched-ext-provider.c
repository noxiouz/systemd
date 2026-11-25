/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "tests.h"

#if HAVE_TEST_SCHED_EXT
#include <bpf/bpf.h>
#include <bpf/btf.h>
#include <bpf/libbpf.h>
#include <linux/btf.h>
#include <poll.h>
#include <sys/stat.h>

#include "alloc-util.h"
#include "dlfcn-util.h"
#include "iovec-util.h"
#include "log.h"
#include "parse-util.h"
#include "sched-ext-child.bpf.skel.h"
#include "sched-ext-cid.bpf.skel.h"
#include "sched-ext-root.bpf.skel.h"
#include "sched-ext-test.h"
#include "socket-util.h"
#include "string-util.h"

static DLSYM_PROTOTYPE(bpf_link__destroy) = NULL;
static DLSYM_PROTOTYPE(bpf_link__fd) = NULL;
static DLSYM_PROTOTYPE(bpf_map__attach_struct_ops) = NULL;
static DLSYM_PROTOTYPE(bpf_map__fd) = NULL;
static DLSYM_PROTOTYPE(bpf_map__initial_value) = NULL;
static DLSYM_PROTOTYPE(bpf_map_lookup_elem) = NULL;
static DLSYM_PROTOTYPE(bpf_object__close) = NULL;
static DLSYM_PROTOTYPE(bpf_object__find_map_by_name) = NULL;
static DLSYM_PROTOTYPE(bpf_object__load) = NULL;
static DLSYM_PROTOTYPE(bpf_object__open_mem) = NULL;
static DLSYM_PROTOTYPE(btf__find_by_name_kind) = NULL;
static DLSYM_PROTOTYPE(btf__free) = NULL;
static DLSYM_PROTOTYPE(btf__load_vmlinux_btf) = NULL;

DEFINE_TRIVIAL_CLEANUP_FUNC_FULL(struct bpf_object*, sym_bpf_object__close, NULL);
DEFINE_TRIVIAL_CLEANUP_FUNC_FULL(struct bpf_link*, sym_bpf_link__destroy, NULL);
DEFINE_TRIVIAL_CLEANUP_FUNC_FULL(struct btf*, sym_btf__free, NULL);

static int serve(const char *invocation, uint64_t target, int link, int record) {
        const char *event = "attach";
        bool sent_exit = false;

        for (;;) {
                struct scx_test_exit status;
                if (sym_bpf_map_lookup_elem(record, &(uint32_t){ 0 }, &status) < 0)
                        return -errno;

                if (!sent_exit && status.kind != 0 && !streq_ptr(event, "attach"))
                        event = "exit";

                if (event) {
                        _cleanup_free_ char *message = NULL;
                        if (asprintf(&message, "%s %s %" PRIu64 " 0 %d %lld %s", event, invocation, target,
                                     status.kind, status.code, status.message) < 0)
                                return -ENOMEM;

                        struct iovec iov = IOVEC_MAKE_STRING(message);
                        ssize_t n = send_one_fd_iov(STDIN_FILENO, streq(event, "attach") ? link : -EBADF, &iov, 1, 0);
                        if (n < 0)
                                return (int) n;
                        sent_exit |= streq(event, "exit");
                        event = NULL;
                }

                struct pollfd pfd = { .fd = STDIN_FILENO, .events = POLLIN };
                int r = poll(&pfd, 1, 100);
                if (r < 0)
                        return -errno;
                if (r == 0)
                        continue;

                char c;
                ssize_t n = read(STDIN_FILENO, &c, 1);
                if (n < 0)
                        return -errno;
                if (n == 0 || c == 'Q')
                        return 0;
                if (c != 'S')
                        return -EBADMSG;
                event = "status";
        }
}

static int run(int argc, char **argv) {
        if (access("/sys/kernel/sched_ext/state", F_OK) < 0)
                return errno == ENOENT ? log_tests_skipped("kernel lacks sched_ext") : -errno;

        _cleanup_(dlclosep) void *dl = NULL;
        int r = dlopen_many_sym_or_warn(
                        &dl, "libbpf.so.1", LOG_DEBUG,
                        DLSYM_ARG(bpf_link__destroy),
                        DLSYM_ARG(bpf_link__fd),
                        DLSYM_ARG(bpf_map__attach_struct_ops),
                        DLSYM_ARG(bpf_map__fd),
                        DLSYM_ARG(bpf_map__initial_value),
                        DLSYM_ARG(bpf_map_lookup_elem),
                        DLSYM_ARG(bpf_object__close),
                        DLSYM_ARG(bpf_object__find_map_by_name),
                        DLSYM_ARG(bpf_object__load),
                        DLSYM_ARG(bpf_object__open_mem),
                        DLSYM_ARG(btf__find_by_name_kind),
                        DLSYM_ARG(btf__free),
                        DLSYM_ARG(btf__load_vmlinux_btf));
        if (r == -EOPNOTSUPP)
                return log_tests_skipped("libbpf.so.1 unavailable");
        if (r < 0)
                return r;

        _cleanup_(sym_btf__freep) struct btf *btf = sym_btf__load_vmlinux_btf();
        if (!btf)
                return errno == ENOENT ? log_tests_skipped("kernel BTF unavailable") : -errno;

        bool hierarchy = sym_btf__find_by_name_kind(btf, "scx_bpf_sub_dispatch", BTF_KIND_FUNC) > 0;
        if (argc == 2 && streq(argv[1], "--probe"))
                return 0;
        if (argc == 2 && streq(argv[1], "--probe-hierarchy"))
                return hierarchy ? 0 : log_tests_skipped("kernel lacks sub-schedulers");
        if (argc != 4 || !STR_IN_SET(argv[3], "child", "root"))
                return -EINVAL;

        bool child = streq(argv[3], "child");
        if (child && !hierarchy)
                return -EOPNOTSUPP;

        int fd = parse_fd(argv[2]);
        if (fd < 0)
                return fd;
        struct stat st;
        if (fstat(fd, &st) < 0)
                return -errno;

        size_t size;
        const void *elf;
        if (!hierarchy)
                elf = sched_ext_root_bpf__elf_bytes(&size);
        else if (sym_btf__find_by_name_kind(btf, "sched_ext_ops_cid", BTF_KIND_STRUCT) > 0)
                elf = sched_ext_cid_bpf__elf_bytes(&size);
        else
                elf = sched_ext_child_bpf__elf_bytes(&size);
        _cleanup_(sym_bpf_object__closep) struct bpf_object *obj = sym_bpf_object__open_mem(elf, size, NULL);
        if (!obj)
                return -errno;

        struct bpf_map *ops = sym_bpf_object__find_map_by_name(obj, "ops"),
                       *record = sym_bpf_object__find_map_by_name(obj, "exit_record");
        if (!ops || !record)
                return -EBADMSG;
        if (child) {
                /* sub_cgroup_id is the first field in both of our hierarchy objects. libbpf relocates it
                 * into the running kernel's struct_ops layout on load. */
                uint64_t *target = sym_bpf_map__initial_value(ops, &size);
                if (!target || size < sizeof(*target))
                        return -EINVAL;
                *target = st.st_ino;
        }
        r = sym_bpf_object__load(obj);
        if (r < 0)
                return r;

        _cleanup_(sym_bpf_link__destroyp) struct bpf_link *link = sym_bpf_map__attach_struct_ops(ops);
        if (!link)
                return -errno;
        return serve(argv[1], st.st_ino, sym_bpf_link__fd(link), sym_bpf_map__fd(record));
}
#endif

int main(int argc, char **argv) {
#if HAVE_TEST_SCHED_EXT
        int r = run(argc, argv);
        if (r < 0) {
                log_error_errno(r, "sched_ext test provider failed: %m");
                return EXIT_FAILURE;
        }
        return r;
#else
        return log_tests_skipped("Built without the sched_ext test BPF objects");
#endif
}
