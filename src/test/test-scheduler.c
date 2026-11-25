/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include <unistd.h>

#include "sd-bus.h"
#include "sd-event.h"

#include "alloc-util.h"
#include "build-path.h"
#include "cgroup-util.h"
#include "fd-util.h"
#include "manager.h"
#include "path-util.h"
#include "pidref.h"
#include "rm-rf.h"
#include "scheduler.h"
#include "service.h"
#include "slice.h"
#include "stat-util.h"
#include "strv.h"
#include "tests.h"

static char **attempts = NULL;
static Slice *foreign = NULL;

int scheduler_attach(Scheduler *s, int target_fd) {
        Unit *u = UNIT(s), *target = UNIT_DEREF(s->target_slice), *parent = UNIT_DEREF(s->runtime_parent);
        _cleanup_close_ int expected = cg_path_open(ASSERT_PTR(unit_get_cgroup_runtime(target))->cgroup_path);

        ASSERT_OK(expected);
        ASSERT_TRUE(fd_inode_same(target_fd, expected));
        ASSERT_EQ(unit_active_state(target), UNIT_ACTIVE);
        ASSERT_PTR_EQ(parent, unit_get_start_scheduler(u));
        if (parent)
                ASSERT_EQ(unit_active_state(parent), UNIT_ACTIVE);

        log_info("Attach %s, target %s, parent %s", u->id, target->id, parent ? parent->id : "(root)");
        ASSERT_OK(strv_extend(&attempts, u->id));

        if (streq(u->instance, "fail"))
                return -ECANCELED;
        if (!streq(u->id, "partition@lab.scheduler"))
                scheduler_set_state(s, SCHEDULER_ATTACHED);
        return 0;
}

void scheduler_detach(Scheduler *s, bool initiate) {
        log_info("Detach %s", UNIT(s)->id);
        scheduler_set_state(s, SCHEDULER_DEAD);
}

bool scheduler_link_is_live(Scheduler *s) {
        return unit_active_state(UNIT_DEREF(s->target_slice)) == UNIT_ACTIVE;
}

int slice_get_effective_scheduler(Slice *slice, Unit **ret) {
        *ret = slice == foreign ? NULL : slice_get_configured_scheduler(slice);
        return 0;
}

/* The in-process provider owns no kernel objects or child processes. */
void scheduler_runtime_done(Scheduler *s) {
        ASSERT_NULL(s->runtime);
}

void scheduler_sigchld(Unit *u, pid_t pid, int code, int status) {
        ASSERT_NULL(SCHEDULER(u)->runtime);
}

int scheduler_runtime_serialize(Scheduler *s, FILE *f, FDSet *fds) {
        return 0;
}

int scheduler_runtime_deserialize(Scheduler *s, const char *key, const char *value, FDSet *fds) {
        return 0;
}

int scheduler_runtime_coldplug(Scheduler *s) {
        return -EOPNOTSUPP;
}

static void wait_for_unit(Unit *u, UnitActiveState state) {
        usec_t until = now(CLOCK_MONOTONIC) + 15 * USEC_PER_SEC;

        while (unit_active_state(u) != state || (state != UNIT_ACTIVATING && u->job)) {
                ASSERT_LT(now(CLOCK_MONOTONIC), until);
                ASSERT_OK(sd_event_run(u->manager->event, 100 * USEC_PER_MSEC));
        }
}

static void check_markers(const char *runtime, const char *instance, bool expected) {
        FOREACH_STRING(suffix, ".condition", ".pre", ".start") {
                _cleanup_free_ char *file = ASSERT_PTR(strjoin(runtime, "/", instance, suffix));
                ASSERT_EQ(access(file, F_OK) >= 0, expected);
        }
}

static Unit* load(Manager *m, const char *name) {
        Unit *u;

        ASSERT_OK(manager_load_startable_unit_or_warn(m, name, /* path= */ NULL, LOG_ERR, &u));
        return u;
}

TEST(scheduler_transactions) {
        _cleanup_(rm_rf_physical_and_freep) char *runtime = NULL;
        _cleanup_(sd_bus_error_free) sd_bus_error error = SD_BUS_ERROR_NULL;
        _cleanup_(manager_freep) Manager *m = NULL;
        _cleanup_free_ char *units = NULL;
        Job *shared;
        int r;

        ASSERT_OK(get_testdata_dir("test-scheduler", &units));
        ASSERT_OK(setenv_unit_path(units));
        ASSERT_NOT_NULL(runtime = setup_fake_runtime_dir());
        r = manager_new(RUNTIME_SCOPE_USER, MANAGER_TEST_RUN_BASIC | MANAGER_TEST_DONT_OPEN_EXECUTOR, &m);
        if (manager_errno_skip_test(r))
                return (void) log_tests_skipped_errno(r, "manager_new");
        ASSERT_OK(r);
        m->executor_fd = ASSERT_OK(pin_callout_binary(SYSTEMD_MULTICALL_BINARY ? SYSTEMD_BINARY_PATH : SYSTEMD_EXECUTOR_BINARY_PATH, /* ret_path= */ NULL));
        m->defaults.std_output = EXEC_OUTPUT_INHERIT;
        ASSERT_OK(manager_startup(m, /* serialization= */ NULL, /* fds= */ NULL, /* named_listen_fds= */ NULL, /* root= */ NULL));

        Unit *a = load(m, "workload@a.service"), *b = load(m, "workload@b.service");
        Unit *middle = load(m, "partition@lab.scheduler"), *leaf = load(m, "batch@lab-build.scheduler");
        Unit *target = UNIT_DEREF(SCHEDULER(leaf)->target_slice);
        leaf->stop_when_unneeded = true;
        ASSERT_EQ(unit_active_state(target), UNIT_INACTIVE);
        ASSERT_OK(manager_add_job(m, JOB_START, a, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
        shared = ASSERT_PTR(leaf->job);
        ASSERT_OK(manager_add_job(m, JOB_START, b, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
        ASSERT_PTR_EQ(leaf->job, shared);

        wait_for_unit(middle, UNIT_ACTIVATING);
        ASSERT_EQ(unit_active_state(leaf), UNIT_INACTIVE);
        ASSERT_NULL(strv_find(attempts, leaf->id));
        check_markers(runtime, "a", /* expected= */ false);
        check_markers(runtime, "b", /* expected= */ false);
        ASSERT_OK(job_finish_and_invalidate(ASSERT_PTR(a->job), JOB_CANCELED, /* recursive= */ true, /* already= */ false));
        ASSERT_PTR_EQ(leaf->job, shared);

        /* Changing the slice of an inactive waiter drops its derived scheduler ordering only. */
        ASSERT_NOT_NULL(unit_has_dependency(a, UNIT_ATOM_AFTER, leaf));
        ASSERT_OK(unit_set_slice(a, load(m, "interactive.slice")));
        ASSERT_NULL(unit_has_dependency(a, UNIT_ATOM_AFTER, leaf));
        ASSERT_OK(unit_set_slice(a, load(m, "lab-build-ci.slice")));

        scheduler_set_state(SCHEDULER(middle), SCHEDULER_ATTACHED);
        wait_for_unit(b, UNIT_ACTIVE);

        _cleanup_free_ char *marker = ASSERT_PTR(path_join(runtime, "b.start"));
        usec_t until = now(CLOCK_MONOTONIC) + 5 * USEC_PER_SEC;
        while (access(marker, F_OK) < 0) {
                ASSERT_LT(now(CLOCK_MONOTONIC), until);
                ASSERT_OK(sd_event_run(m->event, 50 * USEC_PER_MSEC));
        }

        check_markers(runtime, "a", /* expected= */ false);
        check_markers(runtime, "b", /* expected= */ true);
        ASSERT_FALSE(unit_is_unneeded(leaf));
        ASSERT_STRV_EQ(attempts, STRV_MAKE("partition@-.scheduler", "partition@lab.scheduler", "batch@lab-build.scheduler"));

        foreign = SLICE(UNIT_GET_SLICE(a));
        ASSERT_ERROR(unit_start(a, /* details= */ NULL), ENOLINK);
        check_markers(runtime, "a", /* expected= */ false);
        foreign = NULL;

        /* Clearing a middle declaration changes the next start. The still-live leaf belongs to the old parent. */
        Unit *mid_slice = UNIT_DEREF(SCHEDULER(middle)->target_slice);
        unit_ref_unset(&SLICE(mid_slice)->scheduler);
        ASSERT_ERROR(unit_start(a, /* details= */ NULL), ENOLINK);
        ASSERT_PTR_EQ(UNIT_DEREF(SCHEDULER(leaf)->runtime_parent), middle);
        ASSERT_PTR_EQ(unit_get_start_scheduler(leaf), load(m, "partition@-.scheduler"));
        unit_ref_set(&SLICE(mid_slice)->scheduler, mid_slice, middle);

        /* Explicit stop is independent of an already-admitted workload. Hold a reference to its PID. */
        _cleanup_(pidref_done) PidRef pid = PIDREF_NULL;
        ASSERT_OK(pidref_set_pid(&pid, SERVICE(b)->main_pid.pid));
        ASSERT_OK(pidref_kill(&pid, /* sig= */ 0));
        ASSERT_OK(manager_add_job(m, JOB_STOP, leaf, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
        wait_for_unit(leaf, UNIT_INACTIVE);
        ASSERT_EQ(unit_active_state(b), UNIT_ACTIVE);
        ASSERT_OK(pidref_kill(&pid, /* sig= */ 0));

        /* Run the same transaction with a pre-existing, occupied target cgroup. */
        ASSERT_EQ(unit_active_state(target), UNIT_ACTIVE);
        ASSERT_OK(manager_add_job(m, JOB_START, a, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
        wait_for_unit(a, UNIT_ACTIVE);
        ASSERT_EQ(strv_length(attempts), 4U);
        ASSERT_STREQ(attempts[3], leaf->id);

        FOREACH_STRING(instance, "fail", "skip") {
                _cleanup_free_ char *name = ASSERT_PTR(strjoin("fault@", instance, ".service"));
                _cleanup_free_ char *failed = ASSERT_PTR(strjoin("workload-failed@", instance, ".target"));
                Unit *workload = load(m, name), *on_failure = load(m, failed);

                ASSERT_OK(manager_add_job(m, JOB_START, workload, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
                wait_for_unit(on_failure, UNIT_ACTIVE);
                ASSERT_NULL(workload->job);
                ASSERT_EQ(unit_active_state(workload), UNIT_INACTIVE);
                check_markers(runtime, instance, /* expected= */ false);
        }

        Unit *failed = load(m, "batch@fail.scheduler");
        size_t n_attempts = strv_length(attempts);
        ASSERT_ERROR(unit_start(failed, /* details= */ NULL), ECANCELED);
        ASSERT_TRUE(failed->start_limit_hit);
        ASSERT_EQ(strv_length(attempts), n_attempts);

        /* Even a broken slice scheduler must allow an action-only service to complete its transition. */
        Unit *action = load(m, "action-only.service");
        ASSERT_OK(manager_add_job(m, JOB_START, action, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
        wait_for_unit(action, UNIT_ACTIVE);

        Unit *cycle = load(m, "fault@cycle.service");
        ASSERT_ERROR(manager_add_job(m, JOB_START, cycle, JOB_REPLACE, &error, /* ret= */ NULL), EDEADLK);
        ASSERT_TRUE(sd_bus_error_is_set(&error));
        check_markers(runtime, "cycle", /* expected= */ false);

        ASSERT_OK(manager_add_job(m, JOB_STOP, a, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
        ASSERT_OK(manager_add_job(m, JOB_STOP, b, JOB_REPLACE, /* reterr_error= */ NULL, /* ret= */ NULL));
        wait_for_unit(a, UNIT_INACTIVE);
        wait_for_unit(b, UNIT_INACTIVE);
        ASSERT_TRUE(unit_is_unneeded(leaf));
        ASSERT_TRUE(leaf->in_stop_when_unneeded_queue);
        attempts = strv_free(attempts);
}

static int intro(void) {
        int r = enter_cgroup_subroot(/* ret_cgroup= */ NULL);
        if (r < 0)
                return log_tests_skipped_errno(r, "writable delegated cgroup required");
        return EXIT_SUCCESS;
}

DEFINE_TEST_MAIN_WITH_INTRO(LOG_DEBUG, intro);
