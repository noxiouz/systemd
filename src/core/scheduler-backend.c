/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include <fcntl.h>
#include <linux/bpf.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "sd-event.h"

#include "alloc-util.h"
#include "cgroup-util.h"
#include "errno-util.h"
#include "extract-word.h"
#include "fd-util.h"
#include "fileio.h"
#include "fs-util.h"
#include "iovec-util.h"
#include "job.h"
#include "manager.h"
#include "parse-util.h"
#include "pidref.h"
#include "scheduler.h"
#include "serialize.h"
#include "slice.h"
#include "socket-util.h"
#include "special.h"
#include "stat-util.h"
#include "stdio-util.h"
#include "string-util.h"
#include "strv.h"
#include "virt.h"

#define SCX_ROOT "/sys/kernel/sched_ext/root"
#define SCX_STATUS_INTERVAL (250 * USEC_PER_MSEC)
#define SCX_STATUS_TIMEOUT (5 * USEC_PER_SEC)

struct SchedulerRuntime {
        PidRef loader, detacher;
        int channel, target, link, node;
        uint64_t target_id;
        char *target_path, *node_path;
        sd_event_source *io, *timer;
        usec_t deadline;
        bool awaiting_status, reconciling;
};

static int dispatch_timer(sd_event_source *source, usec_t usec, void *userdata);

static bool backend_owns_node(Scheduler *s, const char *path) {
        SchedulerRuntime *rt = s->runtime;

        return rt && rt->node >= 0 &&
                inode_same_at(rt->node, /* filea= */ NULL, AT_FDCWD, path, AT_EMPTY_PATH) > 0;
}

bool scheduler_link_is_live(Scheduler *s) {
        SchedulerRuntime *rt = s->runtime;

        return rt && rt->node >= 0 && rt->link >= 0 &&
                inode_same_at(rt->target, /* filea= */ NULL, AT_FDCWD, rt->target_path, AT_EMPTY_PATH) > 0 &&
                backend_owns_node(s, rt->node_path) &&
                faccessat(rt->node, "ops", F_OK, 0) >= 0;
}

/* Follow actual target IDs from root to leaf. A foreign attachment can exist on a slice with no
 * declaration, and another instance with the same algorithm name can replace a managed one. */
static int find_scheduler_node(Slice *s, char **node, Slice **owner) {
        Unit *u = UNIT(s), *parent = UNIT_GET_SLICE(u);
        int r;

        assert(node);
        assert(owner);

        if (parent) {
                r = find_scheduler_node(SLICE(parent), node, owner);
                if (r < 0)
                        return r;

                CGroupRuntime *crt = unit_get_cgroup_runtime(u);
                if (!crt || !crt->cgroup_path)
                        return -ENXIO;

                _cleanup_free_ char *cg = NULL, *child = NULL;
                r = cg_get_path(crt->cgroup_path, /* suffix= */ NULL, &cg);
                if (r < 0)
                        return r;

                struct stat st;
                if (stat(cg, &st) < 0)
                        return -errno;
                if (asprintf(&child, "%s/sub/sub-%" PRIu64, *node, (uint64_t) st.st_ino) < 0)
                        return -ENOMEM;
                if (access(child, F_OK) < 0)
                        return errno == ENOENT ? 0 : -errno; /* Inherits the actual parent. */

                free_and_replace(*node, child);
        } else {
                if (!unit_has_name(u, SPECIAL_ROOT_SLICE))
                        return -ENXIO;
                *node = strdup(SCX_ROOT);
                if (!*node)
                        return -ENOMEM;
                if (access(*node, F_OK) < 0)
                        return -errno;
        }

        *owner = s;
        return 0;
}

int slice_get_effective_scheduler(Slice *slice, Unit **ret) {
        _cleanup_free_ char *node = NULL;
        Slice *owner;
        int r;

        assert(ret);

        r = find_scheduler_node(slice, &node, &owner);
        if (r < 0)
                return r;

        LIST_FOREACH(units_by_type, u, UNIT(slice)->manager->units_by_type[UNIT_SCHEDULER])
                if (backend_owns_node(SCHEDULER(u), node)) {
                        if (!scheduler_is_attached(SCHEDULER(u)))
                                return -EAGAIN;

                        *ret = u;
                        return 0;
                }

        Scheduler *candidate = SCHEDULER(UNIT_DEREF(owner->scheduler));
        if (candidate && candidate->state == SCHEDULER_ATTACHING)
                return -EAGAIN;

        *ret = NULL;
        return 0;
}

void scheduler_runtime_done(Scheduler *s) {
        SchedulerRuntime *rt = TAKE_PTR(s->runtime);

        if (!rt)
                return;

        sd_event_source_disable_unref(rt->io);
        sd_event_source_disable_unref(rt->timer);
        safe_close(rt->channel);
        safe_close(rt->target);
        safe_close(rt->link);
        safe_close(rt->node);
        unit_unwatch_pidref_done(UNIT(s), &rt->loader);
        unit_unwatch_pidref_done(UNIT(s), &rt->detacher);
        free(rt->target_path);
        free(rt->node_path);
        free(rt);
        s->link_id = 0;
}

static int detach_link(Scheduler *s) {
        /* struct_ops unregistration can wait for the kernel. Detach only the link returned by this
         * invocation. BPF_LINK_DETACH also unregisters it when other processes retain duplicate FDs. */
        SchedulerRuntime *rt = ASSERT_PTR(s->runtime);
        _cleanup_(pidref_done) PidRef child = PIDREF_NULL;
        int r;

        r = unit_fork_helper_process(UNIT(s), "(sd-scx-detach)", /* into_cgroup= */ false, &child);
        if (r < 0)
                return r;
        if (r == 0) {
#ifdef __NR_bpf
                union bpf_attr attr = { .link_detach.link_fd = rt->link };
                if (syscall(__NR_bpf, BPF_LINK_DETACH, &attr, sizeof(attr.link_detach)) >= 0 || errno == ENOLINK)
                        _exit(EXIT_SUCCESS);
                int error = errno;
                log_unit_error_errno(UNIT(s), error, "BPF_LINK_DETACH of owned link %u failed: %m", s->link_id);
                _exit(error > 0 && error < 255 ? error : EXIT_FAILURE);
#endif
                _exit(EXIT_FAILURE);
        }

        r = unit_watch_pidref(UNIT(s), &child, /* exclusive= */ true);
        if (r < 0)
                return r;
        rt->detacher = TAKE_PIDREF(child);
        return 0;
}

static int receive_packet(Scheduler *s, int fd);
static void arm_stop_timer(Scheduler *s, bool retry);

static void finish_stop(Scheduler *s) {
        SchedulerRuntime *rt = s->runtime;

        if (rt) {
                if (pidref_is_set(&rt->loader) || pidref_is_set(&rt->detacher))
                        return;

                if (s->cgroup_runtime && s->cgroup_runtime->cgroup_path) {
                        int r = unit_cgroup_is_empty(UNIT(s));
                        if (r < 0)
                                return; /* Retry at the stop deadline. */
                        if (r == 0) {
                                /* The loader has exited. Reap its remaining workers before reusing the
                                 * cgroup. The detach helper runs outside it. */
                                r = unit_kill_context(UNIT(s), KILL_KILL);
                                if (r < 0)
                                        s->result = log_unit_error_errno(UNIT(s), r, "Failed to kill scheduler workers: %m");
                                return; /* Wait for the cgroup empty notification. */
                        }
                }

                if (rt->channel >= 0) {
                        /* The provider has exited. Its first packet may still carry our link, even
                         * when stop or SIGCHLD was dispatched before the attach notification. */
                        if (rt->link < 0)
                                (void) receive_packet(s, rt->channel);
                        rt->channel = safe_close(rt->channel);
                }
                if (rt->link >= 0) {
                        int r = detach_link(s);
                        if (r < 0) {
                                log_unit_debug_errno(UNIT(s), r, "Cannot start scheduler detach, retrying: %m");
                                arm_stop_timer(s, /* retry= */ true);
                        }
                        return;
                }
        }

        scheduler_runtime_done(s);
        scheduler_set_state(s, s->result < 0 ? SCHEDULER_FAILED : SCHEDULER_DEAD);
}

static void stop_timeout(Scheduler *s, int error) {
        Unit *u = UNIT(s);

        /* The helper may be blocked in the kernel. Keep it and the link supervised until detach
         * finishes, but complete the caller's job at its deadline. */
        if (s->result >= 0)
                s->result = log_unit_error_errno(u, error, "Scheduler teardown did not finish: %m");
        if (u->job)
                job_finish_and_invalidate(u->job, JOB_TIMEOUT, /* recursive= */ true, /* already= */ false);
        (void) unit_kill_context(u, KILL_KILL);
}

static void arm_stop_timer(Scheduler *s, bool retry) {
        SchedulerRuntime *rt = ASSERT_PTR(s->runtime);
        usec_t when = rt->deadline;
        int r;

        if (retry)
                when = MIN(when, usec_add(now(CLOCK_MONOTONIC), USEC_PER_SEC));
        r = unit_arm_timer(UNIT(s), &rt->timer, /* relative= */ false, when, dispatch_timer);
        if (r < 0)
                stop_timeout(s, r);
}

void scheduler_detach(Scheduler *s, bool initiate) {
        SchedulerRuntime *rt = s->runtime;

        if (!rt || !initiate)
                return finish_stop(s);

        rt->io = sd_event_source_disable_unref(rt->io);
        if (rt->channel >= 0) {
                (void) send(rt->channel, "Q", 1, MSG_DONTWAIT|MSG_NOSIGNAL);
                (void) shutdown(rt->channel, SHUT_WR);
        }

        rt->deadline = usec_add(now(CLOCK_MONOTONIC), UNIT(s)->manager->defaults.timeout_stop_usec);
        arm_stop_timer(s, /* retry= */ false);
        finish_stop(s);
}

static void backend_fail(Scheduler *s, int error, const char *reason) {
        s->result = log_unit_error_errno(UNIT(s), error, "%s: %m", reason);
        (void) strdup_to(&s->failure_reason, reason);
        scheduler_set_state(s, SCHEDULER_DETACHING);
        scheduler_detach(s, /* initiate= */ true);
}

void scheduler_sigchld(Unit *u, pid_t pid, int code, int status) {
        Scheduler *s = SCHEDULER(u);
        SchedulerRuntime *rt = s->runtime;

        if (!rt)
                return;

        if (rt->loader.pid == pid) {
                unit_unwatch_pidref_done(UNIT(s), &rt->loader);
                if (s->state != SCHEDULER_DETACHING)
                        return backend_fail(s, -ECHILD, "Scheduler loader exited");
        } else if (rt->detacher.pid == pid) {
                unit_unwatch_pidref_done(UNIT(s), &rt->detacher);
                if (code != CLD_EXITED || status != EXIT_SUCCESS) {
                        log_unit_debug_errno(UNIT(s), code == CLD_EXITED ? status : SYNTHETIC_ERRNO(EIO),
                                             "Kernel link detach failed, retrying: %m");
                        arm_stop_timer(s, /* retry= */ true);
                        return;
                }
                rt->link = safe_close(rt->link);
        } else
                return;

        finish_stop(s);
}

static int query_status(Scheduler *s) {
        SchedulerRuntime *rt = ASSERT_PTR(s->runtime);
        if (send(rt->channel, "S", 1, MSG_DONTWAIT|MSG_NOSIGNAL) < 0)
                return -errno;
        rt->awaiting_status = true;
        /* TimeoutStartSec= applies only to activation. Begin the live response deadline when
         * a query is actually sent, preserving a shorter window for lost-node reconciliation. */
        if (s->state == SCHEDULER_ATTACHED)
                rt->deadline = MIN(rt->deadline, usec_add(now(CLOCK_MONOTONIC), SCX_STATUS_TIMEOUT));
        return 0;
}

static int dispatch_timer(sd_event_source *source, usec_t usec, void *userdata) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(userdata));
        SchedulerRuntime *rt = ASSERT_PTR(s->runtime);
        int r;

        if (s->state == SCHEDULER_DETACHING) {
                if (now(CLOCK_MONOTONIC) >= rt->deadline) {
                        stop_timeout(s, -ETIMEDOUT);
                        /* New jobs can arrive while draining an uninterruptible helper. Bound those too. */
                        rt->deadline = usec_add(now(CLOCK_MONOTONIC), UNIT(s)->manager->defaults.timeout_stop_usec);
                }
                arm_stop_timer(s, /* retry= */ false);
                finish_stop(s);
                return 0;
        }

        if (now(CLOCK_MONOTONIC) >= rt->deadline) {
                backend_fail(s, -ETIMEDOUT, "Scheduler confirmation timed out");
                return 0;
        }

        if (rt->link >= 0 && !rt->awaiting_status) {
                r = query_status(s);
                if (r < 0) {
                        backend_fail(s, r, "Cannot query scheduler loader");
                        return 0;
                }
        }

        r = unit_arm_timer(UNIT(s), &rt->timer, /* relative= */ true, SCX_STATUS_INTERVAL, dispatch_timer);
        if (r < 0)
                backend_fail(s, r, "Cannot arm scheduler timeout");
        return 0;
}

static int verify_link_fd(int fd, uint32_t *ret_id) {
        char path[STRLEN("/proc/self/fdinfo/") + DECIMAL_STR_MAX(int)];
        _cleanup_free_ char *type = NULL, *id = NULL;
        int r;

        xsprintf(path, "/proc/self/fdinfo/%i", fd);
        r = get_proc_field(path, "link_type", &type);
        if (r < 0)
                return r;
        if (!streq(type, "struct_ops"))
                return -EBADFD;
        r = get_proc_field(path, "link_id", &id);
        if (r < 0)
                return r;
        return safe_atou32(id, ret_id);
}

static int process_packet(Scheduler *s, const char *packet, int *link) {
        _cleanup_free_ char *event = NULL, *generation = NULL, *target = NULL, *error = NULL, *kind = NULL, *code = NULL;
        SchedulerRuntime *rt = ASSERT_PTR(s->runtime);
        int err, exit_kind, r;
        uint64_t target_id;
        int64_t exit_code;

        assert(link);

        r = extract_many_words(&packet, " ", EXTRACT_DONT_COALESCE_SEPARATORS|EXTRACT_RETAIN_ESCAPE,
                               &event, &generation, &target, &error, &kind, &code);
        if (r != 6 || safe_atou64(target, &target_id) < 0 ||
            safe_atoi(error, &err) < 0 || err < 0 || err > ERRNO_MAX ||
            safe_atoi(kind, &exit_kind) < 0 || safe_atoi64(code, &exit_code) < 0)
                return -EBADMSG;
        if (!streq(generation, UNIT(s)->invocation_id_string))
                return -ESTALE;
        if (target_id != rt->target_id)
                return -EXDEV;

        if (streq(event, "attach")) {
                if (rt->link >= 0 || err != 0)
                        return err ? -err : -EBADMSG;
                if (*link < 0)
                        return -EBADFD;
                r = verify_link_fd(*link, &s->link_id);
                if (r < 0)
                        return r;
                rt->link = TAKE_FD(*link);
                if (s->state == SCHEDULER_DETACHING)
                        return 0;
        } else if (*link >= 0 || rt->link < 0 || !STR_IN_SET(event, "status", "exit"))
                return -EBADMSG;

        if (err || exit_kind || streq(event, "exit")) {
                _cleanup_free_ char *reason = NULL;
                if (asprintf(&reason, "Scheduler terminal record (kind=%d, code=%" PRIi64 ")%s%s",
                             exit_kind, exit_code, isempty(packet) ? "" : ": ", strempty(packet)) >= 0)
                        free_and_replace(s->failure_reason, reason);
                return err ? -err : -ECANCELED;
        }

        if (streq(event, "attach")) {
                rt->node = xopenat(AT_FDCWD, rt->node_path, O_PATH|O_DIRECTORY|O_NOFOLLOW);
                if (rt->node < 0)
                        return rt->node;

                /* A directory may have appeared during a subsequently vetoed attachment. Ask the
                 * provider for the durable exit record after it has returned its actual link. */
                return query_status(s);
        }

        if (!rt->awaiting_status)
                return -EBADMSG;
        rt->awaiting_status = false;
        if (!scheduler_link_is_live(s)) {
                if (s->state != SCHEDULER_ATTACHED && !rt->reconciling)
                        return -ESTALE;

                /* New admissions already reject this node. Give ops.exit a bounded interval to
                 * report its durable terminal reason, even if the unsolicited exit packet was lost. */
                rt->deadline = MIN(rt->deadline, usec_add(now(CLOCK_MONOTONIC), USEC_PER_SEC));
                return 0;
        }
        for (Unit *p = UNIT_DEREF(s->runtime_parent); p; p = UNIT_DEREF(SCHEDULER(p)->runtime_parent))
                if (!scheduler_is_attached(SCHEDULER(p))) {
                        /* Coldplug order is independent of hierarchy. A child with healthy
                         * evidence waits for its parent's outstanding confirmation. */
                        if (rt->reconciling && SCHEDULER(p)->state == SCHEDULER_ATTACHING)
                                return 0;
                        return -ENOLINK;
                }

        rt->reconciling = false;
        rt->deadline = USEC_INFINITY;
        scheduler_set_state(s, SCHEDULER_ATTACHED);
        return 0;
}

static int receive_packet(Scheduler *s, int fd) {
        _cleanup_close_ int link = -EBADF;
        char buf[1537];
        struct iovec iov = IOVEC_MAKE(buf, sizeof(buf) - 1);
        ssize_t n;

        n = receive_one_fd_iov(fd, &iov, 1, MSG_DONTWAIT, &link);
        if (n < 0)
                return (int) n;
        if (n == 0 || memchr(buf, 0, n))
                return -EBADMSG;

        buf[n] = 0;
        return process_packet(s, buf, &link);
}

static int dispatch_io(sd_event_source *source, int fd, uint32_t revents, void *userdata) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(userdata));

        int r = receive_packet(s, fd);
        if (r < 0 && r != -EAGAIN) {
                /* Preserve provider terminal diagnostics across GC of the invocation runtime. */
                _cleanup_free_ char *reason = TAKE_PTR(s->failure_reason);
                backend_fail(s, r, reason ?: "Invalid or failed scheduler confirmation");
        }
        return 0;
}

static int spawn_provider(Scheduler *s, int channel, int target, bool root, PidRef *ret) {
        _cleanup_(exec_params_shallow_clear) ExecParameters params = EXEC_PARAMETERS_INIT(EXEC_PASS_FDS|EXEC_APPLY_SANDBOXING);
        _cleanup_strv_free_ char **argv = NULL;
        ExecCommand command = *s->exec_start;
        int r;

        r = unit_prepare_exec(UNIT(s));
        if (r < 0)
                return r;
        r = unit_set_exec_params(UNIT(s), &params);
        if (r < 0)
                return r;

        params.stdin_fd = channel;
        params.fds = newdup(int, &target, 1);
        params.fd_names = strv_new("sched-ext-target");
        if (!params.fds || !params.fd_names)
                return -ENOMEM;
        params.n_stashed_fds = 1;

        argv = strv_copy(command.argv);
        if (!argv)
                return -ENOMEM;
        r = strv_extend_many(&argv, UNIT(s)->invocation_id_string, "3", root ? "root" : "child");
        if (r < 0)
                return r;
        command.argv = argv;

        return exec_spawn(UNIT(s), &command, &s->exec_context, &params, s->exec_runtime, &s->cgroup_context, ret);
}

static int ensure_runtime(Scheduler *s) {
        if (s->runtime)
                return 0;

        s->runtime = new(SchedulerRuntime, 1);
        if (!s->runtime)
                return -ENOMEM;
        *s->runtime = (SchedulerRuntime) {
                .loader = PIDREF_NULL,
                .detacher = PIDREF_NULL,
                .channel = -EBADF,
                .node = -EBADF,
                .link = -EBADF,
                .target = -EBADF,
                .deadline = USEC_INFINITY,
        };
        return 0;
}

int scheduler_attach(Scheduler *s, int target_fd) {
        Unit *u = UNIT(s), *target = UNIT_DEREF(s->target_slice), *parent = UNIT_DEREF(s->runtime_parent);
        _cleanup_close_pair_ int pair[2] = EBADF_PAIR;
        _cleanup_free_ char *node_path = NULL, *target_path = NULL;
        struct stat st;
        int r;

        assert(!s->runtime);

        if (!MANAGER_IS_SYSTEM(u->manager) || detect_container() != VIRTUALIZATION_NONE)
                return -EOPNOTSUPP;
        if (fstat(target_fd, &st) < 0)
                return -errno;

        if (parent) {
                Scheduler *p = SCHEDULER(parent);
                if (!scheduler_is_attached(p))
                        return -ENOLINK;
                if (faccessat(p->runtime->node, "sub", F_OK, 0) < 0)
                        return errno == ENOENT ? -EOPNOTSUPP : -errno;
                if (asprintf(&node_path, "%s/sub/sub-%" PRIu64, p->runtime->node_path, (uint64_t) st.st_ino) < 0)
                        return -ENOMEM;
        } else {
                /* A nested manager's root cgroup cannot represent the host attachment. */
                r = inode_same_at(target_fd, /* filea= */ NULL, AT_FDCWD, "/sys/fs/cgroup", AT_EMPTY_PATH);
                if (r <= 0)
                        return r < 0 ? r : -EOPNOTSUPP;

                node_path = strdup(SCX_ROOT);
                if (!node_path)
                        return -ENOMEM;
        }

        if (access(node_path, F_OK) >= 0)
                return -EBUSY;
        if (errno != ENOENT)
                return -errno;
        r = cg_get_path(ASSERT_PTR(unit_get_cgroup_runtime(target))->cgroup_path, /* suffix= */ NULL, &target_path);
        if (r < 0)
                return r;

        if (socketpair(AF_UNIX, SOCK_SEQPACKET|SOCK_CLOEXEC, 0, pair) < 0)
                return -errno;
        r = fd_nonblock(pair[0], /* nonblock= */ true);
        if (r < 0)
                return r;

        r = ensure_runtime(s);
        if (r < 0)
                return r;
        SchedulerRuntime *rt = s->runtime;
        rt->channel = TAKE_FD(pair[0]);
        rt->target_id = st.st_ino;
        rt->target_path = TAKE_PTR(target_path);
        rt->node_path = TAKE_PTR(node_path);
        rt->deadline = usec_add(now(CLOCK_MONOTONIC), s->timeout_usec);

        rt->target = fcntl(target_fd, F_DUPFD_CLOEXEC, 3);
        if (rt->target < 0) {
                r = -errno;
                goto fail;
        }
        r = sd_event_add_io(u->manager->event, &rt->io, rt->channel, EPOLLIN, dispatch_io, s);
        if (r < 0)
                goto fail;
        r = unit_arm_timer(u, &rt->timer, /* relative= */ true, MIN(s->timeout_usec, SCX_STATUS_INTERVAL), dispatch_timer);
        if (r < 0)
                goto fail;
        r = spawn_provider(s, pair[1], rt->target, !parent, &rt->loader);
        if (r < 0)
                goto fail;
        r = unit_watch_pidref(u, &rt->loader, /* exclusive= */ true);
        if (r < 0) {
                (void) pidref_kill(&rt->loader, SIGKILL);
                goto fail;
        }
        return 0;

fail:
        scheduler_runtime_done(s);
        return r;
}

int scheduler_runtime_serialize(Scheduler *s, FILE *f, FDSet *fds) {
        SchedulerRuntime *rt = s->runtime;
        if (!rt)
                return 0;

        /* The socket preserves both its pending packets and their SCM_RIGHTS descriptors.
         * Duplicating the link and both directory pins keeps the exact invocation reachable. */
        int r = 0;
        RET_GATHER(r, serialize_fd(f, fds, "scx-channel", rt->channel));
        RET_GATHER(r, serialize_fd(f, fds, "scx-target", rt->target));
        RET_GATHER(r, serialize_fd(f, fds, "scx-node", rt->node));
        RET_GATHER(r, serialize_fd(f, fds, "scx-link", rt->link));
        RET_GATHER(r, serialize_pidref(f, fds, "scx-loader", &rt->loader));
        RET_GATHER(r, serialize_pidref(f, fds, "scx-detacher", &rt->detacher));
        RET_GATHER(r, serialize_item(f, "scx-target-path", rt->target_path));
        RET_GATHER(r, serialize_item(f, "scx-node-path", rt->node_path));
        RET_GATHER(r, serialize_usec(f, "scx-deadline", rt->deadline));
        RET_GATHER(r, serialize_bool(f, "scx-awaiting-status", rt->awaiting_status));
        if (UNIT_ISSET(s->runtime_parent))
                RET_GATHER(r, serialize_item(f, "scx-parent", UNIT_DEREF(s->runtime_parent)->id));
        return r;
}

int scheduler_runtime_deserialize(Scheduler *s, const char *key, const char *value, FDSet *fds) {
        if (!startswith(key, "scx-"))
                return 0;

        int r = ensure_runtime(s);
        if (r < 0)
                return r;
        SchedulerRuntime *rt = s->runtime;
        int *fd;

        if (streq(key, "scx-channel"))
                fd = &rt->channel;
        else if (streq(key, "scx-target"))
                fd = &rt->target;
        else if (streq(key, "scx-node"))
                fd = &rt->node;
        else if (streq(key, "scx-link"))
                fd = &rt->link;
        else if (streq(key, "scx-loader") && !pidref_is_set(&rt->loader))
                return deserialize_pidref(fds, value, &rt->loader);
        else if (streq(key, "scx-detacher") && !pidref_is_set(&rt->detacher))
                return deserialize_pidref(fds, value, &rt->detacher);
        else if (streq(key, "scx-target-path"))
                return strdup_to(&rt->target_path, value);
        else if (streq(key, "scx-node-path"))
                return strdup_to(&rt->node_path, value);
        else if (streq(key, "scx-deadline"))
                return deserialize_usec(value, &rt->deadline);
        else if (streq(key, "scx-awaiting-status")) {
                r = parse_boolean(value);
                if (r >= 0)
                        rt->awaiting_status = r;
                return r;
        } else if (streq(key, "scx-parent")) {
                Unit *parent;
                r = manager_load_unit(UNIT(s)->manager, value, /* path= */ NULL, NULL, &parent);
                if (r < 0)
                        return r;
                if (parent->type != UNIT_SCHEDULER || parent == UNIT(s))
                        return -EINVAL;
                unit_ref_set(&s->runtime_parent, UNIT(s), parent);
                return 0;
        } else
                return 0;

        if (*fd >= 0)
                return -EEXIST;
        *fd = deserialize_fd(fds, value);
        return *fd < 0 ? *fd : 0;
}

int scheduler_runtime_coldplug(Scheduler *s) {
        SchedulerRuntime *rt = s->runtime;
        Unit *u = UNIT(s), *parent = UNIT_DEREF(s->runtime_parent);
        struct stat st;
        int r;

        if (!rt) {
                r = -ENODATA;
                goto fail;
        }
        if (pidref_is_set(&rt->loader)) {
                r = unit_watch_pidref(u, &rt->loader, /* exclusive= */ true);
                if (r < 0)
                        goto fail;
        }
        if (pidref_is_set(&rt->detacher)) {
                r = unit_watch_pidref(u, &rt->detacher, /* exclusive= */ true);
                if (r < 0)
                        goto fail;
        }
        if (rt->link >= 0) {
                r = verify_link_fd(rt->link, &s->link_id);
                if (r < 0)
                        goto fail;
        }
        if (parent) {
                r = scheduler_bind_parent(s, parent);
                if (r < 0)
                        goto fail;
        }

        if (rt->target >= 0) {
                if (fstat(rt->target, &st) < 0) {
                        r = -errno;
                        goto fail;
                }
                rt->target_id = st.st_ino;
        }

        if (s->deserialized_state == SCHEDULER_DETACHING) {
                scheduler_set_state(s, SCHEDULER_DETACHING);
                arm_stop_timer(s, /* retry= */ false);
                finish_stop(s);
                return 0;
        }

        if (rt->channel < 0 || rt->target < 0 || !rt->node_path || !rt->target_path || !pidref_is_set(&rt->loader)) {
                r = -ENODATA;
                goto fail;
        }
        rt->reconciling = true;
        if (s->deserialized_state == SCHEDULER_ATTACHED)
                rt->deadline = MIN(rt->deadline, usec_add(now(CLOCK_MONOTONIC), SCX_STATUS_TIMEOUT));
        scheduler_set_state(s, SCHEDULER_ATTACHING);

        r = sd_event_add_io(u->manager->event, &rt->io, rt->channel, EPOLLIN, dispatch_io, s);
        if (r < 0)
                goto fail;
        if (rt->link >= 0 && !rt->awaiting_status) {
                r = query_status(s);
                if (r < 0)
                        goto fail;
        }
        r = unit_arm_timer(u, &rt->timer, /* relative= */ false, MIN(rt->deadline, usec_add(now(CLOCK_MONOTONIC), SCX_STATUS_INTERVAL)), dispatch_timer);
        if (r < 0)
                goto fail;
        return 0;

fail:
        backend_fail(s, r, "Cannot restore scheduler invocation");
        return r;
}
